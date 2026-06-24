"""Tests for the solar self-calibration learner (forecast.SolarCalibration).

Factors are keyed per (hour-of-day, month). A cell that has never been learned
is *bootstrapped* from the first valid observation regardless of the weather;
once it holds a value, it is only refined on clear-sky days (weather residual
>= CALIB_CLEAR_SKY_MIN).
"""
from datetime import datetime

from forecast import SolarCalibration

# All scenarios are in June -> month 6.
MONTH = 6


def test_bootstraps_correction_factor(tmp_path):
    sc = SolarCalibration(path=str(tmp_path / "sc.json"), alpha=0.15)
    # Hour 10, first ever observation: house produces 90 % of the generic
    # forecast (3600 W vs 4000 W). A brand-new cell is bootstrapped straight to
    # the observed ratio (0.9), not eased in via the EMA.
    for m in range(0, 60, 5):
        sc.observe(datetime(2026, 6, 17, 10, m), actual_w=3600, forecast_w=4000)
    # Crossing into hour 11 finalizes hour 10.
    sc.observe(datetime(2026, 6, 17, 11, 0), actual_w=1500, forecast_w=3000)
    assert sc.factor(10, MONTH) == 0.9


def test_shaded_house_still_learns(tmp_path):
    """Regression: a whole day below the clear-sky threshold must still learn.

    Heavy structural shading (actual = 40 % of forecast every hour) reads as a
    'cloudy' day, but a never-learned cell must bootstrap instead of staying at
    1.0 forever.
    """
    sc = SolarCalibration(path=str(tmp_path / "sc.json"), alpha=0.15)
    for m in range(0, 60, 5):
        sc.observe(datetime(2026, 6, 17, 10, m), actual_w=1600, forecast_w=4000)
    sc.observe(datetime(2026, 6, 17, 11, 0), actual_w=0, forecast_w=0)
    assert sc.factor(10, MONTH) == 0.4          # bootstrapped despite low residual
    assert sc.hours_learned == 1


def test_apply_scales_forecast(tmp_path):
    sc = SolarCalibration(path=str(tmp_path / "sc.json"), alpha=0.15)
    for m in range(0, 60, 5):
        sc.observe(datetime(2026, 6, 17, 10, m), actual_w=3600, forecast_w=4000)
    sc.observe(datetime(2026, 6, 17, 11, 0), actual_w=0, forecast_w=0)
    out = sc.apply({"2026-06-17T10:00": 4000.0, "2026-06-17T09:00": 1000.0})
    assert out["2026-06-17T10:00"] == 3600.0   # 4000 * 0.9
    assert out["2026-06-17T09:00"] == 1000.0   # untouched hour -> factor 1.0


def test_night_hours_are_ignored(tmp_path):
    sc = SolarCalibration(path=str(tmp_path / "sc.json"))
    # Forecast below the night threshold must not create a factor.
    for m in range(0, 60, 5):
        sc.observe(datetime(2026, 6, 17, 2, m), actual_w=0, forecast_w=10)
    sc.observe(datetime(2026, 6, 17, 3, 0), actual_w=0, forecast_w=0)
    assert sc.factor(2, MONTH) == 1.0
    assert sc.hours_learned == 0


def test_cloudy_day_does_not_refine_a_learned_cell(tmp_path):
    sc = SolarCalibration(path=str(tmp_path / "sc.json"), alpha=0.15)
    # Day 1 (clear): bootstrap hour 10 to 0.9.
    for m in range(0, 60, 5):
        sc.observe(datetime(2026, 6, 17, 10, m), actual_w=3600, forecast_w=4000)
    sc.observe(datetime(2026, 6, 17, 11, 0), actual_w=0, forecast_w=0)
    assert sc.factor(10, MONTH) == 0.9
    # Day 2 (cloudy): actual half the forecast -> residual ~0.56 < 0.80, so the
    # already-learned cell must stay untouched.
    for m in range(0, 60, 5):
        sc.observe(datetime(2026, 6, 18, 10, m), actual_w=2000, forecast_w=4000)
    sc.observe(datetime(2026, 6, 18, 11, 0), actual_w=0, forecast_w=0)
    assert sc.factor(10, MONTH) == 0.9
    assert sc.hours_learned == 1


def test_factor_is_clamped(tmp_path):
    sc = SolarCalibration(path=str(tmp_path / "sc.json"), alpha=1.0)  # full weight
    # Actual hugely exceeds forecast -> ratio clamped to 3.0.
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
    assert reloaded.factor(10, MONTH) == 0.9
    assert reloaded.hours_learned == 1
