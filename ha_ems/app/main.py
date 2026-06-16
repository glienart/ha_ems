"""
HA EMS Add-on -- FastAPI application.

Endpoints:
  GET  /           -> dashboard HTML
  GET  /api/state  -> current EMS state (JSON)
  GET  /api/settings -> current settings (JSON)
  POST /api/settings -> update settings
  POST /api/mode   -> change EMS mode quickly
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import ha_client, settings as settings_module
from .energy_html import ENERGY_HTML
from .epex import fetch_prices, resolve_zone
from .optimizer import EmsOptimizer, EmsSnapshot
from .settings import EmsSettings

logging.basicConfig(level=logging.INFO)
_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_settings: EmsSettings = settings_module.load()
_optimizer = EmsOptimizer()
_last_state: dict = {}
_loop_task: Optional[asyncio.Task] = None
_epex_data: dict = {}          # latest EPEX price data
_epex_task: Optional[asyncio.Task] = None


# ---------------------------------------------------------------------------
# Lifespan -- start/stop background loop
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _loop_task, _epex_task
    _loop_task = asyncio.create_task(ems_loop())
    _LOGGER.info("EMS optimizer loop started")
    if _settings.epex_token:
        _epex_task = asyncio.create_task(epex_loop())
        _LOGGER.info("EPEX price loop started (zone %s)", _settings.epex_zone)
    yield
    if _loop_task:
        _loop_task.cancel()
    if _epex_task:
        _epex_task.cancel()
    _LOGGER.info("EMS loops stopped")


app = FastAPI(title="HA EMS", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Optimizer loop
# ---------------------------------------------------------------------------

async def ems_loop():
    """Run the optimizer every update_interval seconds."""
    while True:
        try:
            await run_optimizer()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            _LOGGER.error("Optimizer loop error: %s", exc)
        await asyncio.sleep(_settings.update_interval)


async def epex_loop():
    """Fetch EPEX prices every 15 minutes and publish to HA."""
    global _epex_data
    while True:
        try:
            data = await fetch_prices(resolve_zone(_settings.epex_zone), _settings.epex_token)
            if data:
                _epex_data = data
                await _publish_epex(data)
                _LOGGER.info(
                    "EPEX price updated: %.4f EUR/kWh (zone %s)",
                    data.get("current_price") or 0,
                    _settings.epex_zone,
                )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            _LOGGER.error("EPEX loop error: %s", exc)
        await asyncio.sleep(15 * 60)  # refresh every 15 min


async def _publish_epex(data: dict) -> None:
    """Push EPEX prices as virtual sensors to HA."""
    def _fmt(v):
        return str(round(v, 4)) if v is not None else "unavailable"

    sensors = {
        "sensor.ha_ems_epex_current_price":   (data.get("current_price"),   "EPEX Current Price",    "EUR/kWh"),
        "sensor.ha_ems_epex_next_slot_price":  (data.get("next_slot_price"), "EPEX Next Slot Price",  "EUR/kWh"),
        "sensor.ha_ems_epex_today_min":        (data.get("today_min"),       "EPEX Today Min",        "EUR/kWh"),
        "sensor.ha_ems_epex_today_max":        (data.get("today_max"),       "EPEX Today Max",        "EUR/kWh"),
        "sensor.ha_ems_epex_today_avg":        (data.get("today_avg"),       "EPEX Today Avg",        "EUR/kWh"),
        "sensor.ha_ems_epex_tomorrow_min":     (data.get("tomorrow_min"),    "EPEX Tomorrow Min",     "EUR/kWh"),
        "sensor.ha_ems_epex_tomorrow_max":     (data.get("tomorrow_max"),    "EPEX Tomorrow Max",     "EUR/kWh"),
    }
    for entity_id, (value, name, unit) in sensors.items():
        await ha_client.set_entity_state(
            entity_id,
            _fmt(value),
            {
                "friendly_name":       name,
                "unit_of_measurement": unit,
                "icon":                "mdi:currency-eur",
                "device_class":        "monetary",
                "state_class":         "measurement",
            },
        )
    # Full schedule as attributes on the current price sensor
    if data.get("prices_today"):
        await ha_client.set_entity_state(
            "sensor.ha_ems_epex_current_price",
            _fmt(data.get("current_price")),
            {
                "friendly_name":       "EPEX Current Price",
                "unit_of_measurement": "EUR/kWh",
                "icon":                "mdi:currency-eur",
                "device_class":        "monetary",
                "state_class":         "measurement",
                "zone":                _settings.epex_zone,
                "slot_minutes":        data.get("slot_minutes"),
                "today_min":           data.get("today_min"),
                "today_max":           data.get("today_max"),
                "today_avg":           data.get("today_avg"),
                "tomorrow_min":        data.get("tomorrow_min"),
                "tomorrow_max":        data.get("tomorrow_max"),
                "prices_today":        data.get("prices_today", []),
                "prices_tomorrow":     data.get("prices_tomorrow", []),
            },
        )


async def run_optimizer():
    global _last_state
    s = _settings

    # Read HA states
    solar_w = await ha_client.get_float(s.solar_power_sensor)
    grid_w = await ha_client.get_float(s.grid_power_sensor)
    house_w = await ha_client.get_float(s.house_power_sensor) if s.house_power_sensor else None
    bat_soc   = await ha_client.get_float(s.battery_soc_sensor, default=50.0)
    bat_power = await ha_client.get_float(s.battery_power_sensor) if s.battery_power_sensor else None
    ev_soc = await ha_client.get_float(s.ev_soc_sensor) if s.ev_soc_sensor else None
    ev_connected = await ha_client.get_bool(s.ev_charger_switch) if s.ev_charger_switch else False
    tariff = await ha_client.get_float(s.tariff_sensor) if s.tariff_sensor else None
    # Live EPEX price overrides static tariff sensor
    if _epex_data and _epex_data.get("current_price") is not None:
        tariff = _epex_data["current_price"]

    snap = EmsSnapshot(
        solar_power_w=solar_w,
        grid_power_w=grid_w,
        house_power_w=house_w,
        battery_soc_pct=bat_soc,
        battery_min_soc=s.battery_min_soc,
        battery_max_soc=s.battery_max_soc,
        battery_max_charge_w=s.battery_max_charge_w,
        battery_max_discharge_w=s.battery_max_discharge_w,
        ev_connected=ev_connected or (ev_soc is not None),
        ev_soc_pct=ev_soc,
        ev_target_soc=s.ev_target_soc,
        ev_departure_time=s.ev_departure_time,
        ev_max_charge_w=s.ev_max_charge_w,
        tariff_eur_kwh=tariff,
        cheap_threshold=s.cheap_threshold,
        expensive_threshold=s.expensive_threshold,
        mode=s.mode,
        now=datetime.now(),
    )

    decision = _optimizer.decide(snap)

    # Apply battery decision
    bat = decision.battery
    if s.battery_charge_switch:
        if bat == "charge":
            await ha_client.turn_on(s.battery_charge_switch)
        elif bat in ("discharge", "standby", "idle"):
            await ha_client.turn_off(s.battery_charge_switch)
    if s.battery_discharge_switch:
        if bat == "discharge":
            await ha_client.turn_on(s.battery_discharge_switch)
        elif bat in ("charge", "standby", "idle"):
            await ha_client.turn_off(s.battery_discharge_switch)
    if s.battery_standby_switch:
        if bat == "standby":
            await ha_client.turn_on(s.battery_standby_switch)
        else:
            await ha_client.turn_off(s.battery_standby_switch)

    # Apply EV decision
    if s.ev_charger_switch:
        if decision.ev == "charge":
            await ha_client.turn_on(s.ev_charger_switch)
        else:
            await ha_client.turn_off(s.ev_charger_switch)

    # Publish virtual sensors to HA
    await ha_client.set_entity_state(
        "sensor.ha_ems_mode", s.mode,
        {"friendly_name": "EMS Mode", "icon": "mdi:tune"}
    )
    await ha_client.set_entity_state(
        "sensor.ha_ems_battery_decision", bat,
        {"friendly_name": "EMS Battery Decision", "icon": "mdi:battery-charging"}
    )
    await ha_client.set_entity_state(
        "sensor.ha_ems_ev_decision", decision.ev,
        {"friendly_name": "EMS EV Decision", "icon": "mdi:car-electric"}
    )
    await ha_client.set_entity_state(
        "sensor.ha_ems_solar_surplus", str(round(decision.solar_surplus_w)),
        {"friendly_name": "EMS Solar Surplus", "unit_of_measurement": "W", "icon": "mdi:solar-power"}
    )
    await ha_client.set_entity_state(
        "sensor.ha_ems_reason", decision.reason,
        {"friendly_name": "EMS Last Reason"}
    )

    _last_state = {
        "mode": s.mode,
        "battery": bat,
        "ev": decision.ev,
        "solar_surplus_w": round(decision.solar_surplus_w),
        "net_power_w": round(decision.net_power_w),
        "solar_w": round(solar_w),
        "grid_w": round(grid_w),
        "battery_soc": round(bat_soc),
        "ev_soc": round(ev_soc) if ev_soc is not None else None,
        "tariff": tariff,
        "battery_w": round(bat_power) if bat_power is not None else None,
        "epex_price": _epex_data.get("current_price") if _epex_data else None,
        "reason": decision.reason,
        "updated_at": datetime.now().isoformat(),
    }
    _LOGGER.info("EMS: %s", decision.reason)


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.get("/api/state")
async def api_state():
    return JSONResponse(_last_state)


@app.get("/api/epex")
async def api_epex():
    """Return the latest EPEX price data."""
    return JSONResponse(_epex_data or {"error": "No EPEX data — check token and zone in settings"})


@app.get("/api/settings")
async def api_get_settings():
    from dataclasses import asdict
    return JSONResponse(asdict(_settings))


class SettingsUpdate(BaseModel):
    solar_power_sensor: Optional[str] = None
    grid_power_sensor: Optional[str] = None
    house_power_sensor: Optional[str] = None
    battery_soc_sensor: Optional[str] = None
    battery_power_sensor: Optional[str] = None
    battery_charge_switch: Optional[str] = None
    battery_discharge_switch: Optional[str] = None
    battery_standby_switch: Optional[str] = None
    battery_max_charge_w: Optional[int] = None
    battery_max_discharge_w: Optional[int] = None
    battery_min_soc: Optional[int] = None
    battery_max_soc: Optional[int] = None
    ev_charger_switch: Optional[str] = None
    ev_soc_sensor: Optional[str] = None
    ev_target_soc: Optional[int] = None
    ev_departure_time: Optional[str] = None
    ev_max_charge_w: Optional[int] = None
    tariff_sensor: Optional[str] = None
    cheap_threshold: Optional[float] = None
    expensive_threshold: Optional[float] = None
    update_interval: Optional[int] = None


@app.post("/api/settings")
async def api_update_settings(body: SettingsUpdate):
    global _settings
    data = body.model_dump(exclude_none=True)
    for k, v in data.items():
        if hasattr(_settings, k):
            setattr(_settings, k, v)
    settings_module.save_runtime(_settings)
    # Restart loop with new interval
    if _loop_task and "update_interval" in data:
        _loop_task.cancel()
        asyncio.create_task(ems_loop())
    await run_optimizer()
    return JSONResponse({"ok": True})


class ModeUpdate(BaseModel):
    mode: str


@app.post("/api/mode")
async def api_set_mode(body: ModeUpdate):
    global _settings
    valid = {"auto", "eco", "cheap", "manual", "off"}
    if body.mode not in valid:
        return JSONResponse({"error": f"Invalid mode. Use one of: {valid}"}, status_code=400)
    _settings.mode = body.mode
    settings_module.save_runtime(_settings)
    await run_optimizer()
    return JSONResponse({"ok": True, "mode": body.mode})


# ---------------------------------------------------------------------------
# Dashboard UI
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(DASHBOARD_HTML)


@app.get("/energy", response_class=HTMLResponse)
async def energy_dashboard():
    return HTMLResponse(ENERGY_HTML)



@app.get("/api/entities")
async def api_entities(device_class: str = ""):
    """Return all HA entity IDs, optionally filtered by device_class."""
    url = f"{ha_client.HA_API}/states"
    try:
        async with __import__("httpx").AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=ha_client._headers())
            if r.status_code != 200:
                return JSONResponse({"entities": []})
            states = r.json()
        entities = []
        for s in states:
            eid = s.get("entity_id", "")
            attrs = s.get("attributes", {})
            dc = attrs.get("device_class", "")
            if device_class and dc != device_class:
                continue
            entities.append({
                "entity_id": eid,
                "friendly_name": attrs.get("friendly_name", eid),
                "state": s.get("state", ""),
                "device_class": dc,
                "unit": attrs.get("unit_of_measurement", ""),
            })
        entities.sort(key=lambda x: x["entity_id"])
        return JSONResponse({"entities": entities})
    except Exception as exc:
        _LOGGER.error("api_entities error: %s", exc)
        return JSONResponse({"entities": []})


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Home Energy Management System</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root {
    --bg:#f3f4f6;--card:#ffffff;--border:#e5e7eb;
    --text:#111827;--muted:#6b7280;--accent:#10b981;
    --green:#10b981;--yellow:#f59e0b;--red:#ef4444;--blue:#3b82f6;
  }
  @media(prefers-color-scheme:dark){
    :root{--bg:#111827;--card:#1f2937;--border:#374151;--text:#f9fafb;--muted:#9ca3af}
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:system-ui,sans-serif;padding:1rem}
  /* Nav */
  nav{display:flex;justify-content:space-between;align-items:center;margin-bottom:1.25rem;border-bottom:1px solid var(--border);padding-bottom:.75rem}
  .nav-btn{padding:.35rem .85rem;border-radius:.5rem;border:1px solid transparent;background:none;color:var(--muted);cursor:pointer;font-size:.85rem}
  .nav-btn.active{background:var(--card);border-color:var(--border);color:var(--text)}
  .nav-settings{font-size:1.1rem;padding:.25rem .6rem;border:1px solid var(--border)!important;border-radius:.5rem}
  /* Cards */
  .page{display:none}.page.active{display:block}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.75rem;margin-bottom:1rem}
  .card{background:var(--card);border:1px solid var(--border);border-radius:.75rem;padding:1rem}
  .card-label{font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-bottom:.25rem}
  .card-value{font-size:1.5rem;font-weight:700}
  .card-sub{font-size:.75rem;color:var(--muted);margin-top:.25rem}
  .badge{display:inline-block;padding:.2rem .6rem;border-radius:9999px;font-size:.75rem;font-weight:600}
  .badge-green{background:#d1fae5;color:#065f46}
  .badge-yellow{background:#fef3c7;color:#92400e}
  .badge-blue{background:#dbeafe;color:#1e40af}
  .badge-gray{background:var(--border);color:var(--muted)}
  @media(prefers-color-scheme:dark){
    .badge-green{background:#064e3b;color:#10b981}
    .badge-yellow{background:#78350f;color:#f59e0b}
    .badge-blue{background:#1e3a5f;color:#3b82f6}
  }
  .mode-bar{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:1rem}
  .mode-btn{padding:.4rem .9rem;border-radius:.5rem;border:1px solid var(--border);background:var(--card);color:var(--muted);cursor:pointer;font-size:.85rem}
  .mode-btn.active{border-color:var(--green);color:var(--green);background:#d1fae5}
  @media(prefers-color-scheme:dark){.mode-btn.active{background:#064e3b}}
  .reason-card{background:var(--card);border:1px solid var(--border);border-radius:.75rem;padding:1rem;margin-bottom:1rem}
  .reason-card p{font-size:.85rem;color:var(--muted)}
  .section-title{font-size:.8rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-bottom:.5rem;margin-top:.75rem}
  .updated{font-size:.7rem;color:var(--border);text-align:right;margin-top:.5rem}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--green);animation:pulse 2s infinite;display:inline-block}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  h1{font-size:1.2rem;font-weight:600;margin-bottom:1rem;display:flex;align-items:center;gap:.5rem}
  /* Settings */
  .settings-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1rem;margin-bottom:1rem}
  .settings-group{background:var(--card);border:1px solid var(--border);border-radius:.75rem;padding:1rem}
  .sg-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:.75rem}
  .sg-head h3{font-size:.8rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
  .pencil-btn{background:none;border:1px solid var(--border);cursor:pointer;color:var(--muted);font-size:.9rem;padding:.15rem .45rem;border-radius:.35rem;line-height:1}
  .pencil-btn:hover{background:var(--border);color:var(--text)}
  .sg-row{display:flex;justify-content:space-between;align-items:center;padding:.3rem 0;border-bottom:1px solid var(--border);font-size:.8rem}
  .sg-row:last-child{border-bottom:none}
  .sg-key{color:var(--muted)}
  .sg-val{color:var(--text);font-weight:500;max-width:60%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:right}
  .sg-edit{margin-top:.5rem;padding-top:.5rem;border-top:1px solid var(--border)}
  /* Combo search */
  .combo{position:relative}
  .combo-input{width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:.4rem .6rem;border-radius:.4rem;font-size:.82rem}
  .combo-input:focus{outline:none;border-color:var(--accent)}
  .combo-list{position:absolute;top:100%;left:0;right:0;background:var(--card);border:1px solid var(--border);border-radius:.4rem;max-height:190px;overflow-y:auto;z-index:300;display:none;list-style:none;padding:0;margin:2px 0 0;box-shadow:0 4px 16px rgba(0,0,0,.15)}
  .combo-list li{padding:.3rem .5rem;cursor:pointer;display:flex;align-items:center;gap:.4rem;font-size:.78rem;border-bottom:1px solid var(--border)}
  .combo-list li:last-child{border-bottom:none}
  .combo-list li:hover,.combo-list li.hl{background:var(--border)}
  .cl-name{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text)}
  .cl-unit{font-size:.64rem;padding:.1rem .3rem;border-radius:.25rem;background:var(--border);color:var(--muted);white-space:nowrap;flex-shrink:0}
  .cl-none{color:var(--muted);font-style:italic}
  .field{margin-bottom:.6rem}
  .field label{display:block;font-size:.78rem;color:var(--muted);margin-bottom:.2rem}
  .field select,.field input{width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:.4rem .6rem;border-radius:.4rem;font-size:.82rem}
  .field select:focus,.field input:focus{outline:none;border-color:var(--accent)}
  .save-btn{background:var(--accent);color:#fff;border:none;padding:.4rem 1rem;border-radius:.5rem;cursor:pointer;font-size:.82rem;font-weight:600;margin-top:.25rem}
  .save-btn:hover{opacity:.9}
  .toast{position:fixed;bottom:1rem;right:1rem;background:var(--accent);color:#fff;padding:.5rem 1rem;border-radius:.5rem;font-size:.85rem;display:none;z-index:999}
  /* Energy — HA-style distribution card */
  .ha-e-wrap{position:relative;height:290px;max-width:500px;margin:0 auto .75rem}
  .ha-e-node{position:absolute;display:flex;flex-direction:column;align-items:center;gap:.25rem;z-index:1}
  .ha-e-node.solar{top:0;left:calc(50% - 40px)}
  .ha-e-node.grid{top:105px;left:0}
  .ha-e-node.home{top:105px;right:0}
  .ha-e-node.battery{bottom:0;left:calc(50% - 40px)}
  .ha-e-circle{width:80px;height:80px;border-radius:50%;border:2px solid var(--border);background:var(--card);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1px;font-size:.72rem;box-shadow:0 1px 4px rgba(0,0,0,.08)}
  .ha-e-circle.c-solar{border-color:#ff9800;color:#ff9800}
  .ha-e-circle.c-grid{border-color:#488fc2;color:#488fc2}
  .ha-e-circle.c-home{border-color:var(--accent);color:var(--accent);border-width:3px}
  .ha-e-circle.c-battery{border-color:#4db6ac;color:#4db6ac}
  .ha-e-val{font-size:.82rem;font-weight:700;color:var(--text)}
  .ha-e-sub{font-size:.65rem;color:var(--muted)}
  .ha-e-label{font-size:.68rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
  /* solar label above */
  .ha-e-node.solar{flex-direction:column}
  .ha-e-node.solar .ha-e-label{order:-1}
  /* grid/battery label below */
  .ha-e-node.grid .ha-e-label,.ha-e-node.battery .ha-e-label{order:1}
  /* home label below */
  .ha-e-node.home{align-items:center}
  .ha-e-node.home .ha-e-label{order:1}
  /* flow lines SVG */
  .ha-e-lines{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:0;overflow:visible}
  .ha-e-path{fill:none;stroke-width:1;vector-effect:non-scaling-stroke}
  .p-solar{stroke:#ff9800}.p-return{stroke:#488fc2}.p-grid{stroke:#488fc2}
  .p-bat-home{stroke:#4db6ac}.p-bat-grid{stroke:#4db6ac}
  .d-solar{fill:#ff9800}.d-return{fill:#488fc2}.d-grid{fill:#488fc2}
  .d-bat-home{fill:#4db6ac}.d-bat-grid{fill:#4db6ac}
  /* Energy — EPEX */
  .epex-pills{display:grid;grid-template-columns:repeat(4,1fr);gap:.5rem;margin-bottom:.75rem}
  .pill{background:var(--bg);border:1px solid var(--border);border-radius:.5rem;padding:.4rem .6rem;text-align:center}
  .pill-label{font-size:.62rem;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
  .pill-val{font-size:.95rem;font-weight:700;margin-top:.1rem}
  .day-toggle{display:flex;gap:.4rem}
  .day-btn{padding:.2rem .6rem;border-radius:.4rem;border:1px solid var(--border);background:none;color:var(--muted);cursor:pointer;font-size:.75rem}
  .day-btn.active{background:var(--accent);border-color:var(--accent);color:#fff}
  .chart-wrap{position:relative;height:160px;margin-bottom:.5rem}
  .price-table{width:100%;border-collapse:collapse;font-size:.75rem}
  .price-table th{color:var(--muted);font-size:.62rem;text-transform:uppercase;padding:.25rem .4rem;border-bottom:1px solid var(--border);text-align:left;position:sticky;top:0;background:var(--card)}
  .price-table td{padding:.25rem .4rem;border-bottom:1px solid var(--border)}
  .price-table tr.cur td{background:#d1fae5;color:#065f46;font-weight:700}
  @media(prefers-color-scheme:dark){.price-table tr.cur td{background:#064e3b;color:#10b981}}
  .pbar{height:5px;border-radius:2px;margin-top:2px}
  .energy-layout{display:grid;grid-template-columns:1fr 210px;gap:.75rem}
  @media(max-width:700px){.energy-layout{grid-template-columns:1fr}}
  .no-epex{font-size:.82rem;color:var(--muted);text-align:center;padding:1.25rem 0;line-height:1.7}
  .no-epex a{color:var(--accent);text-decoration:none;font-weight:500}
  .no-epex a:hover{text-decoration:underline}
</style>
</head>
<body>
<h1><span class="dot"></span> HA EMS</h1>

<nav>
  <div style="display:flex;gap:.5rem">
    <button class="nav-btn active" onclick="showPage('dashboard',this)">Dashboard</button>
    <button class="nav-btn" onclick="showPage('energy',this)">Energy ⚡</button>
  </div>
  <button class="nav-btn nav-settings" id="btn-settings" onclick="showPage('settings',this)" title="Settings">✎</button>
</nav>

<!-- DASHBOARD PAGE -->
<div id="page-dashboard" class="page active">
  <div class="section-title">Mode</div>
  <div class="mode-bar" id="modeBar">
    <button class="mode-btn" data-mode="auto">Auto</button>
    <button class="mode-btn" data-mode="eco">Eco</button>
    <button class="mode-btn" data-mode="cheap">Cheap</button>
    <button class="mode-btn" data-mode="manual">Manual</button>
    <button class="mode-btn" data-mode="off">Off</button>
  </div>
  <div class="section-title">Live readings</div>
  <div class="grid">
    <div class="card"><div class="card-label">Solar</div><div class="card-value" id="solar">--</div><div class="card-sub">W production</div></div>
    <div class="card"><div class="card-label">Grid</div><div class="card-value" id="grid">--</div><div class="card-sub">W (+ import)</div></div>
    <div class="card"><div class="card-label">Solar surplus</div><div class="card-value" id="surplus">--</div><div class="card-sub">W available</div></div>
    <div class="card"><div class="card-label">Battery SOC</div><div class="card-value" id="batSoc">--</div><div class="card-sub">%</div></div>
    <div class="card"><div class="card-label">EV SOC</div><div class="card-value" id="evSoc">--</div><div class="card-sub">%</div></div>
    <div class="card"><div class="card-label">Tariff</div><div class="card-value" id="tariff">--</div><div class="card-sub">EUR/kWh</div></div>
  </div>
  <div class="section-title">Decisions</div>
  <div class="grid">
    <div class="card"><div class="card-label">Battery</div><div id="batDecision"><span class="badge badge-gray">--</span></div></div>
    <div class="card"><div class="card-label">EV Charger</div><div id="evDecision"><span class="badge badge-gray">--</span></div></div>
  </div>
  <div class="reason-card"><div class="card-label">Last decision reason</div><p id="reason">--</p></div>
  <div class="updated" id="updated"></div>
</div>

<!-- SETTINGS PAGE -->
<div id="page-settings" class="page">
  <div class="settings-grid">

    <div class="settings-group">
      <div class="sg-head"><h3>Power sensors</h3><button class="pencil-btn" onclick="toggleEdit('sg-power')" title="Edit">✎</button></div>
      <div id="sg-power-view">
        <div class="sg-row"><span class="sg-key">Solar</span><span id="v_solar_power_sensor" class="sg-val">—</span></div>
        <div class="sg-row"><span class="sg-key">Grid</span><span id="v_grid_power_sensor" class="sg-val">—</span></div>
        <div class="sg-row"><span class="sg-key">House</span><span id="v_house_power_sensor" class="sg-val">—</span></div>
        <div class="sg-row"><span class="sg-key">Battery power</span><span id="v_battery_power_sensor" class="sg-val">—</span></div>
      </div>
      <div id="sg-power-edit" class="sg-edit" style="display:none">
        <div class="field"><label>Solar production (W)</label><div class="combo"><input class="combo-input" id="s_solar_power_sensor" placeholder="Search W sensor…" autocomplete="off"><ul class="combo-list" id="sl_solar_power_sensor"></ul></div></div>
        <div class="field"><label>Grid power (W, + = import)</label><div class="combo"><input class="combo-input" id="s_grid_power_sensor" placeholder="Search W sensor…" autocomplete="off"><ul class="combo-list" id="sl_grid_power_sensor"></ul></div></div>
        <div class="field"><label>House consumption (W, optional)</label><div class="combo"><input class="combo-input" id="s_house_power_sensor" placeholder="Search W sensor…" autocomplete="off"><ul class="combo-list" id="sl_house_power_sensor"></ul></div></div>
        <div class="field"><label>Battery power (W, + charge / − discharge)</label><div class="combo"><input class="combo-input" id="s_battery_power_sensor" placeholder="Search W sensor…" autocomplete="off"><ul class="combo-list" id="sl_battery_power_sensor"></ul></div></div>
        <button class="save-btn" onclick="saveGroup(['solar_power_sensor','grid_power_sensor','house_power_sensor','battery_power_sensor'],'sg-power')">Save</button>
      </div>
    </div>

    <div class="settings-group">
      <div class="sg-head"><h3>Battery</h3><button class="pencil-btn" onclick="toggleEdit('sg-bat')" title="Edit">✎</button></div>
      <div id="sg-bat-view">
        <div class="sg-row"><span class="sg-key">SOC sensor</span><span id="v_battery_soc_sensor" class="sg-val">—</span></div>
        <div class="sg-row"><span class="sg-key">Charge switch</span><span id="v_battery_charge_switch" class="sg-val">—</span></div>
        <div class="sg-row"><span class="sg-key">Discharge switch</span><span id="v_battery_discharge_switch" class="sg-val">—</span></div>
        <div class="sg-row"><span class="sg-key">Max charge / discharge</span><span id="v_battery_max_charge_w" class="sg-val">—</span></div>
        <div class="sg-row"><span class="sg-key">SOC range</span><span id="v_battery_soc_range" class="sg-val">—</span></div>
      </div>
      <div id="sg-bat-edit" class="sg-edit" style="display:none">
        <div class="field"><label>Battery SOC (%)</label><div class="combo"><input class="combo-input" id="s_battery_soc_sensor" placeholder="Search % sensor…" autocomplete="off"><ul class="combo-list" id="sl_battery_soc_sensor"></ul></div></div>
        <div class="field"><label>Charge switch</label><div class="combo"><input class="combo-input" id="s_battery_charge_switch" placeholder="Search switch…" autocomplete="off"><ul class="combo-list" id="sl_battery_charge_switch"></ul></div></div>
        <div class="field"><label>Discharge switch</label><div class="combo"><input class="combo-input" id="s_battery_discharge_switch" placeholder="Search switch…" autocomplete="off"><ul class="combo-list" id="sl_battery_discharge_switch"></ul></div></div>
        <div class="field"><label>Standby switch (optional)</label><div class="combo"><input class="combo-input" id="s_battery_standby_switch" placeholder="Search switch…" autocomplete="off"><ul class="combo-list" id="sl_battery_standby_switch"></ul></div></div>
        <div class="field"><label>Max charge (W)</label><input type="number" id="s_battery_max_charge_w" min="100" max="20000"></div>
        <div class="field"><label>Max discharge (W)</label><input type="number" id="s_battery_max_discharge_w" min="100" max="20000"></div>
        <div class="field"><label>Min SOC (%)</label><input type="number" id="s_battery_min_soc" min="0" max="50"></div>
        <div class="field"><label>Max SOC (%)</label><input type="number" id="s_battery_max_soc" min="50" max="100"></div>
        <button class="save-btn" onclick="saveGroup(['battery_soc_sensor','battery_charge_switch','battery_discharge_switch','battery_standby_switch','battery_max_charge_w','battery_max_discharge_w','battery_min_soc','battery_max_soc'],'sg-bat')">Save</button>
      </div>
    </div>

    <div class="settings-group">
      <div class="sg-head"><h3>EV Charger</h3><button class="pencil-btn" onclick="toggleEdit('sg-ev')" title="Edit">✎</button></div>
      <div id="sg-ev-view">
        <div class="sg-row"><span class="sg-key">Charger switch</span><span id="v_ev_charger_switch" class="sg-val">—</span></div>
        <div class="sg-row"><span class="sg-key">SOC sensor</span><span id="v_ev_soc_sensor" class="sg-val">—</span></div>
        <div class="sg-row"><span class="sg-key">Target SOC</span><span id="v_ev_target_soc" class="sg-val">—</span></div>
        <div class="sg-row"><span class="sg-key">Departure</span><span id="v_ev_departure_time" class="sg-val">—</span></div>
        <div class="sg-row"><span class="sg-key">Max charge</span><span id="v_ev_max_charge_w" class="sg-val">—</span></div>
      </div>
      <div id="sg-ev-edit" class="sg-edit" style="display:none">
        <div class="field"><label>Charger switch</label><div class="combo"><input class="combo-input" id="s_ev_charger_switch" placeholder="Search switch…" autocomplete="off"><ul class="combo-list" id="sl_ev_charger_switch"></ul></div></div>
        <div class="field"><label>EV SOC sensor (%)</label><div class="combo"><input class="combo-input" id="s_ev_soc_sensor" placeholder="Search % sensor…" autocomplete="off"><ul class="combo-list" id="sl_ev_soc_sensor"></ul></div></div>
        <div class="field"><label>Target SOC (%)</label><input type="number" id="s_ev_target_soc" min="20" max="100"></div>
        <div class="field"><label>Departure (HH:MM)</label><input type="time" id="s_ev_departure_time"></div>
        <div class="field"><label>Max charge (W)</label><input type="number" id="s_ev_max_charge_w" min="1000" max="22000"></div>
        <button class="save-btn" onclick="saveGroup(['ev_charger_switch','ev_soc_sensor','ev_target_soc','ev_departure_time','ev_max_charge_w'],'sg-ev')">Save</button>
      </div>
    </div>

    <div class="settings-group">
      <div class="sg-head"><h3>Tariff &amp; optimizer</h3><button class="pencil-btn" onclick="toggleEdit('sg-tariff')" title="Edit">✎</button></div>
      <div id="sg-tariff-view">
        <div class="sg-row"><span class="sg-key">Price sensor</span><span id="v_tariff_sensor" class="sg-val">—</span></div>
        <div class="sg-row"><span class="sg-key">Cheap &lt;</span><span id="v_cheap_threshold" class="sg-val">—</span></div>
        <div class="sg-row"><span class="sg-key">Expensive &gt;</span><span id="v_expensive_threshold" class="sg-val">—</span></div>
        <div class="sg-row"><span class="sg-key">Update interval</span><span id="v_update_interval" class="sg-val">—</span></div>
      </div>
      <div id="sg-tariff-edit" class="sg-edit" style="display:none">
        <div class="field"><label>Price sensor (EUR/kWh, optional)</label><div class="combo"><input class="combo-input" id="s_tariff_sensor" placeholder="Search price sensor…" autocomplete="off"><ul class="combo-list" id="sl_tariff_sensor"></ul></div></div>
        <div class="field"><label>Cheap threshold (EUR/kWh)</label><input type="number" id="s_cheap_threshold" step="0.01" min="0" max="1"></div>
        <div class="field"><label>Expensive threshold (EUR/kWh)</label><input type="number" id="s_expensive_threshold" step="0.01" min="0" max="1"></div>
        <div class="field"><label>Update interval (s)</label><input type="number" id="s_update_interval" min="10" max="3600"></div>
        <button class="save-btn" onclick="saveGroup(['tariff_sensor','cheap_threshold','expensive_threshold','update_interval'],'sg-tariff')">Save</button>
      </div>
    </div>

  </div>
</div>

<!-- ENERGY PAGE -->
<div id="page-energy" class="page">

  <!-- HA-style Energy Distribution -->
  <div class="card" style="margin-bottom:.75rem;padding:1rem">
    <div class="card-label" style="margin-bottom:.5rem">Energy distribution</div>
    <div class="ha-e-wrap">

      <!-- Solar -->
      <div class="ha-e-node solar">
        <span class="ha-e-label">Solar</span>
        <div class="ha-e-circle c-solar">
          <svg viewBox="0 0 24 24" width="22" height="22"><path fill="#ff9800" d="M11.45,2V5.55L15,3.77L11.45,2M10.45,8L8,10.46L11.75,11.71L10.45,8M2,11.45L3.77,15L5.55,11.45H2M10,2H2V10C2.57,10.17 3.17,10.25 3.77,10.25C7.35,10.26 10.26,7.35 10.27,3.75C10.26,3.16 10.17,2.57 10,2M17,22V16H14L19,7V13H22L17,22Z"/></svg>
          <span class="ha-e-val" id="ev-solar">-- W</span>
        </div>
      </div>

      <!-- Grid -->
      <div class="ha-e-node grid">
        <div class="ha-e-circle c-grid">
          <svg viewBox="0 0 24 24" width="22" height="22"><path fill="#488fc2" d="M8.28,5.45L6.5,4.55L7.76,2H16.23L17.5,4.55L15.72,5.44L15,4H9L8.28,5.45M18.62,8H14.09L13.3,5H10.7L9.91,8H5.38L4.1,10.55L5.89,11.44L6.62,10H17.38L18.1,11.45L19.89,10.56L18.62,8M17.77,22H15.7L15.46,21.1L12,15.9L8.53,21.1L8.3,22H6.23L9.12,11H11.19L10.83,12.35L12,14.1L13.16,12.35L12.81,11H14.88L17.77,22M11.4,15L10.5,13.65L9.32,18.13L11.4,15M14.68,18.12L13.5,13.64L12.6,15L14.68,18.12Z"/></svg>
          <span class="ha-e-val" id="ev-grid">-- W</span>
          <span class="ha-e-sub" id="ev-grid-dir">--</span>
        </div>
        <span class="ha-e-label">Grid</span>
      </div>

      <!-- Home -->
      <div class="ha-e-node home">
        <div class="ha-e-circle c-home">
          <svg viewBox="0 0 24 24" width="22" height="22"><path fill="currentColor" d="M10,20V14H14V20H19V12H22L12,3L2,12H5V20H10Z"/></svg>
          <span class="ha-e-val" id="ev-home">-- W</span>
        </div>
        <span class="ha-e-label">Home</span>
      </div>

      <!-- Battery -->
      <div class="ha-e-node battery">
        <div class="ha-e-circle c-battery">
          <svg viewBox="0 0 24 24" width="20" height="20"><path fill="#4db6ac" d="M16.67,4H15V2H9V4H7.33A1.33,1.33 0 0,0 6,5.33V20.67C6,21.4 6.6,22 7.33,22H16.67A1.33,1.33 0 0,0 18,20.67V5.33C18,4.6 17.4,4 16.67,4Z"/></svg>
          <span class="ha-e-val" id="ev-bat">-- %</span>
          <span class="ha-e-sub" id="ev-bat-dec">--</span>
        </div>
        <span class="ha-e-label">Battery</span>
      </div>

      <!-- SVG flow lines (viewBox matches 500x290 container, preserveAspectRatio=none) -->
      <svg class="ha-e-lines" viewBox="0 0 100 100" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">
        <!-- Solar (center top, ~28% down) → Home (right, ~50% down) -->
        <path id="epl-solar"    class="ha-e-path p-solar"    d="M50,27 C50,48 72,48 84,50"/>
        <!-- Solar → Grid (export) -->
        <path id="epl-return"   class="ha-e-path p-return"   d="M50,27 C50,48 28,48 16,50"/>
        <!-- Grid import → Home -->
        <path id="epl-grid"     class="ha-e-path p-grid"     d="M16,50 H84"/>
        <!-- Battery → Home -->
        <path id="epl-bat-home" class="ha-e-path p-bat-home" d="M50,73 C50,52 72,52 84,50"/>
        <!-- Battery → Grid -->
        <path id="epl-bat-grid" class="ha-e-path p-bat-grid" d="M50,73 C50,52 28,52 16,50"/>

        <!-- Animated dots – hidden until JS turns them on -->
        <circle r="1.2" class="d-solar"    id="edot-solar"    style="display:none"><animateMotion dur="2.8s" repeatCount="indefinite" calcMode="linear"><mpath xlink:href="#epl-solar"/></animateMotion></circle>
        <circle r="1.2" class="d-return"   id="edot-return"   style="display:none"><animateMotion dur="3.2s" repeatCount="indefinite" calcMode="linear"><mpath xlink:href="#epl-return"/></animateMotion></circle>
        <circle r="1.2" class="d-grid"     id="edot-grid"     style="display:none"><animateMotion dur="4s"   repeatCount="indefinite" calcMode="linear"><mpath xlink:href="#epl-grid"/></animateMotion></circle>
        <circle r="1.2" class="d-bat-home" id="edot-bat-home" style="display:none"><animateMotion dur="3.5s" repeatCount="indefinite" calcMode="linear"><mpath xlink:href="#epl-bat-home"/></animateMotion></circle>
        <circle r="1.2" class="d-bat-grid" id="edot-bat-grid" style="display:none"><animateMotion dur="4s"   repeatCount="indefinite" calcMode="linear"><mpath xlink:href="#epl-bat-grid"/></animateMotion></circle>
      </svg>
    </div>
  </div>

  <!-- EPEX prices -->
  <div class="section-title">EPEX SPOT prices</div>
  <div class="energy-layout">
    <div>
      <div class="card" style="margin-bottom:.75rem">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.5rem">
          <span class="card-label">Day-ahead</span>
          <div class="day-toggle">
            <button class="day-btn active" onclick="epexDay('today',this)">Today</button>
            <button class="day-btn" id="tmrw-btn" onclick="epexDay('tomorrow',this)" style="display:none">Tomorrow</button>
          </div>
        </div>
        <div class="epex-pills">
          <div class="pill"><div class="pill-label">Now</div><div class="pill-val" style="color:var(--yellow)" id="ep-now">--</div></div>
          <div class="pill"><div class="pill-label">Next</div><div class="pill-val" id="ep-next">--</div></div>
          <div class="pill"><div class="pill-label">Min</div><div class="pill-val" style="color:var(--green)" id="ep-min">--</div></div>
          <div class="pill"><div class="pill-label">Max</div><div class="pill-val" style="color:var(--red)" id="ep-max">--</div></div>
        </div>
        <div class="chart-wrap"><canvas id="epexChart"></canvas></div>
        <div class="no-epex" id="no-epex" style="display:none">
          No EPEX data — add your ENTSO-E token<br>in <strong>Add-on → Configuration</strong>.<br><br>
          <a href="https://transparency.entsoe.eu/usrm/user/createPublicUser" target="_blank" rel="noopener">→ Get a free ENTSO-E token</a>
        </div>
        <div class="updated" id="ep-zone"></div>
      </div>
    </div>
    <div>
      <div class="card" style="max-height:340px;overflow:hidden">
        <div class="card-label" id="sched-title" style="margin-bottom:.4rem">Schedule — Today</div>
        <div style="max-height:290px;overflow-y:auto">
          <table class="price-table">
            <thead><tr><th>Time</th><th>ct/kWh</th><th></th></tr></thead>
            <tbody id="sched-body"></tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="toast" id="toast">Saved!</div>

<script>
const BASE = window.location.pathname.replace(/\/+$/, "");

// ── Inherit HA theme from parent ingress frame ─────────────────────────────
(function(){
  try {
    const ps = window.parent.getComputedStyle(window.parent.document.documentElement);
    const map = {'--bg':'--primary-background-color','--card':'--card-background-color',
                 '--border':'--divider-color','--text':'--primary-text-color',
                 '--muted':'--secondary-text-color','--accent':'--primary-color'};
    const root = document.documentElement;
    for (const [l,h] of Object.entries(map)) {
      const v = ps.getPropertyValue(h).trim();
      if (v) root.style.setProperty(l, v);
    }
  } catch(e) { /* standalone or cross-origin — use prefers-color-scheme fallback */ }
})();

let _epexData = null, _epexChartInst = null, _epexDay = 'today';

function showPage(name, btn) {
  document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
  document.querySelectorAll(".nav-btn").forEach(b => b.classList.remove("active"));
  document.getElementById("page-" + name).classList.add("active");
  btn.classList.add("active");
  if (name === "settings") loadSettings();
  if (name === "energy" && !_epexData) loadEpex();
}

function showToast() {
  const t = document.getElementById("toast");
  t.style.display = "block";
  setTimeout(() => t.style.display = "none", 2500);
}

function badge(val, map) {
  const cfg = map[val] || {cls:"gray", label: val||"--"};
  return `<span class="badge badge-${cfg.cls}">${cfg.label}</span>`;
}
const BAT_MAP = {charge:{cls:"green",label:"Charging"},discharge:{cls:"yellow",label:"Discharging"},standby:{cls:"blue",label:"Standby"},idle:{cls:"gray",label:"Idle"}};
const EV_MAP  = {charge:{cls:"green",label:"Charging"},pause:{cls:"gray",label:"Paused"}};

async function refresh() {
  try {
    const d = await fetch(BASE+"/api/state").then(r=>r.json());
    document.getElementById("solar").textContent   = d.solar_w ?? "--";
    document.getElementById("grid").textContent    = d.grid_w ?? "--";
    document.getElementById("surplus").textContent = d.solar_surplus_w ?? "--";
    document.getElementById("batSoc").textContent  = d.battery_soc!=null ? d.battery_soc+"%" : "--";
    document.getElementById("evSoc").textContent   = d.ev_soc!=null ? d.ev_soc+"%" : "--";
    document.getElementById("tariff").textContent  = d.tariff!=null ? d.tariff.toFixed(3) : "--";
    document.getElementById("batDecision").innerHTML = badge(d.battery, BAT_MAP);
    document.getElementById("evDecision").innerHTML  = badge(d.ev, EV_MAP);
    document.getElementById("reason").textContent  = d.reason || "--";
    document.getElementById("updated").textContent = "Updated: "+(d.updated_at||"--");
    document.querySelectorAll(".mode-btn").forEach(b => b.classList.toggle("active", b.dataset.mode===d.mode));
    updateFlow(d);
  } catch(e) { console.error(e); }
}

function updateFlow(d) {
  const set  = (id,v) => { const el=document.getElementById(id); if(el) el.textContent=v; };
  const show = (id,v) => { const el=document.getElementById(id); if(el) el.style.display=v?'':'none'; };

  const solar   = d.solar_w   ?? 0;
  const grid    = d.grid_w    ?? 0;  // + = importing, - = exporting
  const batSoc  = d.battery_soc;
  const batDec  = d.battery || 'idle';
  const homeEst = Math.max(0, solar + Math.max(0, grid) +
                  (batDec==='discharge' ? (d.battery_max_discharge_w||0)*0.5 : 0) -
                  (batDec==='charge'   ? (d.battery_max_charge_w||0)*0.5   : 0));

  set('ev-solar',    solar > 0 ? solar+' W' : '0 W');
  set('ev-grid',     Math.abs(grid)+' W');
  set('ev-grid-dir', grid > 0 ? 'Import' : grid < 0 ? 'Export' : 'Idle');
  set('ev-home',     homeEst > 0 ? Math.round(homeEst)+' W' : '-- W');
  set('ev-bat',      batSoc!=null ? batSoc+'%' : '--');
  const batW = d.battery_w;
  set('ev-bat-dec',  batW!=null ? (batW>50?'↑ '+batW+' W':batW<-50?'↓ '+Math.abs(batW)+' W':'idle') : batDec);

  // Animated dot visibility
  show('edot-solar',    solar > 50);                        // solar → home
  show('edot-return',   grid < -50);                        // solar/bat → grid (export)
  show('edot-grid',     grid >  50);                        // grid → home (import)
  show('edot-bat-home', batDec === 'discharge');             // battery → home
  show('edot-bat-grid', batDec === 'discharge' && grid < 0); // battery → grid
}

document.querySelectorAll(".mode-btn").forEach(btn => {
  btn.addEventListener("click", async () => {
    await fetch(BASE+"/api/mode", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:btn.dataset.mode})});
    await refresh();
  });
});

// ── Settings ─────────────────────────────────────────────────────────────────

// Unit filter map: which units to prefer for each field
const FIELD_UNITS = {
  solar_power_sensor:        ['W','kW','watt','watts'],
  grid_power_sensor:         ['W','kW','watt','watts'],
  house_power_sensor:        ['W','kW','watt','watts'],
  battery_power_sensor:      ['W','kW','watt','watts'],
  battery_soc_sensor:        ['%'],
  ev_soc_sensor:             ['%'],
  tariff_sensor:             ['EUR/kWh','€/kWh','$/kWh','USD/kWh','ct/kWh'],
};
const SWITCH_FIELDS = ['battery_charge_switch','battery_discharge_switch','battery_standby_switch','ev_charger_switch'];
const COMBO_FIELDS  = [...Object.keys(FIELD_UNITS), ...SWITCH_FIELDS];

function shorten(val) { return val ? (val.split('.').slice(1).join('.')||val) : '—'; }

// Combobox engine
function setupCombo(fieldKey, allEntities, currentVal) {
  const input = document.getElementById('s_'+fieldKey);
  const list  = document.getElementById('sl_'+fieldKey);
  if (!input || !list) return;

  // Build filtered pool
  const isSwitch   = SWITCH_FIELDS.includes(fieldKey);
  const wantedUnits = FIELD_UNITS[fieldKey] || [];
  let pool;
  if (isSwitch) {
    pool = allEntities.filter(e => e.entity_id.startsWith('switch.')||e.entity_id.startsWith('input_boolean.'));
  } else if (wantedUnits.length) {
    const pref = allEntities.filter(e =>
      e.entity_id.startsWith('sensor.') &&
      wantedUnits.some(u => (e.unit||'').toLowerCase().includes(u.toLowerCase()))
    );
    pool = pref.length ? pref : allEntities.filter(e => e.entity_id.startsWith('sensor.'));
  } else {
    pool = allEntities.filter(e => e.entity_id.startsWith('sensor.'));
  }

  // Set initial display
  input.dataset.value = currentVal || '';
  const findName = id => { const e = allEntities.find(x=>x.entity_id===id); return e ? (e.friendly_name||e.entity_id) : id; };
  input.value = currentVal ? findName(currentVal) : '';

  function renderList(q) {
    const lq = q.toLowerCase().trim();
    const matches = pool.filter(e =>
      !lq ||
      e.entity_id.toLowerCase().includes(lq) ||
      (e.friendly_name||'').toLowerCase().includes(lq)
    ).slice(0, 50);
    list.innerHTML =
      `<li data-id="" class="cl-none">— none —</li>` +
      matches.map(e => {
        const name = (e.friendly_name && e.friendly_name !== e.entity_id) ? e.friendly_name : e.entity_id;
        const unit = e.unit ? `<span class="cl-unit">${e.unit}</span>` : '';
        return `<li data-id="${e.entity_id}" title="${e.entity_id}"><span class="cl-name">${name}</span>${unit}</li>`;
      }).join('');
    list.style.display = 'block';   // ← was '' which kept CSS display:none
  }

  // On focus: clear text so user can type freely, show full list
  input.addEventListener('focus', () => {
    input.value = '';
    renderList('');
  });

  input.addEventListener('input', () => {
    input.dataset.value = '';
    renderList(input.value);
  });

  input.addEventListener('keydown', ev => {
    if (ev.key === 'Escape') { list.style.display = 'none'; input.blur(); }
  });

  // On blur: hide list, restore display name if selection was made
  input.addEventListener('blur', () => {
    setTimeout(() => { list.style.display = 'none'; }, 200);
    // Restore friendly name of the currently selected value
    const sel = input.dataset.value;
    input.value = sel ? findName(sel) : '';
  });

  // mousedown fires before blur — capture selection first
  list.addEventListener('mousedown', ev => {
    ev.preventDefault();   // prevent blur from firing before we read the click
    const li = ev.target.closest('li'); if (!li) return;
    input.dataset.value = li.dataset.id;
    currentVal          = li.dataset.id;
    input.value         = li.dataset.id ? findName(li.dataset.id) : '';
    list.style.display  = 'none';
  });
}

function getComboValue(fieldKey) {
  const input = document.getElementById('s_'+fieldKey);
  return input ? input.dataset.value : '';
}

let _allEntities = [];

async function loadSettings() {
  const [settingsRes, entitiesRes] = await Promise.all([
    fetch(BASE+"/api/settings").then(r=>r.json()),
    fetch(BASE+"/api/entities").then(r=>r.json()),
  ]);
  _allEntities = entitiesRes.entities || [];

  // Setup combo fields
  for (const key of COMBO_FIELDS) {
    setupCombo(key, _allEntities, settingsRes[key] || '');
    const v = document.getElementById("v_"+key);
    if (v) v.textContent = shorten(settingsRes[key]);
  }

  // Numeric / time inputs
  const numFmt = {
    battery_max_charge_w:   v=>v+' W',      battery_max_discharge_w: v=>v+' W',
    battery_min_soc:        v=>v+'%',       battery_max_soc:         v=>v+'%',
    ev_target_soc:          v=>v+'%',       ev_max_charge_w:         v=>v+' W',
    cheap_threshold:        v=>v+' €/kWh', expensive_threshold:     v=>v+' €/kWh',
    update_interval:        v=>v+'s',       ev_departure_time:       v=>v,
  };
  for (const [key,fmt] of Object.entries(numFmt)) {
    const el = document.getElementById("s_"+key); if (el&&settingsRes[key]!=null) el.value = settingsRes[key];
    const v  = document.getElementById("v_"+key); if (v&&settingsRes[key]!=null)  v.textContent = fmt(settingsRes[key]);
  }
  const socRange  = document.getElementById("v_battery_soc_range");
  if (socRange)  socRange.textContent  = `${settingsRes.battery_min_soc??'?'}% – ${settingsRes.battery_max_soc??'?'}%`;
  const maxCharge = document.getElementById("v_battery_max_charge_w");
  if (maxCharge) maxCharge.textContent = `${settingsRes.battery_max_charge_w??'?'} W / ${settingsRes.battery_max_discharge_w??'?'} W`;
}

function toggleEdit(id) {
  const view = document.getElementById(id+'-view');
  const edit = document.getElementById(id+'-edit');
  if (!view||!edit) return;
  const editing = edit.style.display !== 'none';
  view.style.display = editing ? '' : 'none';
  edit.style.display = editing ? 'none' : '';
}

async function saveGroup(keys, groupId) {
  const body = {};
  const intKeys   = ['battery_max_charge_w','battery_max_discharge_w','battery_min_soc','battery_max_soc','ev_target_soc','ev_max_charge_w','update_interval'];
  const floatKeys = ['cheap_threshold','expensive_threshold'];
  for (const key of keys) {
    let val;
    if (COMBO_FIELDS.includes(key)) {
      val = getComboValue(key);   // combo — read dataset.value
    } else {
      const el = document.getElementById('s_'+key); if (!el) continue;
      val = el.value;
    }
    if (val === '' || val === undefined) continue;
    body[key] = intKeys.includes(key) ? parseInt(val) : floatKeys.includes(key) ? parseFloat(val) : val;
  }
  await fetch(BASE+'/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  await loadSettings();
  toggleEdit(groupId);
  showToast();
}

refresh();
setInterval(refresh, 10000);

// ── ENERGY TAB ────────────────────────────────────────────────

async function loadEpex() {
  try { _epexData = await fetch(BASE+'/api/epex').then(r=>r.json()); } catch(e){}
  renderEpex();
}

function ct(v) { return v != null ? (v*100).toFixed(2)+' ct' : '--'; }

function renderEpex() {
  const d = _epexData;
  if (!d || d.error || !d.prices_today || !d.prices_today.length) {
    document.getElementById('no-epex').style.display='block';
    document.getElementById('epexChart').style.display='none';
    return;
  }
  document.getElementById('no-epex').style.display='none';
  document.getElementById('epexChart').style.display='block';
  document.getElementById('ep-now').textContent  = ct(d.current_price);
  document.getElementById('ep-next').textContent = ct(d.next_slot_price);
  document.getElementById('ep-min').textContent  = ct(d.today_min);
  document.getElementById('ep-max').textContent  = ct(d.today_max);
  document.getElementById('ep-zone').textContent = 'Zone: '+(d.zone||'--')+' · '+(d.slot_minutes||60)+' min';
  if (d.prices_tomorrow && d.prices_tomorrow.length)
    document.getElementById('tmrw-btn').style.display='';
  const slots = _epexDay==='today' ? d.prices_today : (d.prices_tomorrow||[]);
  drawEpexChart(slots);
  renderSchedule(slots);
}

function epexDay(day, btn) {
  _epexDay = day;
  document.querySelectorAll('.day-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('sched-title').textContent = 'Schedule — '+(day==='today'?'Today':'Tomorrow');
  if (!_epexData) return;
  const slots = day==='today' ? _epexData.prices_today : (_epexData.prices_tomorrow||[]);
  drawEpexChart(slots); renderSchedule(slots);
}

function drawEpexChart(slots) {
  if (!slots||!slots.length) return;
  const now = new Date();
  const mn = Math.min(...slots.map(s=>s.price_eur_kwh));
  const mx = Math.max(...slots.map(s=>s.price_eur_kwh));
  const labels = slots.map(s=>new Date(s.start).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}));
  const vals   = slots.map(s=>+(s.price_eur_kwh*100).toFixed(3));
  const colors = slots.map(s=>{
    if (new Date(s.start)<=now && now<new Date(s.end)) return 'rgba(245,158,11,.95)';
    const r = mx>mn?(s.price_eur_kwh-mn)/(mx-mn):0.5;
    return r<0.33?'rgba(16,185,129,.8)':r>0.66?'rgba(239,68,68,.8)':'rgba(245,158,11,.75)';
  });
  const ctx = document.getElementById('epexChart').getContext('2d');
  if (_epexChartInst) _epexChartInst.destroy();
  _epexChartInst = new Chart(ctx,{
    type:'bar',
    data:{labels,datasets:[{data:vals,backgroundColor:colors,borderRadius:2}]},
    options:{
      responsive:true,maintainAspectRatio:false,
      animation:{duration:500},
      plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>` ${c.parsed.y.toFixed(2)} ct/kWh`}}},
      scales:{
        x:{ticks:{color:getComputedStyle(document.documentElement).getPropertyValue('--muted').trim()||'#6b7280',maxTicksLimit:10,font:{size:9}},grid:{display:false}},
        y:{ticks:{color:getComputedStyle(document.documentElement).getPropertyValue('--muted').trim()||'#6b7280',font:{size:9},callback:v=>v+' ct'},grid:{color:getComputedStyle(document.documentElement).getPropertyValue('--border').trim()||'#e5e7eb'}}
      }
    }
  });
}

function renderSchedule(slots) {
  const tbody = document.getElementById('sched-body');
  if (!slots||!slots.length){tbody.innerHTML='<tr><td colspan="3" style="color:var(--muted);text-align:center;padding:.5rem">No data</td></tr>';return;}
  const now=new Date();
  const mn=Math.min(...slots.map(s=>s.price_eur_kwh));
  const mx=Math.max(...slots.map(s=>s.price_eur_kwh));
  tbody.innerHTML=slots.map(s=>{
    const isCur=new Date(s.start)<=now&&now<new Date(s.end);
    const pct=mx>mn?Math.round((s.price_eur_kwh-mn)/(mx-mn)*100):50;
    const col=pct<33?'var(--green)':pct>66?'var(--red)':'var(--yellow)';
    const t=new Date(s.start).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
    return `<tr class="${isCur?'cur':''}"><td>${t}</td><td>${(s.price_eur_kwh*100).toFixed(2)}</td><td><div class="pbar" style="width:${Math.max(4,pct)}%;background:${col}"></div></td></tr>`;
  }).join('');
  const cur=tbody.querySelector('tr.cur');
  if(cur) setTimeout(()=>cur.scrollIntoView({block:'nearest',behavior:'smooth'}),100);
}

// Refresh every 60s to keep current-slot highlight up to date
setInterval(()=>{ if(_epexData) renderEpex(); }, 60*1000);
// Re-fetch full data every 15 min (prices update once/day ~13:00)
setInterval(loadEpex, 15*60*1000);
</script>
</body>
</html>
"""
