"""Tests for the 24h battery planner (scheduler.py)."""
import os
import time
from datetime import datetime, timedelta, timezone

import scheduler
from scheduler import build_schedule


def _utc_hour_prices(start_utc, n=24, price=0.20, cheap_hour=None, cheap=0.01):
    """Build n hourly price slots in UTC ISO form."""
    prices = []
    for h in range(n):
        s = start_utc + timedelta(hours=h)
        p = cheap if h == cheap_hour else price
        prices.append({
            "start": s.isoformat(),
            "end": (s + timedelta(hours=1)).isoformat(),
            "price_eur_kwh": p,
        })
    return prices


def test_zero_capacity_returns_empty():
    assert build_schedule(
        now=datetime(2026, 6, 17, 0, 0), solar_forecast={}, consumption_forecast={},
        epex_buy_prices=[], epex_sell_prices=[], battery_soc_pct=50,
        battery_capacity_kwh=0, battery_min_soc=10, battery_max_soc=95,
        battery_max_charge_kw=3, battery_max_discharge_kw=3,
    ) == []


def test_returns_24_slots_all_idle_without_signals():
    slots = build_schedule(
        now=datetime(2026, 6, 17, 0, 0), solar_forecast={}, consumption_forecast={},
        epex_buy_prices=[], epex_sell_prices=[], battery_soc_pct=50,
        battery_capacity_kwh=10, battery_min_soc=10, battery_max_soc=95,
        battery_max_charge_kw=3, battery_max_discharge_kw=3,
    )
    assert len(slots) == 24
    assert all(s.battery_action == "idle" for s in slots)


def test_cheapest_slot_is_scheduled_to_charge():
    os.environ["TZ"] = "UTC"
    time.tzset()
    now = datetime(2026, 6, 17, 0, 0)  # naive local == UTC under TZ=UTC
    start_utc = datetime(2026, 6, 17, 0, 0, tzinfo=timezone.utc)
    buy = _utc_hour_prices(start_utc, cheap_hour=3, cheap=0.01)
    slots = build_schedule(
        now=now, solar_forecast={}, consumption_forecast={},
        epex_buy_prices=buy, epex_sell_prices=[], battery_soc_pct=50,
        battery_capacity_kwh=10, battery_min_soc=10, battery_max_soc=95,
        battery_max_charge_kw=3, battery_max_discharge_kw=3, n_cheap_slots=1,
    )
    charging = [s for s in slots if s.battery_action == "charge"]
    assert len(charging) == 1
    assert charging[0].hour.hour == 3


def test_no_grid_charge_when_solar_covers_battery():
    """If the day's forecast solar surplus fills the battery, don't grid-charge at night."""
    os.environ["TZ"] = "UTC"
    time.tzset()
    now = datetime(2026, 6, 17, 0, 0)
    solar = {}
    for h in range(24):
        key = (now + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00")
        solar[key] = 6000 if 9 <= h <= 16 else 0   # big midday surplus
    start_utc = datetime(2026, 6, 17, 0, 0, tzinfo=timezone.utc)
    buy = _utc_hour_prices(start_utc, cheap_hour=2, cheap=0.01)
    slots = build_schedule(
        now=now, solar_forecast=solar, consumption_forecast={},
        epex_buy_prices=buy, epex_sell_prices=[], battery_soc_pct=50,
        battery_capacity_kwh=10, battery_min_soc=10, battery_max_soc=95,
        battery_max_charge_kw=3, battery_max_discharge_kw=3, n_cheap_slots=4,
    )
    # No charging in slots without solar (i.e. no night grid charging).
    night_charges = [s for s in slots if s.battery_action == "charge" and s.solar_forecast_w == 0]
    assert night_charges == []


def test_price_at_converts_local_to_utc():
    """The TZ fix: a naive *local* hour must map to the correct UTC price slot."""
    os.environ["TZ"] = "Europe/Brussels"
    time.tzset()
    base = datetime(2026, 6, 17, 0, 0, tzinfo=timezone.utc)
    prices = [{
        "start": (base + timedelta(hours=h)).isoformat(),
        "end": (base + timedelta(hours=h + 1)).isoformat(),
        "price_eur_kwh": float(h),  # price encodes the UTC hour
    } for h in range(24)]
    # Summer in Brussels is CEST (UTC+2): local 14:00 -> 12:00 UTC -> price 12.0
    assert scheduler._price_at(prices, datetime(2026, 6, 17, 14, 0)) == 12.0
