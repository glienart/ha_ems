"""
Solar production forecast, consumption history and solar self-calibration for HA EMS.

Solar forecast: Forecast.Solar API (free, no API key required).
  GET https://api.forecast.solar/estimate/{lat}/{lon}/{dec}/{az}/{kwp}
  dec = panel tilt 0-90° (0=horizontal, 90=vertical)
  az  = azimuth -180..180 from south (0=south, -90=east, 90=west)

Consumption history: in-memory rolling buffer, per-hour-of-week average.
"""
from __future__ import annotations

import json
import logging
import math
import os
from collections import defaultdict
from datetime import datetime, timedelta

_LOGGER = logging.getLogger(__name__)
FORECAST_SOLAR_BASE = "https://api.forecast.solar/estimate"
CONSUMPTION_PATH = "/data/consumption_history.json"
SOLAR_CALIB_PATH = "/data/solar_calibration.json"
CALIB_ALPHA = 0.15        # EMA learning rate (slow, robust to noisy days)
CALIB_MIN_FC_W = 100      # ignore hours where the forecast is essentially night
CALIB_MIN_RATIO = 0.2     # clamp the per-hour correction to a sane band
CALIB_MAX_RATIO = 3.0
# Only update the long-term per-hour factors (structural shading) when today
# looks like a clear-sky day.  Cloudy days have a low weather residual that
# would contaminate the shading signal, so we skip the EMA update for them.
CALIB_CLEAR_SKY_MIN = 0.80  # residual >= this → day is clear enough to learn from
# Intra-day (weather) residual: how today's real production compares to what
# the calibrated forecast predicted for the daylight hours already elapsed.
# Applied to the remaining hours of *today* so a cloudy/sunny day is tracked
# in real time instead of waiting for the slow per-hour EMA to catch up.
TODAY_MIN_EXP_WH = 300.0  # need some meaningful elapsed daylight before trusting it
TODAY_MIN_RATIO = 0.3
TODAY_MAX_RATIO = 1.8


def solar_window(lat: float, lon: float, dt: "datetime | None" = None) -> tuple[float, float]:
    """Return (sunrise_hour, sunset_hour) in local *wall-clock* hours for `dt`
    (defaults to today) using the standard approximate formula (±5 min accuracy).

    Both values are in [0, 24).  Returns (6.0, 20.0) if lat/lon are not set.
    """
    if lat == 0.0 and lon == 0.0:
        return 6.0, 20.0
    if dt is None:
        from datetime import date
        dt = datetime.combine(date.today(), datetime.min.time())
    # Day-of-year
    n = dt.timetuple().tm_yday
    # Declination (degrees)
    decl = 23.45 * math.sin(math.radians((360 / 365) * (n - 81)))
    # Hour-angle at sunrise (degrees) — standard formula
    cos_ha = -(math.tan(math.radians(lat)) * math.tan(math.radians(decl)))
    cos_ha = max(-1.0, min(1.0, cos_ha))   # clamp for polar edge cases
    ha = math.degrees(math.acos(cos_ha))   # in [0, 180]
    # UTC hours of sunrise/sunset
    sr_utc = 12.0 - ha / 15.0
    ss_utc = 12.0 + ha / 15.0
    # Approximate local offset via longitude (UTC+lon/15), ignoring DST.
    # Good enough for the purpose of "don't call the API at 03:00".
    tz_offset = lon / 15.0
    return sr_utc + tz_offset, ss_utc + tz_offset


def is_daylight(lat: float, lon: float, dt: "datetime | None" = None, margin_h: float = 0.5) -> bool:
    """True if `dt` (default: now) is within the solar window (with `margin_h` buffer)."""
    if dt is None:
        dt = datetime.now()
    sr, ss = solar_window(lat, lon, dt)
    h = dt.hour + dt.minute / 60.0
    return (sr - margin_h) <= h <= (ss + margin_h)


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

    def forecast_day_kwh(self, d: datetime) -> float:
        """Total forecast house consumption (kWh) for the calendar day of `d`,
        summing the per-hour-of-week averages over its 24 hours.

        Used to record a stable full-day consumption forecast for the
        "Réel vs Prévisionnel" comparison chart.
        """
        total_w = 0.0
        for h in range(24):
            key = d.weekday() * 24 + h
            readings = self._data.get(key, [])
            total_w += sum(w for _, w in readings) / len(readings) if readings else 500.0
        return round(total_w / 1000.0, 3)

    @property
    def has_enough_data(self) -> bool:
        """True once we have at least ~100 readings (roughly 1–2 h of data)."""
        return sum(len(v) for v in self._data.values()) > 100


class SolarCalibration:
    """Learns a per-(hour-of-day, month) correction factor between the generic
    Forecast.Solar prediction and *this house's* actual PV production.

    Using 24 × 12 = 288 cells instead of 24 captures two real-world effects:

    1. **Sun position**: the solar elevation angle at, say, 08:00 is ~10° in
       December and ~35° in June at 51 °N.  A roof ridge or neighbour that
       blocks the panel at low angles causes no shading in summer.

    2. **Deciduous trees**: a large tree to the south-east can cut production
       by 60 % in August (full foliage) while barely affecting February.

    The EMA (alpha=0.15, ~15–20 clear days per cell to converge) is updated
    only on clear-sky days (weather residual ≥ CALIB_CLEAR_SKY_MIN) so clouds
    don't contaminate the structural shading signal.
    """

    # JSON key format for a (hour, month) cell: "HH-MM" e.g. "08-06"
    @staticmethod
    def _key(hour: int, month: int) -> str:
        return f"{hour:02d}-{month:02d}"

    @staticmethod
    def _parse_key(k: str) -> tuple[int, int] | None:
        parts = k.split("-")
        if len(parts) == 2:
            try:
                return int(parts[0]), int(parts[1])
            except ValueError:
                pass
        return None

    def __init__(self, path: str = SOLAR_CALIB_PATH, alpha: float = CALIB_ALPHA):
        self._path = path
        self._alpha = alpha
        self._factors: dict[str, float] = {}   # "HH-MM" -> factor
        self._cur = {"hour": None, "month": None, "date": None, "sum": 0.0, "n": 0, "fc": 0.0}
        self._today = {"date": None, "act": 0.0, "exp": 0.0}
        self._load()

    def _load(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        try:
            with open(self._path) as f:
                raw = json.load(f)
            raw_factors = raw.get("factors", {})
            # Migration: old format used plain int keys "0"–"23" (hour only).
            # Spread each old factor across all 12 months as a starting point.
            migrated = 0
            for k, v in raw_factors.items():
                if "-" in str(k):
                    self._factors[str(k)] = float(v)
                else:
                    try:
                        h = int(k)
                        for m in range(1, 13):
                            cell = self._key(h, m)
                            if cell not in self._factors:
                                self._factors[cell] = float(v)
                                migrated += 1
                    except ValueError:
                        pass
            td = raw.get("today")
            if isinstance(td, dict) and td.get("date"):
                self._today = {"date": td["date"], "act": float(td.get("act", 0.0)),
                               "exp": float(td.get("exp", 0.0))}
            cells = len(self._factors)
            if migrated:
                _LOGGER.info("Solar calibration migrated %d old-format factors → %d cells", migrated, cells)
            else:
                _LOGGER.info("Solar calibration loaded: %d cells (hour×month)", cells)
        except Exception as exc:
            _LOGGER.error("Failed to load solar calibration: %s", exc)

    def save(self) -> None:
        if not self._path:
            return
        try:
            with open(self._path, "w") as f:
                json.dump({"factors": self._factors, "today": self._today}, f)
        except Exception as exc:
            _LOGGER.error("Failed to save solar calibration: %s", exc)

    def factor(self, hour: int, month: int) -> float:
        return self._factors.get(self._key(hour, month), 1.0)

    def observe(self, now: datetime, actual_w, forecast_w) -> None:
        """Feed one measurement; measurements are rolled up per clock hour."""
        if actual_w is None:
            return
        hour = now.hour
        month = now.month
        date = now.date().isoformat()
        cur_slot = (self._cur["hour"], self._cur["month"])
        if self._cur["hour"] is None:
            self._cur = {"hour": hour, "month": month, "date": date, "sum": 0.0, "n": 0, "fc": forecast_w or 0.0}
        elif cur_slot != (hour, month):
            self._finalize()
            self._cur = {"hour": hour, "month": month, "date": date, "sum": 0.0, "n": 0, "fc": forecast_w or 0.0}
        self._cur["sum"] += max(0.0, actual_w)
        self._cur["n"] += 1
        if forecast_w:
            self._cur["fc"] = forecast_w

    def _finalize(self) -> None:
        c = self._cur
        if c["n"] <= 0 or c["fc"] < CALIB_MIN_FC_W:
            return
        avg = c["sum"] / c["n"]
        cell = self._key(c["hour"], c["month"])
        is_new = cell not in self._factors
        old = self._factors.get(cell, 1.0)
        # Always feed the intra-day weather residual (any weather).
        self._accumulate_today(c.get("date"), avg, c["fc"] * old)
        ratio = max(CALIB_MIN_RATIO, min(CALIB_MAX_RATIO, avg / c["fc"]))
        # Bootstrap: a cell that has never been learned starts from the observed
        # ratio on its first valid day, regardless of the weather. Without this a
        # house whose whole-day production sits below the clear-sky threshold
        # (heavy shading, soiling, bad orientation) would read as "cloudy" every
        # single day and stay stuck at 1.0 forever. Once the cell holds the
        # structural factor, the clear-sky residual normalises back to ~1 and the
        # gate below correctly rejects genuinely cloudy days.
        if is_new:
            self._factors[cell] = round(ratio, 4)
            return
        # Refine the long-term structural shading factor ONLY on clear-sky days.
        residual = self.today_residual(c.get("date"))
        if residual is not None and residual < CALIB_CLEAR_SKY_MIN:
            _LOGGER.debug(
                "Calib %s skipped (cloudy day, residual=%.2f)", cell, residual
            )
            return
        self._factors[cell] = round((1 - self._alpha) * old + self._alpha * ratio, 4)

    def _accumulate_today(self, date_str, actual_w: float, expected_w: float) -> None:
        if not date_str or expected_w <= 0:
            return
        if self._today.get("date") != date_str:
            self._today = {"date": date_str, "act": 0.0, "exp": 0.0}
        self._today["act"] += max(0.0, actual_w)
        self._today["exp"] += expected_w

    def today_residual(self, date_str) -> float | None:
        """Weather factor for `date_str` (≈1 clear, <1 cloudy, >1 unusually sunny).
        Returns None until enough daylight hours have elapsed."""
        t = self._today
        if not date_str or t.get("date") != date_str or t["exp"] < TODAY_MIN_EXP_WH:
            return None
        return max(TODAY_MIN_RATIO, min(TODAY_MAX_RATIO, t["act"] / t["exp"]))

    def apply(self, forecast_dict: dict, now: datetime | None = None) -> dict:
        """Return a calibrated copy of {hour_key: watts}.

        Long-term (hour × month) shading factors + intra-day weather residual.
        The residual is applied only to remaining hours of today so a cloudy
        morning immediately reshapes the rest of today's curve.
        """
        today_str = now.date().isoformat() if now else None
        residual = self.today_residual(today_str) if today_str else None
        out = {}
        for key, w in forecast_dict.items():
            try:
                hour = int(key[11:13])
                # Parse month from the date part of the key (YYYY-MM-DDTHH:mm)
                month = int(key[5:7])
            except (ValueError, IndexError):
                out[key] = round(w, 1)
                continue
            f = self.factor(hour, month)
            val = w * f
            if (residual is not None and now is not None
                    and key[:10] == today_str and hour >= now.hour):
                val *= residual
            out[key] = round(val, 1)
        return out

    @property
    def today_factor(self) -> float | None:
        return self.today_residual(datetime.now().date().isoformat())

    @property
    def cells_learned(self) -> int:
        """Number of (hour, month) cells that have been updated at least once."""
        return len(self._factors)

    @property
    def hours_learned(self) -> int:
        """Distinct hours that have at least one month cell learned."""
        return len({self._parse_key(k)[0] for k in self._factors if self._parse_key(k)})

    @property
    def mean_factor(self) -> float:
        if not self._factors:
            return 1.0
        return round(sum(self._factors.values()) / len(self._factors), 3)
