"""Tests for the solar self-calibration learner (forecast.SolarCalibration).

Factors are keyed per (hour-of-day, month) and the long-term factor is only
updated on clear-sky days (weather residual >= CALIB_CLEAR_SKY_MIN), so the
scenarios below feed a clear day where actual production is a steady 90 % of
the generic forecast.
"""
from datetime import datetime

from forecast import SolarCalibration

# All scenarios are in June -> month 6.
MONTH = 6


def test_learns_correction_factor(tmp_path):
    sc = SolarCalibration(path=str(tmp_path / "sc.json"), alpha=0.15)
    # Hour 10 on a clear day: house produces 90 % of the generic forecast
    # (3600 W vs 4000 W) -> residual 0.9 is above the clear-sky threshold.
    for m in range(0, 60, 5):
        sc.observe(datetime(2026, 6, 17, 10, m), actual_w=3600, forecast_w=4000)
    # Crossing into hour 11 finalizes hour 10.
    sc.observe(datetime(2026, 6, 17, 11, 0), actual_w=1500, forecast_w=3000)
    # EMA: 0.85*1.0 + 0.15*0.9 = 0.985
    assert sc.factor(10, MONTH) == 0.985


def test_apply_scales_forecast(tmp_path):
    sc = SolarCalibration(path=str(tmp_path / "sc.json"), alpha=0.15)
    for m in range(0, 60, 5):
        sc.observe(datetime(2026, 6, 17, 10, m), actual_w=3600, forecast_w=4000)
    sc.observe(datetime(2026, 6, 17, 11, 0), actual_w=0, forecast_w=0)
    out = sc.apply({"2026-06-17T10:00": 4000.0, "2026-06-17T09:00": 1000.0})
    assert out["2026-06-17T10:00"] == 3940.0   # 4000 * 0.985
    assert out["2026-06-17T09:00"] == 1000.0   # untouched hour -> factor 1.0


def test_night_hours_are_ignored(tmp_path):
    sc = SolarCalibration(path=str(tmp_path / "sc.json"))
    # Forecast below the night threshold must not create a factor.
    for m in range(0, 60, 5):
        sc.observe(datetime(2026, 6, 17, 2, m), actual_w=0, forecast_w=10)
    sc.observe(datetime(2026, 6, 17, 3, 0), actual_w=0, forecast_w=0)
    assert sc.factor(2, MONTH) == 1.0
    assert sc.hours_learned == 0


def test_cloudy_days_do_not_learn(tmp_path):
    sc = SolarCalibration(path=str(tmp_path / "sc.json"), alpha=0.15)
    # Heavy clouds: actual is only half the forecast -> residual 0.5 is below
    # the clear-sky threshold, so the structural factor must stay untouched.
    for m in range(0, 60, 5):
        sc.observe(datetime(2026, 6, 17, 10, m), actual_w=2000, forecast_w=4000)
    sc.observe(datetime(2026, 6, 17, 11, 0), actual_w=0, forecast_w=0)
    assert sc.factor(10, MONTH) == 1.0
    assert sc.hours_learned == 0


def test_factor_is_clamped(tmp_path):
    sc = SolarCalibration(path=str(tmp_path / "sc.json"), alpha=1.0)  # full weight
    # Actual hugely exceeds forecast -> ratio clamped to 3.0 (and the day reads
    # as very sunny, well above the clear-sky threshold).
    for m in range(0, 60, 5):
        sc.observe(datetime(2026, 6, 17, 12, m), actual_w=50000, forecast_w=1000)
    sc.observe(datetime(2026, 6, 17, 13, 0), actual_w=0, forecast_w=0)
    assert sc.factor(12, MONTH) == 3.0


def test_calibration_survives_reload(tmp_path):
    path = str(tmp_path / "sc.json")
    sc = SolarCalibration(path=path, alpha=0.15)
    for m in range(0, 60, 5):
        sc.observe(datetime(2026, 6, 17, 10, m), actual_w=3600, forecast_w=4000)
    sc.observe(datetime(2026, 6, 17, 11, 0), actual_w=0, forecast_w=0)
    sc.save()

    reloaded = SolarCalibration(path=path)
    assert reloaded.factor(10, MONTH) == 0.985
    assert reloaded.hours_learned == 1
