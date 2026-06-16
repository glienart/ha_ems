"""
HA EMS -- Decision engine (v0.5.6).

Three optimizer improvements:
  1. Hysteresis: avoid oscillating near cheap/expensive thresholds by requiring
     the price to cross the threshold plus a configurable dead-band before
     switching state.
  2. EPEX look-ahead: charge during the N cheapest remaining slots today even if
     the price is above the static cheap_threshold.
  3. EV urgency: if departure is imminent and the SOC gap cannot be covered
     before then, charge regardless of tariff or solar conditions.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

BAT_CHARGE    = "charge"
BAT_DISCHARGE = "discharge"
BAT_STANDBY   = "standby"
BAT_IDLE      = "idle"
EV_CHARGE     = "charge"
EV_PAUSE      = "pause"
MODE_AUTO     = "auto"
MODE_ECO      = "eco"
MODE_CHEAP    = "cheap"
MODE_OFF      = "off"
MODE_MANUAL   = "manual"

_LOGGER = logging.getLogger(__name__)


@dataclass
class EvSnapshot:
    name: str = "EV"
    charger_switch: str = ""
    soc_pct: Optional[float] = None
    target_soc: float = 80.0
    departure_time: str = "07:00"
    max_charge_w: float = 7400.0
    capacity_kwh: float = 40.0
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
    cheap_hysteresis: float = 0.01
    expensive_hysteresis: float = 0.01
    cheap_lookahead_slots: int = 4
    epex_prices_today: list = field(default_factory=list)
    mode: str = MODE_AUTO
    now: datetime = field(default_factory=datetime.now)
    scheduled_battery: Optional[str] = None  # from 24h planner


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

    def __init__(self):
        # Hysteresis state — persists between optimizer cycles
        self._bat_cheap_active: bool = False
        self._bat_expensive_active: bool = False

    # ── Public entry point ────────────────────────────────────────────────────

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

    # ── Hysteresis helpers ────────────────────────────────────────────────────

    def _update_cheap_state(self, snap: EmsSnapshot) -> bool:
        """Return True if tariff is currently 'cheap' (with hysteresis dead-band)."""
        t = snap.tariff_eur_kwh
        if t is None:
            self._bat_cheap_active = False
            return False
        if not self._bat_cheap_active and t <= snap.cheap_threshold:
            self._bat_cheap_active = True
        elif self._bat_cheap_active and t > snap.cheap_threshold + snap.cheap_hysteresis:
            self._bat_cheap_active = False
        return self._bat_cheap_active

    def _update_expensive_state(self, snap: EmsSnapshot) -> bool:
        """Return True if tariff is currently 'expensive' (with hysteresis dead-band)."""
        t = snap.tariff_eur_kwh
        if t is None:
            self._bat_expensive_active = False
            return False
        if not self._bat_expensive_active and t >= snap.expensive_threshold:
            self._bat_expensive_active = True
        elif self._bat_expensive_active and t < snap.expensive_threshold - snap.expensive_hysteresis:
            self._bat_expensive_active = False
        return self._bat_expensive_active

    # ── EPEX look-ahead ───────────────────────────────────────────────────────

    def _current_slot_is_optimal(self, snap: EmsSnapshot) -> bool:
        """
        True if the current EPEX slot is among the N cheapest remaining today,
        where N = snap.cheap_lookahead_slots.
        Falls back to False if EPEX data is unavailable.
        """
        prices = snap.epex_prices_today
        if not prices or snap.cheap_lookahead_slots <= 0:
            return False
        try:
            now_utc = datetime.now(timezone.utc)
            remaining = [
                p for p in prices
                if datetime.fromisoformat(p["end"].replace("Z", "+00:00")) > now_utc
            ]
            if not remaining:
                return False
            current = [
                p for p in remaining
                if datetime.fromisoformat(p["start"].replace("Z", "+00:00")) <= now_utc
            ]
            if not current:
                return False
            current_price = current[0]["price_eur_kwh"]
            n = snap.cheap_lookahead_slots
            sorted_prices = sorted(remaining, key=lambda p: p["price_eur_kwh"])
            cutoff = sorted_prices[min(n - 1, len(sorted_prices) - 1)]["price_eur_kwh"]
            return current_price <= cutoff
        except Exception:
            return False

    # ── EV urgency ────────────────────────────────────────────────────────────

    @staticmethod
    def _ev_urgent(ev: EvSnapshot, now: datetime) -> bool:
        """
        True if we must start charging this EV now to reach target SOC before
        departure, given its capacity and max charge power.
        Adds a 25% safety margin to the estimated charge time.
        """
        if ev.soc_pct is None or not ev.connected:
            return False
        gap = ev.target_soc - ev.soc_pct
        if gap <= 0:
            return False
        max_charge_kw = ev.max_charge_w / 1000.0
        if max_charge_kw <= 0:
            return False
        hours_needed = (gap / 100.0) * ev.capacity_kwh / max_charge_kw
        try:
            dep_h, dep_m = map(int, ev.departure_time.split(":"))
            departure = now.replace(hour=dep_h, minute=dep_m, second=0, microsecond=0)
            if departure <= now:
                departure += timedelta(days=1)
            hours_left = (departure - now).total_seconds() / 3600.0
            return hours_left < hours_needed * 1.25
        except (ValueError, AttributeError):
            return False

    # ── Mode handlers ─────────────────────────────────────────────────────────

    def _decide_auto(self, snap, surplus_w, result):
        is_cheap     = self._update_cheap_state(snap)
        is_expensive = self._update_expensive_state(snap)
        is_optimal   = self._current_slot_is_optimal(snap)
        tariff = snap.tariff_eur_kwh

        # 1. Battery protection
        if snap.battery_soc_pct < snap.battery_min_soc:
            result.battery = BAT_CHARGE
            result.ev_decisions = self._all_evs_pause(snap)
            result.reason = (
                f"Battery below min SOC "
                f"({snap.battery_soc_pct:.0f}% < {snap.battery_min_soc:.0f}%)"
            )
            return

        # 2. EV urgency — must charge now regardless of tariff/surplus
        urgent = [ev for ev in snap.evs
                  if isinstance(ev, EvSnapshot) and self._ev_urgent(ev, snap.now)]
        if urgent:
            result.battery = BAT_IDLE
            result.ev_decisions = self._decide_evs(snap, should_charge=False, urgent_set=urgent)
            result.reason = f"EV urgent charge before departure: {', '.join(ev.name for ev in urgent)}"
            return

        # 2b. Follow 24h optimized schedule (overrides reactive rules 3–7)
        if snap.scheduled_battery and snap.scheduled_battery != "idle":
            if snap.scheduled_battery == "charge" and snap.battery_soc_pct < snap.battery_max_soc:
                result.battery = BAT_CHARGE
                result.ev_decisions = self._decide_evs(snap, should_charge=True)
                result.reason = "24h optimized schedule — charge"
                return
            if snap.scheduled_battery == "discharge" and snap.battery_soc_pct > snap.battery_min_soc:
                result.battery = BAT_DISCHARGE
                result.ev_decisions = self._all_evs_pause(snap)
                result.reason = "24h optimized schedule — discharge"
                return

        # 3. Cheap tariff (hysteresis) or look-ahead optimal slot
        if (is_cheap or is_optimal) and snap.battery_soc_pct < snap.battery_max_soc:
            if is_optimal and not is_cheap:
                label = f"look-ahead optimal slot ({tariff:.3f} EUR/kWh)"
            else:
                label = f"cheap tariff ({tariff:.3f} EUR/kWh)"
            result.battery = BAT_CHARGE
            result.ev_decisions = self._decide_evs(snap, should_charge=True)
            result.reason = f"{label} — grid charging"
            return

        # 4. Solar surplus
        if surplus_w > 200:
            result.battery = (
                BAT_CHARGE if snap.battery_soc_pct < snap.battery_max_soc else BAT_IDLE
            )
            result.reason = (
                f"Solar surplus {surplus_w:.0f}W — charging battery"
                if result.battery == BAT_CHARGE
                else f"Solar surplus {surplus_w:.0f}W — battery full"
            )
            result.ev_decisions = self._decide_evs(snap, should_charge=True)
            return

        # 5. Expensive tariff (hysteresis) → discharge battery
        if is_expensive and snap.battery_soc_pct > snap.battery_min_soc:
            result.battery = BAT_DISCHARGE
            result.ev_decisions = self._all_evs_pause(snap)
            result.reason = f"Expensive tariff ({tariff:.3f} EUR/kWh) — discharging battery"
            return

        # 6. EV overnight window
        result.battery = BAT_IDLE
        result.ev_decisions = self._decide_evs(snap, should_charge=False, use_window=True)
        if any(d["decision"] == EV_CHARGE for d in result.ev_decisions):
            result.reason = "Within EV charge window — charging EVs"
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
            result.reason = "ECO: grid import — discharging battery"
            return

        result.battery = BAT_IDLE
        result.ev_decisions = self._all_evs_pause(snap)
        result.reason = "ECO: balanced"

    def _decide_cheap(self, snap, result):
        is_cheap   = self._update_cheap_state(snap)
        is_optimal = self._current_slot_is_optimal(snap)
        tariff = snap.tariff_eur_kwh
        urgent = [ev for ev in snap.evs
                  if isinstance(ev, EvSnapshot) and self._ev_urgent(ev, snap.now)]

        if (is_cheap or is_optimal) and snap.battery_soc_pct < snap.battery_max_soc:
            label = (f"look-ahead slot ({tariff:.3f})" if (is_optimal and not is_cheap)
                     else f"tariff {tariff:.3f}")
            result.battery = BAT_CHARGE
            result.ev_decisions = self._decide_evs(snap, should_charge=True)
            result.reason = f"CHEAP: {label} — charging"
        elif urgent:
            result.battery = BAT_IDLE
            result.ev_decisions = self._decide_evs(snap, should_charge=False, urgent_set=urgent)
            result.reason = f"CHEAP: EV urgent — {', '.join(ev.name for ev in urgent)}"
        else:
            result.battery = BAT_IDLE
            result.ev_decisions = self._all_evs_pause(snap)
            lbl = f"{tariff:.3f}" if tariff is not None else "unknown"
            result.reason = f"CHEAP: tariff {lbl} above threshold"

    # ── EV decision helper ────────────────────────────────────────────────────

    def _decide_evs(self, snap, should_charge: bool,
                    use_window: bool = False,
                    urgent_set: Optional[list] = None) -> list:
        urgent_set = urgent_set or []
        decisions = []
        for ev in snap.evs:
            if not isinstance(ev, EvSnapshot):
                continue
            if not ev.connected or not self._ev_needs_charge(ev):
                dec = EV_PAUSE
            elif ev in urgent_set:
                dec = EV_CHARGE
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

    # ── Static helpers ────────────────────────────────────────────────────────

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
