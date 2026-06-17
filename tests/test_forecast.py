"""Tests for ConsumptionHistory persistence and forecasting (forecast.py)."""
from datetime import datetime

from forecast import ConsumptionHistory


def test_forecast_defaults_to_500_without_history(tmp_path):
    h = ConsumptionHistory(path=str(tmp_path / "c.json"))
    now = datetime(2026, 6, 17, 14, 0)
    assert h.forecast_next_24h(now)["2026-06-17T14:00"] == 500.0


def test_forecast_averages_recorded_values(tmp_path):
    h = ConsumptionHistory(path=str(tmp_path / "c.json"))
    now = datetime(2026, 6, 17, 14, 0)
    for w in (800, 900, 1000):
        h.record(now, w)
    assert h.forecast_next_24h(now)["2026-06-17T14:00"] == 900.0


def test_history_survives_reload(tmp_path):
    path = str(tmp_path / "c.json")
    h = ConsumptionHistory(path=path)
    now = datetime(2026, 6, 17, 14, 0)
    for w in (800, 900, 1000):
        h.record(now, w)
    h.save()

    reloaded = ConsumptionHistory(path=path)
    assert reloaded.forecast_next_24h(now)["2026-06-17T14:00"] == 900.0


def test_negative_readings_are_ignored(tmp_path):
    h = ConsumptionHistory(path=str(tmp_path / "c.json"))
    now = datetime(2026, 6, 17, 14, 0)
    h.record(now, -50)
    assert h.forecast_next_24h(now)["2026-06-17T14:00"] == 500.0
