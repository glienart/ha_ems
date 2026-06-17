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
