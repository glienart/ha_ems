"""
HA EMS — Decision engine.

Implements a rule-based optimizer that runs every update cycle and decides:
  - Battery: charge / discharge / standby / idle
  - EV: charge / pause

Design principles:
  - Stateless per-call: takes a snapshot dict, returns a decision dict.
  - Priority order (AUTO mode):
      1. Keep battery above min_soc  (discharge protection)
      2. Cheap tariff → charge battery & EV from grid if below max_soc
      3. Solar surplus → charge battery first, then EV
      4. Expensive tariff → discharge battery to cover house load
      5. Otherwise → idle / follow EV schedule
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Optional

# Constants (inlined -- no separate const module in add-on)
BAT_CHARGE = "charge"
BAT_DISCHARGE = "discharge"
BAT_STANDBY = "standby"
BAT_IDLE = "idle"
EV_CHARGE = "charge"
EV_PAUSE = "pause"
MODE_AUTO = "auto"
MODE_ECO = "eco"
MODE_CHEAP = "cheap"
MODE_OFF = "off"
MODE_MANUAL = "manual"


_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Snapshot — everything the optimizer needs from HA state
# ---------------------------------------------------------------------------

@dataclass
class EmsSnapshot:
    """Current state snapshot passed into the optimizer."""

    # Power readings (W) — positive = producing/importing
    solar_power_w: float = 0.0
    grid_power_w: float = 0.0         # positive = importing from grid
    house_power_w: Optional[float] = None  # if None, computed from others

    # Battery
    battery_soc_pct: float = 50.0
    battery_min_soc: float = 10.0
    battery_max_soc: float = 95.0
    battery_max_charge_w: float = 3000.0
    battery_max_discharge_w: float = 3000.0

    # EV
    ev_connected: bool = False
    ev_soc_pct: Optional[float] = None
    ev_target_soc: float = 80.0
    ev_departure_time: str = "07:00"   # HH:MM
    ev_max_charge_w: float = 7400.0

    # Tariff
    tariff_eur_kwh: Optional[float] = None
    cheap_threshold: float = 0.10
    expensive_threshold: float = 0.25

    # EMS mode
    mode: str = MODE_AUTO

    # Current time (injected for testability)
    now: datetime = field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Decision result
# ---------------------------------------------------------------------------

@dataclass
class EmsDecision:
    """What the optimizer decided this cycle."""

    battery: str = BAT_IDLE       # BAT_CHARGE / BAT_DISCHARGE / BAT_STANDBY / BAT_IDLE
    ev: str = EV_PAUSE            # EV_CHARGE / EV_PAUSE
    solar_surplus_w: float = 0.0
    net_power_w: float = 0.0
    reason: str = ""


# ---------------------------------------------------------------------------
# Optimizer
# ---------------------------------------------------------------------------

class EmsOptimizer:
    """Stateless rule-based EMS decision engine."""

    def decide(self, snap: EmsSnapshot) -> EmsDecision:
        """Compute a decision from the current snapshot."""

        if snap.mode == MODE_OFF:
            return EmsDecision(reason="EMS is off")

        net, surplus = self._compute_net(snap)
        result = EmsDecision(solar_surplus_w=max(surplus, 0), net_power_w=net)

        if snap.mode == MODE_AUTO:
            self._decide_auto(snap, surplus, result)
        elif snap.mode == MODE_ECO:
            self._decide_eco(snap, surplus, result)
        elif snap.mode == MODE_CHEAP:
            self._decide_cheap(snap, result)
        # MODE_MANUAL: no-op, user controls directly

        _LOGGER.debug(
            "EMS decision: battery=%s ev=%s surplus=%.0fW reason=%s",
            result.battery, result.ev, result.solar_surplus_w, result.reason,
        )
        return result

    # ------------------------------------------------------------------
    # Mode handlers
    # ------------------------------------------------------------------

    def _decide_auto(self, snap: EmsSnapshot, surplus_w: float, result: EmsDecision):
        """AUTO: balance cost, self-consumption, and battery health."""

        tariff = snap.tariff_eur_kwh
        is_cheap = tariff is not None and tariff <= snap.cheap_threshold
        is_expensive = tariff is not None and tariff >= snap.expensive_threshold

        # 1. Battery protection — always charge if below min_soc
        if snap.battery_soc_pct < snap.battery_min_soc:
            result.battery = BAT_CHARGE
            result.ev = EV_PAUSE
            result.reason = f"Battery below min SOC ({snap.battery_soc_pct:.0f}% < {snap.battery_min_soc:.0f}%)"
            return

        # 2. Cheap tariff — grid-charge battery and EV
        if is_cheap and snap.battery_soc_pct < snap.battery_max_soc:
            result.battery = BAT_CHARGE
            result.ev = EV_CHARGE if snap.ev_connected and self._ev_needs_charge(snap) else EV_PAUSE
            result.reason = f"Cheap tariff ({tariff:.3f} €/kWh) — grid charging"
            return

        # 3. Solar surplus — charge battery first, then EV
        if surplus_w > 200:  # 200 W deadband to avoid flapping
            if snap.battery_soc_pct < snap.battery_max_soc:
                result.battery = BAT_CHARGE
                result.reason = f"Solar surplus {surplus_w:.0f}W — charging battery"
            else:
                result.battery = BAT_IDLE
                result.reason = f"Solar surplus {surplus_w:.0f}W — battery full"

            result.ev = EV_CHARGE if snap.ev_connected and self._ev_needs_charge(snap) else EV_PAUSE
            return

        # 4. Expensive tariff — discharge battery to cover load
        if is_expensive and snap.battery_soc_pct > snap.battery_min_soc:
            result.battery = BAT_DISCHARGE
            result.ev = EV_PAUSE
            result.reason = f"Expensive tariff ({tariff:.3f} €/kWh) — discharging battery"
            return

        # 5. EV schedule — charge if within window even without surplus
        if snap.ev_connected and self._ev_needs_charge(snap) and self._within_ev_window(snap):
            result.battery = BAT_IDLE
            result.ev = EV_CHARGE
            result.reason = "Within EV charge window, charging EV"
            return

        # Default — idle
        result.battery = BAT_IDLE
        result.ev = EV_PAUSE
        result.reason = "No action needed"

    def _decide_eco(self, snap: EmsSnapshot, surplus_w: float, result: EmsDecision):
        """ECO: maximise self-consumption, ignore tariff."""

        if snap.battery_soc_pct < snap.battery_min_soc:
            result.battery = BAT_CHARGE
            result.reason = "ECO: battery below min SOC"
            return

        if surplus_w > 200 and snap.battery_soc_pct < snap.battery_max_soc:
            result.battery = BAT_CHARGE
            result.ev = EV_CHARGE if snap.ev_connected and self._ev_needs_charge(snap) else EV_PAUSE
            result.reason = f"ECO: solar surplus {surplus_w:.0f}W"
            return

        if snap.battery_soc_pct > snap.battery_min_soc and snap.net_power_w > 200:
            result.battery = BAT_DISCHARGE
            result.reason = "ECO: grid import — discharging battery"
            return

        result.battery = BAT_IDLE
        result.reason = "ECO: balanced"

    def _decide_cheap(self, snap: EmsSnapshot, result: EmsDecision):
        """CHEAP: charge battery and EV whenever tariff is below threshold."""

        tariff = snap.tariff_eur_kwh
        if tariff is None:
            result.battery = BAT_IDLE
            result.reason = "CHEAP mode: no tariff sensor available"
            return

        if tariff <= snap.cheap_threshold and snap.battery_soc_pct < snap.battery_max_soc:
            result.battery = BAT_CHARGE
            result.ev = EV_CHARGE if snap.ev_connected and self._ev_needs_charge(snap) else EV_PAUSE
            result.reason = f"CHEAP: tariff {tariff:.3f} ≤ threshold {snap.cheap_threshold:.3f}"
        else:
            result.battery = BAT_IDLE
            result.ev = EV_PAUSE
            result.reason = f"CHEAP: tariff {tariff:.3f} above threshold"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_net(snap: EmsSnapshot) -> tuple[float, float]:
        """
        Net power (W) = grid_power (positive = importing).
        Solar surplus (W) = solar - house load (positive = excess solar).
        """
        house = snap.house_power_w
        if house is None:
            # Estimate: house = solar - grid  (grid negative means exporting)
            house = snap.solar_power_w - snap.grid_power_w
        surplus = snap.solar_power_w - house
        return snap.grid_power_w, surplus

    @staticmethod
    def _ev_needs_charge(snap: EmsSnapshot) -> bool:
        """True if the EV still needs charging."""
        if snap.ev_soc_pct is not None:
            return snap.ev_soc_pct < snap.ev_target_soc
        # No SOC sensor — assume it needs charge if connected
        return snap.ev_connected

    @staticmethod
    def _within_ev_window(snap: EmsSnapshot) -> bool:
        """True if we are in the overnight charge window (now → departure)."""
        try:
            dep_h, dep_m = map(int, snap.ev_departure_time.split(":"))
            departure = snap.now.replace(hour=dep_h, minute=dep_m, second=0, microsecond=0)
            # Window: 21:00 previous evening → departure
            window_start = departure.replace(hour=21, minute=0)
            if window_start > departure:
                # departure is before 21:00 today — window started yesterday
                from datetime import timedelta
                window_start -= timedelta(days=1)
            return window_start <= snap.now <= departure
        except (ValueError, AttributeError):
            return False
