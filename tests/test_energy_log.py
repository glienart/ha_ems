"""Tests for the energy cost logger (energy_log.py)."""
import energy_log
from energy_log import EnergyLogger


def _logger(tmp_path):
    energy_log.LOG_PATH = str(tmp_path / "energy_log.json")
    return EnergyLogger()


def test_import_accumulates_kwh_and_cost(tmp_path):
    el = _logger(tmp_path)
    # 1000 W imported for 3600 s = 1.0 kWh at 0.25 EUR/kWh = 0.25 EUR
    el.record(grid_w=1000, tariff_consumption=0.25, tariff_injection=0.05,
              interval_s=3600)
    totals = el.get_history("today")["totals"]
    assert round(totals["kwh_in"], 3) == 1.0
    assert round(totals["cost"], 3) == 0.25


def test_export_accumulates_revenue_and_net(tmp_path):
    el = _logger(tmp_path)
    el.record(grid_w=1000, tariff_consumption=0.25, tariff_injection=0.05,
              interval_s=3600)   # +0.25 cost
    el.record(grid_w=-2000, tariff_consumption=0.25, tariff_injection=0.05,
              interval_s=3600)   # 2 kWh out * 0.05 = 0.10 revenue
    totals = el.get_history("today")["totals"]
    assert round(totals["kwh_out"], 3) == 2.0
    assert round(totals["revenue"], 3) == 0.10
    assert round(totals["net_cost"], 3) == 0.15


def test_flush_clears_dirty_flag(tmp_path):
    el = _logger(tmp_path)
    el.record(grid_w=500, tariff_consumption=0.2, tariff_injection=0.05,
              interval_s=60)
    el._dirty = True
    el.flush()
    assert el._dirty is False


def test_history_hourly_anchored_to_date(tmp_path):
    el = _logger(tmp_path)
    el._data = {
        "2026-06-16T10": {"kwh_in": 1.0, "kwh_out": 0.0, "kwh_house": 1.0, "cost": 0.2, "revenue": 0.0},
        "2026-06-17T10": {"kwh_in": 2.0, "kwh_out": 0.0, "kwh_house": 2.0, "cost": 0.4, "revenue": 0.0},
    }
    h = el.get_history("hourly", date="2026-06-16")
    assert h["date"] == "2026-06-16"
    assert [i["label"] for i in h["items"]] == ["10:00"]
    assert h["totals"]["kwh_in"] == 1.0


def test_history_daily_filters_to_month(tmp_path):
    el = _logger(tmp_path)
    el._data = {
        "2026-06-16T10": {"kwh_in": 1.0, "kwh_out": 0.0, "kwh_house": 1.0, "cost": 0.0, "revenue": 0.0},
        "2026-06-17T10": {"kwh_in": 2.0, "kwh_out": 0.0, "kwh_house": 2.0, "cost": 0.0, "revenue": 0.0},
        "2026-05-01T10": {"kwh_in": 9.0, "kwh_out": 0.0, "kwh_house": 9.0, "cost": 0.0, "revenue": 0.0},
    }
    h = el.get_history("daily", date="2026-06-17")
    assert [i["label"] for i in h["items"]] == ["2026-06-16", "2026-06-17"]  # May excluded
    assert h["totals"]["kwh_in"] == 3.0


def test_history_monthly_filters_to_year(tmp_path):
    el = _logger(tmp_path)
    el._data = {
        "2026-06-16T10": {"kwh_in": 1.0, "kwh_out": 0.0, "kwh_house": 1.0, "cost": 0.0, "revenue": 0.0},
        "2026-07-16T10": {"kwh_in": 2.0, "kwh_out": 0.0, "kwh_house": 2.0, "cost": 0.0, "revenue": 0.0},
        "2025-07-16T10": {"kwh_in": 5.0, "kwh_out": 0.0, "kwh_house": 5.0, "cost": 0.0, "revenue": 0.0},
    }
    h = el.get_history("monthly", date="2026-03-01")
    assert [i["label"] for i in h["items"]] == ["2026-06", "2026-07"]  # 2025 excluded
    assert h["totals"]["kwh_in"] == 3.0


def test_legacy_period_names_still_work(tmp_path):
    el = _logger(tmp_path)
    el.record(grid_w=1000, tariff_consumption=0.25, tariff_injection=0.05, interval_s=3600)
    # "today" maps to hourly anchored on today
    assert el.get_history("today")["period"] == "hourly"
