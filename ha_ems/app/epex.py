"""EPEX SPOT price fetcher via ENTSO-E Transparency Platform."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import httpx

_LOGGER = logging.getLogger(__name__)
ENTSOE_URL = "https://web-api.tp.entsoe.eu/api"

EPEX_ZONES: dict[str, str] = {
    "BE":     "10YBE----------2",
    "FR":     "10YFR-RTE------C",
    "DE-LU":  "10Y1001A1001A63L",
    "NL":     "10YNL----------L",
    "AT":     "10YAT-APG------L",
    "CH":     "10YCH-SWISSGRID4",
    "ES":     "10YES-REE------0",
    "PT":     "10YPT-REN------W",
    "IT-N":   "10Y1001A1001A73I",
    "DK1":    "10YDK-1--------W",
    "DK2":    "10YDK-2--------M",
    "SE3":    "10Y1001A1001A46L",
    "NO2":    "10YNO-2--------T",
    "FI":     "10YFI-1--------U",
    "PL":     "10YPL-AREA-----S",
    "CZ":     "10YCZ-CEPS-----N",
}


def resolve_zone(zone: str) -> str:
    """Accept either a short code ('BE') or a raw EIC — return EIC."""
    return EPEX_ZONES.get(zone, zone)


async def fetch_prices(zone_eic: str, token: str) -> dict:
    """
    Fetch day-ahead prices from ENTSO-E and return a structured dict:
        current_price       float | None   €/kWh
        next_slot_price     float | None
        today_min/max/avg   float | None
        tomorrow_min/max/avg float | None  (None until ~13:00 CET)
        prices_today        list[{start, end, price_eur_kwh}]
        prices_tomorrow     list[...]
        slot_minutes        int
    """
    now = datetime.now(timezone.utc)
    period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    period_end   = period_start + timedelta(days=2)
    fmt = "%Y%m%d%H%M"

    params = {
        "securityToken": token,
        "documentType":  "A44",
        "in_Domain":     zone_eic,
        "out_Domain":    zone_eic,
        "periodStart":   period_start.strftime(fmt),
        "periodEnd":     period_end.strftime(fmt),
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(ENTSOE_URL, params=params)
            if r.status_code != 200:
                _LOGGER.error("ENTSO-E HTTP %s: %s", r.status_code, r.text[:200])
                return {}
            xml_text = r.text
    except Exception as exc:
        _LOGGER.error("EPEX fetch error: %s", exc)
        return {}

    return _parse(xml_text, now)


def _parse(xml_text: str, now: datetime) -> dict:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        _LOGGER.error("EPEX XML parse error: %s", exc)
        return {}

    tag    = root.tag
    ns_uri = tag[1: tag.index("}")] if tag.startswith("{") else ""
    ns     = {"ns": ns_uri} if ns_uri else {}

    def _findall(el, path):
        return el.findall(path, ns) if ns else el.findall(path.replace("ns:", ""))

    def _find(el, path):
        return el.find(path, ns) if ns else el.find(path.replace("ns:", ""))

    prices: list[dict] = []

    for ts in _findall(root, ".//ns:TimeSeries"):
        period_el = _find(ts, "ns:Period")
        if period_el is None:
            continue

        res_el  = _find(period_el, "ns:resolution")
        res_str = res_el.text if res_el is not None else "PT60M"
        slot_min = 15 if "15" in res_str else (30 if "30" in res_str else 60)

        start_el = _find(period_el, "ns:timeInterval/ns:start")
        if start_el is None:
            continue
        try:
            period_start = datetime.fromisoformat(start_el.text.replace("Z", "+00:00"))
        except ValueError:
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
            prices.append({
                "start":         slot_start,
                "end":           slot_start + timedelta(minutes=slot_min),
                "price_eur_kwh": round(price_mwh / 1000, 6),
                "slot_min":      slot_min,
            })

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
        _LOGGER.warning("EPEX: no prices returned — check token and zone")
        return {}

    now_utc    = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    today_d    = now_utc.date()
    tmrw_d     = (now_utc + timedelta(days=1)).date()

    today_p  = [p for p in prices if p["start"].date() == today_d]
    tmrw_p   = [p for p in prices if p["start"].date() == tmrw_d]
    current  = next((p for p in prices if p["start"] <= now_utc < p["end"]), None)
    nxt      = next((p for p in prices if p["start"] > now_utc),             None)

    def _stats(slots):
        if not slots:
            return None, None, None
        vals = [s["price_eur_kwh"] for s in slots]
        return min(vals), max(vals), round(sum(vals) / len(vals), 6)

    def _ser(slots):
        return [{"start": s["start"].isoformat(), "end": s["end"].isoformat(), "price_eur_kwh": s["price_eur_kwh"]} for s in slots]

    t_min,  t_max,  t_avg  = _stats(today_p)
    tm_min, tm_max, tm_avg = _stats(tmrw_p)

    return {
        "current_price":    current["price_eur_kwh"] if current else None,
        "next_slot_price":  nxt["price_eur_kwh"]     if nxt     else None,
        "today_min":        t_min,
        "today_max":        t_max,
        "today_avg":        t_avg,
        "tomorrow_min":     tm_min,
        "tomorrow_max":     tm_max,
        "tomorrow_avg":     tm_avg,
        "prices_today":     _ser(today_p),
        "prices_tomorrow":  _ser(tmrw_p),
        "slot_minutes":     prices[0]["slot_min"] if prices else 60,
    }