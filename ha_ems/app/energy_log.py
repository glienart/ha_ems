"""
Energy cost logger — accumulates kWh imported/exported and cost/revenue
into hourly buckets stored in /data/energy_log.json.

Bucket key format: "YYYY-MM-DDTHH"  (local time)
Each bucket: { kwh_in, kwh_out, cost, revenue }
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import datetime
from typing import Optional

_LOGGER = logging.getLogger(__name__)

LOG_PATH = "/data/energy_log.json"
MAX_BUCKETS = 24 * 365 * 3  # ~3 years of hourly data


class EnergyLogger:
    def __init__(self) -> None:
        self._data: dict[str, dict] = {}
        self._dirty = False
        self._load()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        if os.path.exists(LOG_PATH):
            try:
                with open(LOG_PATH) as f:
                    self._data = json.load(f)
                _LOGGER.info("Energy log loaded: %d hourly buckets", len(self._data))
            except Exception as exc:
                _LOGGER.error("Failed to load energy log: %s", exc)
                self._data = {}

    def _save(self) -> None:
        # Trim oldest buckets if over limit
        if len(self._data) > MAX_BUCKETS:
            for key in sorted(self._data)[: len(self._data) - MAX_BUCKETS]:
                del self._data[key]
        try:
            with open(LOG_PATH, "w") as f:
                json.dump(self._data, f)
        except Exception as exc:
            _LOGGER.error("Failed to save energy log: %s", exc)

    # ── Recording ─────────────────────────────────────────────────────────────

    def record(
        self,
        grid_w: Optional[float],
        tariff_consumption: Optional[float],
        tariff_injection: Optional[float],
        interval_s: int,
        house_w: Optional[float] = None,
    ) -> None:
        """Accumulate one EMS tick into the current hourly bucket."""
        if grid_w is None:
            return

        key = datetime.now().strftime("%Y-%m-%dT%H")
        b = self._data.setdefault(
            key, {"kwh_in": 0.0, "kwh_out": 0.0, "kwh_house": 0.0, "cost": 0.0, "revenue": 0.0}
        )

        # W × s → kWh
        kwh = abs(grid_w) * interval_s / 3_600_000.0

        if grid_w > 0:
            b["kwh_in"] += kwh
            if tariff_consumption is not None:
                b["cost"] += kwh * tariff_consumption
        else:
            b["kwh_out"] += kwh
            if tariff_injection is not None:
                b["revenue"] += kwh * tariff_injection

        if house_w is not None and house_w > 0:
            b["kwh_house"] += house_w * interval_s / 3_600_000.0

        self._save()

    # ── Aggregation ───────────────────────────────────────────────────────────

    def get_history(self, period: str = "day") -> dict:
        """
        Aggregate hourly buckets into bars.

        period: "today" → today's 24 hourly slots (one bar per hour)
                "day"   → last 30 days  (one bar per day)
                "week"  → last 13 weeks (one bar per ISO week)
                "month" → last 12 months
                "year"  → last 5 years
        """
        agg: dict[str, dict] = defaultdict(
            lambda: {"kwh_in": 0.0, "kwh_out": 0.0, "kwh_house": 0.0, "cost": 0.0, "revenue": 0.0}
        )

        today_date = datetime.now().strftime("%Y-%m-%d")

        for key, bucket in self._data.items():
            # key: "YYYY-MM-DDTHH"
            date_part = key[:10]  # "YYYY-MM-DD"
            if period == "today":
                if date_part != today_date:
                    continue
                agg_key = key[11:13] + ":00"  # "HH:00"
            elif period == "day":
                agg_key = date_part
            elif period == "week":
                dt = datetime.strptime(date_part, "%Y-%m-%d")
                iso = dt.isocalendar()
                agg_key = f"{iso[0]}-W{iso[1]:02d}"
            elif period == "month":
                agg_key = key[:7]  # "YYYY-MM"
            else:  # year
                agg_key = key[:4]  # "YYYY"

            for k in ("kwh_in", "kwh_out", "kwh_house", "cost", "revenue"):
                agg[agg_key][k] += bucket.get(k, 0.0)

        limits = {"today": 24, "day": 30, "week": 13, "month": 12, "year": 5}
        sorted_keys = sorted(agg)[-limits.get(period, 30):]

        items = []
        for k in sorted_keys:
            d = agg[k]
            items.append(
                {
                    "label": k,
                    "kwh_in":    round(d["kwh_in"],    3),
                    "kwh_out":   round(d["kwh_out"],   3),
                    "kwh_house": round(d["kwh_house"], 3),
                    "cost":      round(d["cost"],      4),
                    "revenue":   round(d["revenue"],   4),
                    "net_cost":  round(d["cost"] - d["revenue"], 4),
                }
            )

        totals = {
            "kwh_in":    round(sum(i["kwh_in"]    for i in items), 3),
            "kwh_out":   round(sum(i["kwh_out"]   for i in items), 3),
            "kwh_house": round(sum(i["kwh_house"] for i in items), 3),
            "cost":      round(sum(i["cost"]      for i in items), 4),
            "revenue":   round(sum(i["revenue"]   for i in items), 4),
        }
        totals["net_cost"] = round(totals["cost"] - totals["revenue"], 4)

        return {"period": period, "items": items, "totals": totals}
