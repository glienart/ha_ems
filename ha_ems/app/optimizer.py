"""
HA EMS -- Decision engine.

Implements a rule-based optimizer that runs every update cycle and decides:
  - Battery: charge / discharge / standby / idle
  - EVs: charge / pause  (one decision per configured vehicle)

Priority order (AUTO mode):
  1. Keep battery above min_soc  (discharge protection)
  2. Cheap tariff -> charge battery & all connected EVs from grid
  3. Solar surplus -> charge battery first, then connected EVs
  4. Expensive tariff -> discharge battery to cover house load
  5. Otherwise -> follow each EV's overnight schedule
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

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


@dataclass
class EvSnapshot:
    name: str = "EV"
    charger_switch: str = ""
    soc_pct: Optional[float] = None
    target_soc: float = 80.0
    departure_time: str = "07:00"
    max_charge_w: float = 7400.0
    connected: bool = False


@dataclass
class EmsSnapshot:
    solar_power_w: float = 0.0
    grid_power_w: float = 0.0
    house_power_w: Optional[float] = None
    battery_soc_pct: float = 50.0
    battery_min_soc: float = 10.0
    battery_max_soc: float = 95.0
    battery_max_charge_w: float = 3000.0
    battery_max_discharge_w: float = 3000.0
    evs: list = field(default_factory=list)
    tariff_eur_kwh: Optional[float] = None
    cheap_threshold: float = 0.10
    expensive_threshold: float = 0.25
    mode: str = MODE_AUTO
    now: datetime = field(default_factory=datetime.now)


@dataclass
class EmsDecision:
    battery: str = BAT_IDLE
    ev_decisions: list = field(default_factory=list)
    solar_surplus_w: float = 0.0
    net_power_w: float = 0.0
    reason: str = ""

    @property
    def ev(self) -> str:
        if self.ev_decisions:
            return self.ev_decisions[0].get("decision", EV_PAUSE)
        return EV_PAUSE


class EmsOptimizer:

    def decide(self, snap: EmsSnapshot) -> EmsDecision:
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

        _LOGGER.debug(
            "EMS decision: battery=%s evs=%s surplus=%.0fW reason=%s",
            result.battery,
            [(d["name"], d["decision"]) for d in result.ev_decisions],
            result.solar_surplus_w,
            result.reason,
        )
        return result

    def _decide_auto(self, snap, surplus_w, result):
        tariff = snap.tariff_eur_kwh
        is_cheap = tariff is not None and tariff <= snap.cheap_threshold
        is_expensive = tariff is not None and tariff >= snap.expensive_threshold

        if snap.battery_soc_pct < snap.battery_min_soc:
            result.battery = BAT_CHARGE
            result.ev_decisions = self._all_evs_pause(snap)
            result.reason = (
                f"Battery below min SOC "
                f"({snap.battery_soc_pct:.0f}% < {snap.battery_min_soc:.0f}%)"
            )
            return

        if is_cheap and snap.battery_soc_pct < snap.battery_max_soc:
            result.battery = BAT_CHARGE
            result.ev_decisions = self._decide_evs(snap, should_charge=True)
            result.reason = f"Cheap tariff ({tariff:.3f} EUR/kWh) -- grid charging"
            return

        if surplus_w > 200:
            result.battery = (
                BAT_CHARGE if snap.battery_soc_pct < snap.battery_max_soc else BAT_IDLE
            )
            result.reason = (
                f"Solar surplus {surplus_w:.0f}W -- charging battery"
                if result.battery == BAT_CHARGE
                else f"Solar surplus {surplus_w:.0f}W -- battery full"
            )
            result.ev_decisions = self._decide_evs(snap, should_charge=True)
            return

        if is_expensive and snap.battery_soc_pct > snap.battery_min_soc:
            result.battery = BAT_DISCHARGE
            result.ev_decisions = self._all_evs_pause(snap)
            result.reason = f"Expensive tariff ({tariff:.3f} EUR/kWh) -- discharging battery"
            return

        result.battery = BAT_IDLE
        result.ev_decisions = self._decide_evs(snap, should_charge=False, use_window=True)
        if any(d["decision"] == EV_CHARGE for d in result.ev_decisions):
            result.reason = "Within EV charge window -- charging EVs"
            return

        result.reason = "No action needed"

    def _decide_eco(self, snap, surplus_w, result):
        if snap.battery_soc_pct < snap.battery_min_soc:
            result.battery = BAT_CHARGE
            result.ev_decisions = self._all_evs_pause(snap)
            result.reason = "ECO: battery below min SOC"
            return

        if surplus_w > 200 and snap.battery_soc_pct < snap.battery_max_soc:
            result.battery = BAT_CHARGE
            result.ev_decisions = self._decide_evs(snap, should_charge=True)
            result.reason = f"ECO: solar surplus {surplus_w:.0f}W"
            return

        if snap.battery_soc_pct > snap.battery_min_soc and snap.grid_power_w > 200:
            result.battery = BAT_DISCHARGE
            result.ev_decisions = self._all_evs_pause(snap)
            result.reason = "ECO: grid import -- discharging battery"
            return

        result.battery = BAT_IDLE
        result.ev_decisions = self._all_evs_pause(snap)
        result.reason = "ECO: balanced"

    def _decide_cheap(self, snap, result):
        tariff = snap.tariff_eur_kwh
        if tariff is None:
            result.battery = BAT_IDLE
            result.ev_decisions = self._all_evs_pause(snap)
            result.reason = "CHEAP mode: no tariff sensor available"
            return

        if tariff <= snap.cheap_threshold and snap.battery_soc_pct < snap.battery_max_soc:
            result.battery = BAT_CHARGE
            result.ev_decisions = self._decide_evs(snap, should_charge=True)
            result.reason = (
                f"CHEAP: tariff {tariff:.3f} <= threshold {snap.cheap_threshold:.3f}"
            )
        else:
            result.battery = BAT_IDLE
            result.ev_decisions = self._all_evs_pause(snap)
            result.reason = f"CHEAP: tariff {tariff:.3f} above threshold"

    def _decide_evs(self, snap, should_charge: bool, use_window: bool = False) -> list:
        decisions = []
        for ev in snap.evs:
            if not isinstance(ev, EvSnapshot):
                continue
            if not ev.connected or not self._ev_needs_charge(ev):
                dec = EV_PAUSE
            elif should_charge:
                dec = EV_CHARGE
            elif use_window and self._within_ev_window(ev, snap.now):
                dec = EV_CHARGE
            else:
                dec = EV_PAUSE
            decisions.append({
                "name": ev.name,
                "charger_switch": ev.charger_switch,
                "decision": dec,
            })
        return decisions

    def _all_evs_pause(self, snap) -> list:
        return [
            {"name": ev.name, "charger_switch": ev.charger_switch, "decision": EV_PAUSE}
            for ev in snap.evs if isinstance(ev, EvSnapshot)
        ]

    @staticmethod
    def _compute_net(snap):
        house = snap.house_power_w
        if house is None:
            house = snap.solar_power_w - snap.grid_power_w
        surplus = snap.solar_power_w - house
        return snap.grid_power_w, surplus

    @staticmethod
    def _ev_needs_charge(ev: EvSnapshot) -> bool:
        if ev.soc_pct is not None:
            return ev.soc_pct < ev.target_soc
        return ev.connected

    @staticmethod
    def _within_ev_window(ev: EvSnapshot, now: datetime) -> bool:
        try:
            dep_h, dep_m = map(int, ev.departure_time.split(":"))
            departure = now.replace(hour=dep_h, minute=dep_m, second=0, microsecond=0)
            window_start = departure.replace(hour=21, minute=0)
            if window_start > departure:
                window_start -= timedelta(days=1)
            return window_start <= now <= departure
        except (ValueError, AttributeError):
            return False
