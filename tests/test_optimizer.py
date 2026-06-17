"""Tests for the reactive decision engine (optimizer.py)."""
from datetime import datetime

import optimizer as opt
from optimizer import EmsOptimizer, EmsSnapshot, EvSnapshot


def _decide(**kwargs):
    return EmsOptimizer().decide(EmsSnapshot(**kwargs))


def test_mode_off_is_idle():
    d = _decide(mode="off")
    assert d.battery == opt.BAT_IDLE
    assert d.reason == "EMS is off"


def test_battery_below_min_forces_charge():
    d = _decide(mode="auto", battery_soc_pct=5, battery_min_soc=10)
    assert d.battery == opt.BAT_CHARGE
    assert "below min" in d.reason.lower()


def test_cheap_tariff_charges():
    d = _decide(mode="auto", battery_soc_pct=50, tariff_eur_kwh=0.05,
                cheap_threshold=0.10)
    assert d.battery == opt.BAT_CHARGE
    assert "cheap" in d.reason.lower()


def test_expensive_tariff_discharges():
    d = _decide(mode="auto", battery_soc_pct=50, tariff_eur_kwh=0.30,
                expensive_threshold=0.25)
    assert d.battery == opt.BAT_DISCHARGE
    assert "expensive" in d.reason.lower()


def test_solar_surplus_charges():
    # No tariff signal; pure solar surplus should drive a charge.
    d = _decide(mode="auto", battery_soc_pct=50,
                solar_power_w=3000, house_power_w=500)
    assert d.battery == opt.BAT_CHARGE
    assert "surplus" in d.reason.lower()


def test_ev_urgent_charges_before_departure():
    now = datetime(2026, 6, 17, 6, 0)
    ev = EvSnapshot(name="Car", charger_switch="switch.car", soc_pct=20,
                    target_soc=80, departure_time="07:00", max_charge_w=7400,
                    capacity_kwh=40, connected=True)
    d = EmsOptimizer().decide(EmsSnapshot(mode="auto", now=now, evs=[ev],
                                          battery_soc_pct=50))
    assert "urgent" in d.reason.lower()
    assert d.ev_decisions[0]["decision"] == opt.EV_CHARGE


def test_cheap_hysteresis_dead_band():
    o = EmsOptimizer()
    snap = EmsSnapshot(cheap_threshold=0.10, cheap_hysteresis=0.01)

    snap.tariff_eur_kwh = 0.09          # below threshold -> becomes cheap
    assert o._update_cheap_state(snap) is True

    snap.tariff_eur_kwh = 0.105         # inside dead-band -> stays cheap
    assert o._update_cheap_state(snap) is True

    snap.tariff_eur_kwh = 0.12          # above threshold + hysteresis -> exits
    assert o._update_cheap_state(snap) is False
