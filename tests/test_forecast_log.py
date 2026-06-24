"""Tests for the daily forecast snapshot logger (forecast_log.ForecastLog)
and ConsumptionHistory.forecast_day_kwh."""
from datetime import datetime

from forecast import ConsumptionHistory
from forecast_log import ForecastLog


def test_update_and_get_range(tmp_path):
    fl = ForecastLog(path=str(tmp_path / "fc.json"))
    fl.update({
        "2026-06-20": {"solar_kwh": 12.345, "house_kwh": 8.2},
        "2026-06-21": {"solar_kwh": 10.0, "house_kwh": 9.0},
        "2026-06-22": {"solar_kwh": 5.0, "house_kwh": 7.0},
    })
    rng = fl.get_range("2026-06-21", "2026-06-22")
    assert set(rng) == {"2026-06-21", "2026-06-22"}
    assert rng["2026-06-21"]["solar_kwh"] == 10.0


def test_update_overwrites_with_latest(tmp_path):
    fl = ForecastLog(path=str(tmp_path / "fc.json"))
    fl.update({"2026-06-20": {"solar_kwh": 12.0, "house_kwh": 8.0}})
    fl.update({"2026-06-20": {"solar_kwh": 15.0, "house_kwh": 8.5}})
    assert fl.get_range("2026-06-20", "2026-06-20")["2026-06-20"]["solar_kwh"] == 15.0


def test_get_range_handles_reversed_bounds(tmp_path):
    fl = ForecastLog(path=str(tmp_path / "fc.json"))
    fl.update({"2026-06-20": {"solar_kwh": 1.0, "house_kwh": 1.0}})
    assert "2026-06-20" in fl.get_range("2026-06-25", "2026-06-15")


def test_survives_reload(tmp_path):
    path = str(tmp_path / "fc.json")
    fl = ForecastLog(path=path)
    fl.update({"2026-06-20": {"solar_kwh": 12.0, "house_kwh": 8.0}})
    fl.flush()

    reloaded = ForecastLog(path=path)
    rec = reloaded.get_range("2026-06-20", "2026-06-20")["2026-06-20"]
    assert rec["solar_kwh"] == 12.0
    assert rec["house_kwh"] == 8.0


def test_forecast_day_kwh_uses_hour_of_week_average(tmp_path):
    ch = ConsumptionHistory(path=str(tmp_path / "ch.json"))
    # Tuesday 2026-06-23: record a steady 1000 W for every hour of that weekday.
    day = datetime(2026, 6, 23)
    for h in range(24):
        ch.record(datetime(2026, 6, 23, h, 0), 1000.0)
    # 1000 W for 24 h = 24 kWh.
    assert ch.forecast_day_kwh(day) == 24.0


def test_forecast_day_kwh_falls_back_to_default(tmp_path):
    ch = ConsumptionHistory(path=str(tmp_path / "ch.json"))
    # No history at all → 500 W default for all 24 hours = 12 kWh.
    assert ch.forecast_day_kwh(datetime(2026, 6, 23)) == 12.0
