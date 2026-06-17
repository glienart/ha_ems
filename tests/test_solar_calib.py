"""Tests for the solar self-calibration learner (forecast.SolarCalibration)."""
from datetime import datetime

from forecast import SolarCalibration


def test_learns_correction_factor(tmp_path):
    sc = SolarCalibration(path=str(tmp_path / "sc.json"), alpha=0.15)
    # Hour 10: house produces ~half of the generic forecast (2000 W vs 4000 W).
    for m in range(0, 60, 5):
        sc.observe(datetime(2026, 6, 17, 10, m), actual_w=2000, forecast_w=4000)
    # Crossing into hour 11 finalizes hour 10.
    sc.observe(datetime(2026, 6, 17, 11, 0), actual_w=1500, forecast_w=3000)
    # EMA: 0.85*1.0 + 0.15*0.5 = 0.925
    assert sc.factor(10) == 0.925


def test_apply_scales_forecast(tmp_path):
    sc = SolarCalibration(path=str(tmp_path / "sc.json"), alpha=0.15)
    for m in range(0, 60, 5):
        sc.observe(datetime(2026, 6, 17, 10, m), actual_w=2000, forecast_w=4000)
    sc.observe(datetime(2026, 6, 17, 11, 0), actual_w=0, forecast_w=0)
    out = sc.apply({"2026-06-17T10:00": 4000.0, "2026-06-17T09:00": 1000.0})
    assert out["2026-06-17T10:00"] == 3700.0   # 4000 * 0.925
    assert out["2026-06-17T09:00"] == 1000.0   # untouched hour -> factor 1.0


def test_night_hours_are_ignored(tmp_path):
    sc = SolarCalibration(path=str(tmp_path / "sc.json"))
    # Forecast below the night threshold must not create a factor.
    for m in range(0, 60, 5):
        sc.observe(datetime(2026, 6, 17, 2, m), actual_w=0, forecast_w=10)
    sc.observe(datetime(2026, 6, 17, 3, 0), actual_w=0, forecast_w=0)
    assert sc.factor(2) == 1.0
    assert sc.hours_learned == 0


def test_factor_is_clamped(tmp_path):
    sc = SolarCalibration(path=str(tmp_path / "sc.json"), alpha=1.0)  # full weight
    # Actual hugely exceeds forecast -> ratio clamped to 3.0
    for m in range(0, 60, 5):
        sc.observe(datetime(2026, 6, 17, 12, m), actual_w=50000, forecast_w=1000)
    sc.observe(datetime(2026, 6, 17, 13, 0), actual_w=0, forecast_w=0)
    assert sc.factor(12) == 3.0


def test_calibration_survives_reload(tmp_path):
    path = str(tmp_path / "sc.json")
    sc = SolarCalibration(path=path, alpha=0.15)
    for m in range(0, 60, 5):
        sc.observe(datetime(2026, 6, 17, 10, m), actual_w=2000, forecast_w=4000)
    sc.observe(datetime(2026, 6, 17, 11, 0), actual_w=0, forecast_w=0)
    sc.save()

    reloaded = SolarCalibration(path=path)
    assert reloaded.factor(10) == 0.925
    assert reloaded.hours_learned == 1
