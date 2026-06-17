"""
Solar production forecast, consumption history and solar self-calibration for HA EMS v0.5.30.

Solar forecast: Forecast.Solar API (free, no API key required).
  GET https://api.forecast.solar/estimate/{lat}/{lon}/{dec}/{az}/{kwp}
  dec = panel tilt 0-90° (0=horizontal, 90=vertical)
  az  = azimuth -180..180 from south (0=south, -90=east, 90=west)

Consumption history: in-memory rolling buffer, per-hour-of-week average.
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta

_LOGGER = logging.getLogger(__name__)
FORECAST_SOLAR_BASE = "https://api.forecast.solar/estimate"
CONSUMPTION_PATH = "/data/consumption_history.json"
SOLAR_CALIB_PATH = "/data/solar_calibration.json"
CALIB_ALPHA = 0.15      # EMA learning rate (slow, robust to noisy days)
CALIB_MIN_FC_W = 100    # ignore hours where the forecast is essentially night
CALIB_MIN_RATIO = 0.2   # clamp the per-hour correction to a sane band
CALIB_MAX_RATIO = 3.0


async def fetch_solar_forecast(
    lat: float, lon: float, tilt: int, azimuth: int, kwp: float
) -> dict:
    """
    Fetch hourly PV production forecast (W) from Forecast.Solar.
    Returns {hour_key: watts} e.g. {"2024-06-15T10:00": 3500.0}.
    Returns {} if parameters are not configured or on error.
    """
    if kwp <= 0 or (lat == 0.0 and lon == 0.0):
        return {}

    url = f"{FORECAST_SOLAR_BASE}/{lat}/{lon}/{tilt}/{azimuth}/{kwp}"
    _LOGGER.info("Fetching solar forecast: %s", url)

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
        if resp.status_code == 429:
            _LOGGER.warning("Forecast.Solar rate-limited (429) — will retry next cycle")
            return {}
        if resp.status_code != 200:
            _LOGGER.warning("Forecast.Solar HTTP %s", resp.status_code)
            return {}
        data = resp.json()
        # result.watts: {"2024-06-15 08:00:00": 1230, ...}
        watts = data.get("result", {}).get("watts", {})
        hourly: dict = {}
        for ts_str, w in watts.items():
            try:
                dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                hourly[dt.strftime("%Y-%m-%dT%H:00")] = float(w)
            except (ValueError, KeyError):
                pass
        _LOGGER.info("Solar forecast: %d hourly values", len(hourly))
        return hourly
    except Exception as exc:
        _LOGGER.error("Forecast.Solar error: %s", exc)
        return {}


class ConsumptionHistory:
    """
    Rolling in-memory buffer of house consumption readings.
    Computes per-hour-of-week average for the forecasting window.
    """

    def __init__(self, max_days: int = 7, path: str = CONSUMPTION_PATH):
        self._max_days = max_days
        self._path = path
        # hour_of_week (0..167) -> list[(epoch_float, watts)]
        self._data: dict[int, list] = defaultdict(list)
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        try:
            with open(self._path) as f:
                raw = json.load(f)
            for k, entries in raw.items():
                # JSON keys are strings; convert back to int hour-of-week
                self._data[int(k)] = [(float(e), float(w)) for e, w in entries]
            _LOGGER.info("Consumption history loaded: %d buckets",
                         sum(len(v) for v in self._data.values()))
        except Exception as exc:
            _LOGGER.error("Failed to load consumption history: %s", exc)
            self._data = defaultdict(list)

    def save(self) -> None:
        if not self._path:
            return
        try:
            data = {str(k): v for k, v in self._data.items() if v}
            with open(self._path, "w") as f:
                json.dump(data, f)
        except Exception as exc:
            _LOGGER.error("Failed to save consumption history: %s", exc)

    def record(self, ts: datetime, watts: float) -> None:
        """Record a consumption reading."""
        if watts is None or watts < 0:
            return
        key = ts.weekday() * 24 + ts.hour  # 0..167
        epoch = ts.timestamp()
        self._data[key].append((epoch, watts))
        # Trim entries older than max_days
        cutoff = (ts - timedelta(days=self._max_days)).timestamp()
        self._data[key] = [(e, w) for e, w in self._data[key] if e >= cutoff]
        # Hard cap
        if len(self._data[key]) > self._max_days * 120:
            self._data[key] = self._data[key][-self._max_days * 120:]

    def forecast_next_24h(self, now: datetime) -> dict:
        """
        Return {hour_key: avg_watts} for the next 24 hours.
        Falls back to 500 W for hours with no history.
        """
        result = {}
        for h in range(24):
            future = now + timedelta(hours=h)
            key = future.weekday() * 24 + future.hour
            readings = self._data.get(key, [])
            avg = sum(w for _, w in readings) / len(readings) if readings else 500.0
            result[future.strftime("%Y-%m-%dT%H:00")] = round(avg, 1)
        return result

    @property
    def has_enough_data(self) -> bool:
        """True once we have at least ~100 readings (roughly 1–2 h of data)."""
        return sum(len(v) for v in self._data.values()) > 100


class SolarCalibration:
    """Learns a per-hour-of-day correction factor between the generic
    Forecast.Solar prediction and *this house's* actual PV production.

    Every clock hour, the average measured PV power is compared to the
    forecast for that hour; the (clamped) ratio updates an exponential moving
    average. Future forecasts are multiplied by these factors so the prediction
    gradually adapts to local shading, soiling and orientation errors.
    """

    def __init__(self, path: str = SOLAR_CALIB_PATH, alpha: float = CALIB_ALPHA):
        self._path = path
        self._alpha = alpha
        self._factors: dict[int, float] = {}        # hour_of_day -> factor
        self._cur = {"hour": None, "sum": 0.0, "n": 0, "fc": 0.0}
        self._load()

    def _load(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        try:
            with open(self._path) as f:
                raw = json.load(f)
            self._factors = {int(k): float(v) for k, v in raw.get("factors", {}).items()}
            _LOGGER.info("Solar calibration loaded: %d learned hours", len(self._factors))
        except Exception as exc:
            _LOGGER.error("Failed to load solar calibration: %s", exc)

    def save(self) -> None:
        if not self._path:
            return
        try:
            with open(self._path, "w") as f:
                json.dump({"factors": {str(k): v for k, v in self._factors.items()}}, f)
        except Exception as exc:
            _LOGGER.error("Failed to save solar calibration: %s", exc)

    def factor(self, hour: int) -> float:
        return self._factors.get(hour, 1.0)

    def observe(self, now: datetime, actual_w, forecast_w) -> None:
        """Feed one measurement; measurements are rolled up per clock hour."""
        if actual_w is None:
            return
        hour = now.hour
        if self._cur["hour"] is None:
            self._cur = {"hour": hour, "sum": 0.0, "n": 0, "fc": forecast_w or 0.0}
        elif self._cur["hour"] != hour:
            self._finalize()
            self._cur = {"hour": hour, "sum": 0.0, "n": 0, "fc": forecast_w or 0.0}
        self._cur["sum"] += max(0.0, actual_w)
        self._cur["n"] += 1
        if forecast_w:
            self._cur["fc"] = forecast_w

    def _finalize(self) -> None:
        c = self._cur
        if c["n"] <= 0 or c["fc"] < CALIB_MIN_FC_W:
            return
        avg = c["sum"] / c["n"]
        ratio = max(CALIB_MIN_RATIO, min(CALIB_MAX_RATIO, avg / c["fc"]))
        old = self._factors.get(c["hour"], 1.0)
        self._factors[c["hour"]] = round((1 - self._alpha) * old + self._alpha * ratio, 4)

    def apply(self, forecast_dict: dict) -> dict:
        """Return a calibrated copy of {hour_key: watts}."""
        out = {}
        for key, w in forecast_dict.items():
            try:
                hour = int(key[11:13])
            except (ValueError, IndexError):
                hour = None
            f = self._factors.get(hour, 1.0) if hour is not None else 1.0
            out[key] = round(w * f, 1)
        return out

    @property
    def hours_learned(self) -> int:
        return len(self._factors)

    @property
    def mean_factor(self) -> float:
        if not self._factors:
            return 1.0
        return round(sum(self._factors.values()) / len(self._factors), 3)
