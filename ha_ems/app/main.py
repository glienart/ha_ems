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
    bat_soc = await ha_client.get_float(s.battery_soc_sensor, default=50.0)
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
<style>
  :root {
    --bg:#111827;--card:#1f2937;--border:#374151;
    --text:#f9fafb;--muted:#9ca3af;
    --green:#10b981;--yellow:#f59e0b;--red:#ef4444;--blue:#3b82f6;
    --accent:#10b981;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:system-ui,sans-serif;padding:1rem}
  /* Nav */
  nav{display:flex;gap:.5rem;margin-bottom:1.25rem;border-bottom:1px solid var(--border);padding-bottom:.75rem}
  .nav-btn{padding:.35rem .85rem;border-radius:.5rem;border:1px solid transparent;background:none;color:var(--muted);cursor:pointer;font-size:.85rem}
  .nav-btn.active{background:var(--card);border-color:var(--border);color:var(--text)}
  /* Cards */
  .page{display:none}.page.active{display:block}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.75rem;margin-bottom:1rem}
  .card{background:var(--card);border:1px solid var(--border);border-radius:.75rem;padding:1rem}
  .card-label{font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-bottom:.25rem}
  .card-value{font-size:1.5rem;font-weight:700}
  .card-sub{font-size:.75rem;color:var(--muted);margin-top:.25rem}
  .badge{display:inline-block;padding:.2rem .6rem;border-radius:9999px;font-size:.75rem;font-weight:600}
  .badge-green{background:#064e3b;color:var(--green)}
  .badge-yellow{background:#78350f;color:var(--yellow)}
  .badge-blue{background:#1e3a5f;color:var(--blue)}
  .badge-gray{background:var(--border);color:var(--muted)}
  .mode-bar{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:1rem}
  .mode-btn{padding:.4rem .9rem;border-radius:.5rem;border:1px solid var(--border);background:var(--card);color:var(--muted);cursor:pointer;font-size:.85rem}
  .mode-btn.active{border-color:var(--green);color:var(--green);background:#064e3b}
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
  .settings-group h3{font-size:.8rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-bottom:.75rem}
  .field{margin-bottom:.6rem}
  .field label{display:block;font-size:.78rem;color:var(--muted);margin-bottom:.2rem}
  .field select,.field input{width:100%;background:#111827;border:1px solid var(--border);color:var(--text);padding:.4rem .6rem;border-radius:.4rem;font-size:.82rem}
  .field select:focus,.field input:focus{outline:none;border-color:var(--accent)}
  .save-btn{background:var(--accent);color:#fff;border:none;padding:.5rem 1.25rem;border-radius:.5rem;cursor:pointer;font-size:.85rem;font-weight:600;margin-top:.5rem}
  .save-btn:hover{opacity:.9}
  .toast{position:fixed;bottom:1rem;right:1rem;background:var(--accent);color:#fff;padding:.5rem 1rem;border-radius:.5rem;font-size:.85rem;display:none}
</style>
</head>
<body>
<h1><span class="dot"></span> HA EMS</h1>

<nav>
  <button class="nav-btn active" onclick="showPage('dashboard',this)">Dashboard</button>
  <button class="nav-btn" onclick="showPage('settings',this)">Settings</button>
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
      <h3>Power sensors</h3>
      <div class="field"><label>Solar production (W)</label><select id="s_solar_power_sensor"><option value="">Loading...</option></select></div>
      <div class="field"><label>Grid power (W, + = import)</label><select id="s_grid_power_sensor"><option value="">Loading...</option></select></div>
      <div class="field"><label>House consumption (W, optional)</label><select id="s_house_power_sensor"><option value="">Loading...</option></select></div>
    </div>
    <div class="settings-group">
      <h3>Battery</h3>
      <div class="field"><label>Battery SOC (%)</label><select id="s_battery_soc_sensor"><option value="">Loading...</option></select></div>
      <div class="field"><label>Charge switch</label><select id="s_battery_charge_switch"><option value="">Loading...</option></select></div>
      <div class="field"><label>Discharge switch</label><select id="s_battery_discharge_switch"><option value="">Loading...</option></select></div>
      <div class="field"><label>Standby switch (optional)</label><select id="s_battery_standby_switch"><option value="">Loading...</option></select></div>
      <div class="field"><label>Max charge power (W)</label><input type="number" id="s_battery_max_charge_w" min="100" max="20000"></div>
      <div class="field"><label>Max discharge power (W)</label><input type="number" id="s_battery_max_discharge_w" min="100" max="20000"></div>
      <div class="field"><label>Min SOC (%)</label><input type="number" id="s_battery_min_soc" min="0" max="50"></div>
      <div class="field"><label>Max SOC (%)</label><input type="number" id="s_battery_max_soc" min="50" max="100"></div>
    </div>
    <div class="settings-group">
      <h3>EV Charger</h3>
      <div class="field"><label>Charger switch (optional)</label><select id="s_ev_charger_switch"><option value="">Loading...</option></select></div>
      <div class="field"><label>EV SOC sensor (optional)</label><select id="s_ev_soc_sensor"><option value="">Loading...</option></select></div>
      <div class="field"><label>Target SOC (%)</label><input type="number" id="s_ev_target_soc" min="20" max="100"></div>
      <div class="field"><label>Departure time (HH:MM)</label><input type="time" id="s_ev_departure_time"></div>
      <div class="field"><label>Max charge power (W)</label><input type="number" id="s_ev_max_charge_w" min="1000" max="22000"></div>
    </div>
    <div class="settings-group">
      <h3>Tariff</h3>
      <div class="field"><label>Price sensor (EUR/kWh, optional)</label><select id="s_tariff_sensor"><option value="">Loading...</option></select></div>
      <div class="field"><label>Cheap threshold (EUR/kWh)</label><input type="number" id="s_cheap_threshold" step="0.01" min="0" max="1"></div>
      <div class="field"><label>Expensive threshold (EUR/kWh)</label><input type="number" id="s_expensive_threshold" step="0.01" min="0" max="1"></div>
      <div class="field"><label>Update interval (s)</label><input type="number" id="s_update_interval" min="10" max="3600"></div>
    </div>
  </div>
  <button class="save-btn" onclick="saveSettings()">Save settings</button>
</div>

<div class="toast" id="toast">Saved!</div>

<script>
const BASE = window.location.pathname.replace(/[/]$/, "");

function showPage(name, btn) {
  document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
  document.querySelectorAll(".nav-btn").forEach(b => b.classList.remove("active"));
  document.getElementById("page-" + name).classList.add("active");
  btn.classList.add("active");
  if (name === "settings") loadSettings();
}

function badge(val, map) {
  const cfg = map[val] || {cls:"gray", label: val||"--"};
  return `<span class="badge badge-${cfg.cls}">${cfg.label}</span>`;
}
const BAT_MAP = {charge:{cls:"green",label:"Charging"},discharge:{cls:"yellow",label:"Discharging"},standby:{cls:"blue",label:"Standby"},idle:{cls:"gray",label:"Idle"}};
const EV_MAP = {charge:{cls:"green",label:"Charging"},pause:{cls:"gray",label:"Paused"}};

async function refresh() {
  try {
    const d = await fetch(BASE+"/api/state").then(r=>r.json());
    document.getElementById("solar").textContent = d.solar_w ?? "--";
    document.getElementById("grid").textContent = d.grid_w ?? "--";
    document.getElementById("surplus").textContent = d.solar_surplus_w ?? "--";
    document.getElementById("batSoc").textContent = d.battery_soc!=null ? d.battery_soc+"%" : "--";
    document.getElementById("evSoc").textContent = d.ev_soc!=null ? d.ev_soc+"%" : "--";
    document.getElementById("tariff").textContent = d.tariff!=null ? d.tariff.toFixed(3) : "--";
    document.getElementById("batDecision").innerHTML = badge(d.battery, BAT_MAP);
    document.getElementById("evDecision").innerHTML = badge(d.ev, EV_MAP);
    document.getElementById("reason").textContent = d.reason || "--";
    document.getElementById("updated").textContent = "Updated: "+(d.updated_at||"--");
    document.querySelectorAll(".mode-btn").forEach(b => b.classList.toggle("active", b.dataset.mode===d.mode));
  } catch(e) { console.error(e); }
}

document.querySelectorAll(".mode-btn").forEach(btn => {
  btn.addEventListener("click", async () => {
    await fetch(BASE+"/api/mode", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:btn.dataset.mode})});
    await refresh();
  });
});

// --- Settings ---
const SENSOR_SELECTS = ["s_solar_power_sensor","s_grid_power_sensor","s_house_power_sensor","s_battery_soc_sensor","s_tariff_sensor","s_ev_soc_sensor"];
const SWITCH_SELECTS = ["s_battery_charge_switch","s_battery_discharge_switch","s_battery_standby_switch","s_ev_charger_switch"];

function buildOptions(entities, currentVal, includeEmpty=true) {
  let html = includeEmpty ? `<option value="">-- none --</option>` : "";
  for (const e of entities) {
    const sel = e.entity_id === currentVal ? "selected" : "";
    const label = e.friendly_name !== e.entity_id ? `${e.friendly_name} (${e.entity_id})` : e.entity_id;
    html += `<option value="${e.entity_id}" ${sel}>${label}</option>`;
  }
  return html;
}

async function loadSettings() {
  const [settingsRes, sensorsRes, switchesRes] = await Promise.all([
    fetch(BASE+"/api/settings").then(r=>r.json()),
    fetch(BASE+"/api/entities?device_class=").then(r=>r.json()),
    fetch(BASE+"/api/entities?device_class=").then(r=>r.json()),
  ]);

  const sensors = sensorsRes.entities.filter(e => e.entity_id.startsWith("sensor."));
  const switches = switchesRes.entities.filter(e => e.entity_id.startsWith("switch.") || e.entity_id.startsWith("input_boolean."));

  // Populate sensor dropdowns
  for (const id of SENSOR_SELECTS) {
    const key = id.replace("s_","");
    const el = document.getElementById(id);
    if (el) el.innerHTML = buildOptions(sensors, settingsRes[key]);
  }
  // Populate switch dropdowns
  for (const id of SWITCH_SELECTS) {
    const key = id.replace("s_","");
    const el = document.getElementById(id);
    if (el) el.innerHTML = buildOptions(switches, settingsRes[key]);
  }
  // Numeric / text inputs
  const numFields = ["battery_max_charge_w","battery_max_discharge_w","battery_min_soc","battery_max_soc","ev_target_soc","ev_max_charge_w","cheap_threshold","expensive_threshold","update_interval","ev_departure_time"];
  for (const key of numFields) {
    const el = document.getElementById("s_"+key);
    if (el && settingsRes[key] != null) el.value = settingsRes[key];
  }
}

async function saveSettings() {
  const body = {};
  const allFields = [...SENSOR_SELECTS,...SWITCH_SELECTS].map(id=>id.replace("s_",""));
  const numFields = ["battery_max_charge_w","battery_max_discharge_w","battery_min_soc","battery_max_soc","ev_target_soc","ev_max_charge_w","cheap_threshold","expensive_threshold","update_interval","ev_departure_time"];

  for (const key of allFields) {
    const el = document.getElementById("s_"+key);
    if (el) body[key] = el.value;
  }
  for (const key of numFields) {
    const el = document.getElementById("s_"+key);
    if (el && el.value !== "") body[key] = key.includes("threshold") ? parseFloat(el.value) : (key==="ev_departure_time" ? el.value : parseInt(el.value));
  }

  await fetch(BASE+"/api/settings", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const t = document.getElementById("toast");
  t.style.display="block";
  setTimeout(()=>t.style.display="none", 2500);
}

refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""
