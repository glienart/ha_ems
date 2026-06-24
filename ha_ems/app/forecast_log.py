"""
Daily forecast snapshot logger — persists, per calendar day, the predicted
solar production and house consumption (kWh) so the "Réel vs Prévisionnel"
chart can compare actuals against the forecast over an arbitrary date range.

The live 24h plan is a rolling window and is not retained; this log records a
stable *full calendar-day* forecast total each rebuild cycle (overwriting the
day with the latest estimate). Only days seen on/after this feature shipped
have forecast data — older days simply have no forecast point.

Bucket key format: "YYYY-MM-DD"
Each bucket: { solar_kwh, house_kwh }
"""
from __future__ import annotations

import json
import logging
import os
import time

_LOGGER = logging.getLogger(__name__)

FORECAST_LOG_PATH = "/data/forecast_log.json"
MAX_DAYS = 365 * 3          # ~3 years of daily forecast snapshots
SAVE_INTERVAL_S = 300       # write to disk at most once every 5 min


class ForecastLog:
    def __init__(self, path: str = FORECAST_LOG_PATH) -> None:
        self._path = path
        self._data: dict[str, dict] = {}   # {"YYYY-MM-DD": {"solar_kwh", "house_kwh"}}
        self._dirty = False
        self._last_save = 0.0
        self._load()

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        try:
            with open(self._path) as f:
                self._data = json.load(f)
            _LOGGER.info("Forecast log loaded: %d daily snapshots", len(self._data))
        except Exception as exc:
            _LOGGER.error("Failed to load forecast log: %s", exc)
            self._data = {}

    def _save(self) -> None:
        if not self._path:
            return
        # Trim oldest snapshots if over limit
        if len(self._data) > MAX_DAYS:
            for key in sorted(self._data)[: len(self._data) - MAX_DAYS]:
                del self._data[key]
        try:
            with open(self._path, "w") as f:
                json.dump(self._data, f)
            self._dirty = False
            self._last_save = time.monotonic()
        except Exception as exc:
            _LOGGER.error("Failed to save forecast log: %s", exc)

    def _maybe_save(self) -> None:
        if time.monotonic() - self._last_save >= SAVE_INTERVAL_S:
            self._save()

    def flush(self) -> None:
        if self._dirty:
            self._save()

    # ── Recording ──────────────────────────────────────────────────────────────

    def update(self, daily: dict) -> None:
        """Overwrite the per-day forecast totals with the latest estimate.

        `daily` maps "YYYY-MM-DD" -> {"solar_kwh": float, "house_kwh": float}.
        """
        for day, rec in daily.items():
            self._data[day] = {
                "solar_kwh": round(float(rec.get("solar_kwh", 0.0)), 3),
                "house_kwh": round(float(rec.get("house_kwh", 0.0)), 3),
            }
        self._dirty = True
        self._maybe_save()

    # ── Lookup ─────────────────────────────────────────────────────────────────

    def get_range(self, start: str, end: str) -> dict:
        """Return {date: {"solar_kwh", "house_kwh"}} for dates in [start, end]."""
        if start > end:
            start, end = end, start
        return {d: rec for d, rec in self._data.items() if start <= d <= end}
