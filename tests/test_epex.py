"""Tests for the ENTSO-E XML parser (epex.py)."""
from datetime import datetime, timezone

import epex

_NS = "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:0"
_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="{_NS}">
  <TimeSeries>
    <Period>
      <timeInterval>
        <start>2026-06-17T00:00:00Z</start>
        <end>2026-06-17T02:00:00Z</end>
      </timeInterval>
      <resolution>PT60M</resolution>
      <Point><position>1</position><price.amount>50.0</price.amount></Point>
      <Point><position>2</position><price.amount>100.0</price.amount></Point>
    </Period>
  </TimeSeries>
</Publication_MarketDocument>"""


def test_parse_extracts_prices_and_stats():
    now = datetime(2026, 6, 17, 0, 30, tzinfo=timezone.utc)
    data = epex._parse(_XML, now)
    assert len(data["prices_today"]) == 2
    # 50 EUR/MWh -> 0.05 EUR/kWh
    assert data["prices_today"][0]["price_eur_kwh"] == 0.05
    assert data["today_min"] == 0.05
    assert data["today_max"] == 0.10
    assert data["slot_minutes"] == 60


def test_parse_current_and_next_slot():
    now = datetime(2026, 6, 17, 0, 30, tzinfo=timezone.utc)
    data = epex._parse(_XML, now)
    assert data["current_price"] == 0.05   # 00:00-01:00 slot
    assert data["next_slot_price"] == 0.10  # 01:00-02:00 slot


def test_parse_empty_xml_returns_empty_dict():
    assert epex._parse("<nonsense/>", datetime.now(timezone.utc)) == {}


def test_resolve_zone():
    assert epex.resolve_zone("BE") == "10YBE----------2"
    # Unknown short code is passed through unchanged (assume raw EIC).
    assert epex.resolve_zone("10YXYZ") == "10YXYZ"
