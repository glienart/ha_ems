"""EPEX SPOT price coordinator — fetches day-ahead prices via ENTSO-E."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

ENTSOE_URL = "https://web-api.tp.entsoe.eu/api"


class EpexCoordinator(DataUpdateCoordinator):
    """
    Polls ENTSO-E Transparency Platform for EPEX SPOT day-ahead prices.

    Prices are in €/kWh (converted from the API's €/MWh).
    Data dict keys:
        current_price       float | None  — active 15-min slot price
        next_slot_price     float | None  — next slot price
        today_min / max / avg
        tomorrow_min / max / avg        (None until ~13:00 CET)
        prices_today        list[dict]   — [{start, end, price_eur_kwh}, ...]
        prices_tomorrow     list[dict]
        zone                str          — bidding zone EIC code
        slot_minutes        int          — resolution (15, 30, or 60)
    """

    def __init__(self, hass: HomeAssistant, zone: str, token: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_epex",
            update_interval=timedelta(minutes=15),
        )
        self._zone = zone
        self._token = token
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _async_update_data(self) -> dict:
        now = datetime.now(timezone.utc)

        # Always fetch today + tomorrow (tomorrow available ~13:00 CET)
        period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        period_end   = period_start + timedelta(days=2)
        fmt = "%Y%m%d%H%M"

        params = {
            "securityToken": self._token,
            "documentType":  "A44",   # day-ahead prices
            "in_Domain":     self._zone,
            "out_Domain":    self._zone,
            "periodStart":   period_start.strftime(fmt),
            "periodEnd":     period_end.strftime(fmt),
        }

        session = await self._get_session()
        try:
            async with session.get(
                ENTSOE_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                xml_text = await resp.text()
                if resp.status != 200:
                    raise UpdateFailed(
                        f"ENTSO-E returned HTTP {resp.status}: {xml_text[:300]}"
                    )
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Network error: {err}") from err

        return self._parse(xml_text, now)

    # ------------------------------------------------------------------ #
    # XML parser                                                           #
    # ------------------------------------------------------------------ #

    def _parse(self, xml_text: str, now: datetime) -> dict:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as err:
            raise UpdateFailed(f"Cannot parse ENTSO-E XML: {err}") from err

        # Detect namespace from root tag
        tag = root.tag
        ns_uri = tag[1 : tag.index("}")] if tag.startswith("{") else ""
        ns = {"ns": ns_uri} if ns_uri else {}

        def _findall(el, path):
            if ns:
                return el.findall(path, ns)
            return el.findall(path.replace("ns:", ""))

        def _find(el, path):
            if ns:
                return el.find(path, ns)
            return el.find(path.replace("ns:", ""))

        prices: list[dict] = []

        for ts in _findall(root, ".//ns:TimeSeries"):
            period_el = _find(ts, "ns:Period")
            if period_el is None:
                continue

            # Resolution → slot duration in minutes
            res_el = _find(period_el, "ns:resolution")
            res_str = res_el.text if res_el is not None else "PT60M"
            if "15" in res_str:
                slot_min = 15
            elif "30" in res_str:
                slot_min = 30
            else:
                slot_min = 60

            # Period start
            start_el = _find(period_el, "ns:timeInterval/ns:start")
            if start_el is None:
                continue
            try:
                period_start = datetime.fromisoformat(
                    start_el.text.replace("Z", "+00:00")
                )
            except ValueError:
                _LOGGER.warning("Cannot parse period start: %s", start_el.text)
                continue

            for point in _findall(period_el, "ns:Point"):
                pos_el   = _find(point, "ns:position")
                price_el = _find(point, "ns:price.amount")
                if pos_el is None or price_el is None:
                    continue
                try:
                    pos       = int(pos_el.text)
                    price_mwh = float(price_el.text)
                except (ValueError, TypeError):
                    continue

                slot_start = period_start + timedelta(minutes=slot_min * (pos - 1))
                slot_end   = slot_start   + timedelta(minutes=slot_min)
                prices.append(
                    {
                        "start":        slot_start,
                        "end":          slot_end,
                        "price_eur_kwh": round(price_mwh / 1000, 6),
                        "slot_min":     slot_min,
                    }
                )

        # Sort + deduplicate
        prices.sort(key=lambda x: x["start"])
        seen: set[str] = set()
        unique: list[dict] = []
        for p in prices:
            k = p["start"].isoformat()
            if k not in seen:
                seen.add(k)
                unique.append(p)
        prices = unique

        if not prices:
            _LOGGER.warning(
                "EPEX: no price data returned for zone %s. "
                "Check your ENTSO-E token and zone code.",
                self._zone,
            )

        # Helpers
        now_utc     = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        today_date  = now_utc.date()
        tmrw_date   = (now_utc + timedelta(days=1)).date()

        today_prices   = [p for p in prices if p["start"].date() == today_date]
        tmrw_prices    = [p for p in prices if p["start"].date() == tmrw_date]

        current   = next((p for p in prices if p["start"] <= now_utc < p["end"]), None)
        next_slot = next((p for p in prices if p["start"] > now_utc),             None)

        def _stats(slots):
            if not slots:
                return None, None, None
            vals = [s["price_eur_kwh"] for s in slots]
            return min(vals), max(vals), round(sum(vals) / len(vals), 6)

        t_min,  t_max,  t_avg  = _stats(today_prices)
        tm_min, tm_max, tm_avg = _stats(tmrw_prices)

        def _serialize(slots):
            return [
                {
                    "start":         s["start"].isoformat(),
                    "end":           s["end"].isoformat(),
                    "price_eur_kwh": s["price_eur_kwh"],
                }
                for s in slots
            ]

        slot_min = prices[0]["slot_min"] if prices else 60

        return {
            "current_price":    current["price_eur_kwh"]   if current   else None,
            "next_slot_price":  next_slot["price_eur_kwh"] if next_slot else None,
            "today_min":        t_min,
            "today_max":        t_max,
            "today_avg":        t_avg,
            "tomorrow_min":     tm_min,
            "tomorrow_max":     tm_max,
            "tomorrow_avg":     tm_avg,
            "prices_today":     _serialize(today_prices),
            "prices_tomorrow":  _serialize(tmrw_prices),
            "zone":             self._zone,
            "slot_minutes":     slot_min,
            "updated_at":       now_utc.isoformat(),
        }
