"""
24h battery schedule optimizer for HA EMS v0.5.8.

Given solar / consumption forecasts and EPEX day-ahead prices,
produces a per-hour battery action plan that minimises grid cost.

Algorithm: two-pass greedy
  Pass 1 — classify: solar-surplus → charge free; cheapest N non-solar
            slots → grid-charge; most expensive deficit hours → discharge.
  Pass 2 — feasibility: walk chronologically, apply battery SOC constraints.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

_LOGGER = logging.getLogger(__name__)
EFFICIENCY = 0.92  # one-way efficiency used for SOC accounting


@dataclass
class ScheduleSlot:
    hour: datetime
    solar_forecast_w: float = 0.0
    consumption_forecast_w: float = 0.0
    epex_buy_price: Optional[float] = None
    epex_sell_price: Optional[float] = None
    battery_action: str = "idle"   # charge / discharge / idle
    battery_kw: float = 0.0
    reason: str = ""

    @property
    def net_solar_w(self) -> float:
        return max(0.0, self.solar_forecast_w - self.consumption_forecast_w)

    @property
    def grid_needed_w(self) -> float:
        return max(0.0, self.consumption_forecast_w - self.solar_forecast_w)


def build_schedule(
    now: datetime,
    solar_forecast: dict,
    consumption_forecast: dict,
    epex_buy_prices: list,
    epex_sell_prices: list,
    battery_soc_pct: float,
    battery_capacity_kwh: float,
    battery_min_soc: float,
    battery_max_soc: float,
    battery_max_charge_kw: float,
    battery_max_discharge_kw: float,
    n_cheap_slots: int = 4,
) -> list:
    """Build and return a list[ScheduleSlot] covering the next 24 hours."""
    if battery_capacity_kwh <= 0:
        return []

    now_h = now.replace(minute=0, second=0, microsecond=0)
    slots = []
    for h in range(24):
        hour_dt = now_h + timedelta(hours=h)
        key = hour_dt.strftime("%Y-%m-%dT%H:00")
        slots.append(ScheduleSlot(
            hour=hour_dt,
            solar_forecast_w=float(solar_forecast.get(key, 0.0)),
            consumption_forecast_w=float(consumption_forecast.get(key, 500.0)),
            epex_buy_price=_price_at(epex_buy_prices, hour_dt),
            epex_sell_price=_price_at(epex_sell_prices, hour_dt),
        ))

    # ── Pass 1: classify ────────────────────────────────────────────────────

    for s in slots:
        if s.net_solar_w > 300:
            s.battery_action = "_solar"
            s.reason = f"Solar surplus {s.solar_forecast_w:.0f} W forecast"

    non_solar = sorted(
        [s for s in slots if s.battery_action != "_solar" and s.epex_buy_price is not None],
        key=lambda s: s.epex_buy_price,
    )
    for slot in non_solar[:n_cheap_slots]:
        slot.battery_action = "_cheap"
        slot.reason = f"Cheap grid ({slot.epex_buy_price:.4f} €/kWh)"

    discharge_cands = sorted(
        [
            s for s in slots
            if s.battery_action not in ("_solar", "_cheap")
            and s.epex_buy_price is not None
            and s.epex_sell_price is not None
            and s.grid_needed_w > 200
            and s.epex_buy_price > (s.epex_sell_price or 0) * 1.05
        ],
        key=lambda s: -(s.epex_buy_price or 0),
    )
    for slot in discharge_cands[:4]:
        slot.battery_action = "_discharge"
        slot.reason = f"Expensive slot ({slot.epex_buy_price:.4f} €/kWh) — discharge"

    # ── Pass 2: feasibility ─────────────────────────────────────────────────

    bat_soc = float(battery_soc_pct)
    bat_cap = float(battery_capacity_kwh)

    for slot in slots:
        act = slot.battery_action

        if act == "_solar":
            headroom = (battery_max_soc - bat_soc) / 100 * bat_cap
            charge_kw = min(slot.net_solar_w / 1000, battery_max_charge_kw, headroom)
            if charge_kw > 0.1:
                slot.battery_action = "charge"
                slot.battery_kw = round(charge_kw, 2)
                bat_soc = min(battery_max_soc,
                              bat_soc + charge_kw * EFFICIENCY / bat_cap * 100)
            else:
                slot.battery_action = "idle"
                slot.reason = "Battery full — solar surplus to grid"

        elif act == "_cheap":
            headroom = (battery_max_soc - bat_soc) / 100 * bat_cap
            charge_kw = min(battery_max_charge_kw, headroom)
            if charge_kw > 0.1:
                slot.battery_action = "charge"
                slot.battery_kw = round(charge_kw, 2)
                bat_soc = min(battery_max_soc,
                              bat_soc + charge_kw * EFFICIENCY / bat_cap * 100)
            else:
                slot.battery_action = "idle"
                slot.reason = "Battery already full"

        elif act == "_discharge":
            available = (bat_soc - battery_min_soc) / 100 * bat_cap
            discharge_kw = min(
                battery_max_discharge_kw,
                slot.grid_needed_w / 1000,
                available,
            )
            if discharge_kw > 0.1:
                slot.battery_action = "discharge"
                slot.battery_kw = round(discharge_kw, 2)
                bat_soc = max(battery_min_soc,
                              bat_soc - discharge_kw / EFFICIENCY / bat_cap * 100)
            else:
                slot.battery_action = "idle"
                slot.reason = "Insufficient charge to discharge"

        else:
            slot.battery_action = "idle"
            if not slot.reason:
                slot.reason = "No action needed"

    _LOGGER.info(
        "Schedule: %d slots · charge=%d · discharge=%d · idle=%d",
        len(slots),
        sum(1 for s in slots if s.battery_action == "charge"),
        sum(1 for s in slots if s.battery_action == "discharge"),
        sum(1 for s in slots if s.battery_action == "idle"),
    )
    return slots


def current_scheduled_action(schedule: list, now: datetime) -> Optional[ScheduleSlot]:
    """Return the ScheduleSlot covering the current hour, or None."""
    if not schedule:
        return None
    hour_now = now.replace(minute=0, second=0, microsecond=0)
    for slot in schedule:
        if slot.hour == hour_now:
            return slot
    return None


def _price_at(prices: list, dt: datetime) -> Optional[float]:
    """Find the effective price list entry covering local naive datetime dt."""
    if not prices:
        return None
    try:
        dt_utc = dt.replace(tzinfo=timezone.utc)
        for p in prices:
            start = datetime.fromisoformat(p["start"].replace("Z", "+00:00"))
            end   = datetime.fromisoformat(p["end"].replace("Z", "+00:00"))
            if start <= dt_utc < end:
                return float(p.get("price_eur_kwh", p.get("price", 0)))
    except Exception:
        pass
    return None
