"""
Solar production forecast and consumption history for HA EMS v0.5.8.

Solar forecast: Forecast.Solar API (free, no API key required).
  GET https://api.forecast.solar/estimate/{lat}/{lon}/{dec}/{az}/{kwp}
  dec = panel tilt 0-90° (0=horizontal, 90=vertical)
  az  = azimuth -180..180 from south (0=south, -90=east, 90=west)

Consumption history: in-memory rolling buffer, per-hour-of-week average.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta

_LOGGER = logging.getLogger(__name__)
FORECAST_SOLAR_BASE = "https://api.forecast.solar/estimate"


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

    def __init__(self, max_days: int = 7):
        self._max_days = max_days
        # hour_of_week (0..167) -> list[(epoch_float, watts)]
        self._data: dict[int, list] = defaultdict(list)

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
