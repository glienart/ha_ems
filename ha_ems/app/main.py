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


# ---------------------------------------------------------------------------
# Lifespan -- start/stop background loop
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _loop_task
    _loop_task = asyncio.create_task(ems_loop())
    _LOGGER.info("EMS optimizer loop started")
    yield
    if _loop_task:
        _loop_task.cancel()
    _LOGGER.info("EMS optimizer loop stopped")


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


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Home Energy Management System</title>
<style>
  :root {
    --bg: #111827; --card: #1f2937; --border: #374151;
    --text: #f9fafb; --muted: #9ca3af;
    --green: #10b981; --yellow: #f59e0b; --red: #ef4444; --blue: #3b82f6;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: system-ui, sans-serif; padding: 1rem; }
  h1 { font-size: 1.25rem; font-weight: 600; margin-bottom: 1rem; display: flex; align-items: center; gap: .5rem; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: .75rem; margin-bottom: 1rem; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: .75rem; padding: 1rem; }
  .card-label { font-size: .7rem; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); margin-bottom: .25rem; }
  .card-value { font-size: 1.5rem; font-weight: 700; }
  .card-sub { font-size: .75rem; color: var(--muted); margin-top: .25rem; }
  .badge { display: inline-block; padding: .2rem .6rem; border-radius: 9999px; font-size: .75rem; font-weight: 600; }
  .badge-green { background: #064e3b; color: var(--green); }
  .badge-yellow { background: #78350f; color: var(--yellow); }
  .badge-red { background: #7f1d1d; color: var(--red); }
  .badge-blue { background: #1e3a5f; color: var(--blue); }
  .badge-gray { background: var(--border); color: var(--muted); }
  .mode-bar { display: flex; gap: .5rem; flex-wrap: wrap; margin-bottom: 1rem; }
  .mode-btn { padding: .4rem .9rem; border-radius: .5rem; border: 1px solid var(--border); background: var(--card); color: var(--muted); cursor: pointer; font-size: .85rem; transition: all .15s; }
  .mode-btn.active { border-color: var(--green); color: var(--green); background: #064e3b; }
  .reason-card { background: var(--card); border: 1px solid var(--border); border-radius: .75rem; padding: 1rem; margin-bottom: 1rem; }
  .reason-card p { font-size: .85rem; color: var(--muted); }
  .section-title { font-size: .8rem; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); margin-bottom: .5rem; }
  .updated { font-size: .7rem; color: var(--border); text-align: right; margin-top: .5rem; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); animation: pulse 2s infinite; display: inline-block; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
</style>
</head>
<body>
<h1><span class="dot"></span> EMS Dashboard</h1>

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
  <div class="card">
    <div class="card-label">Solar</div>
    <div class="card-value" id="solar">--</div>
    <div class="card-sub">W production</div>
  </div>
  <div class="card">
    <div class="card-label">Grid</div>
    <div class="card-value" id="grid">--</div>
    <div class="card-sub">W (+ import)</div>
  </div>
  <div class="card">
    <div class="card-label">Solar surplus</div>
    <div class="card-value" id="surplus">--</div>
    <div class="card-sub">W available</div>
  </div>
  <div class="card">
    <div class="card-label">Battery SOC</div>
    <div class="card-value" id="batSoc">--</div>
    <div class="card-sub">%</div>
  </div>
  <div class="card">
    <div class="card-label">EV SOC</div>
    <div class="card-value" id="evSoc">--</div>
    <div class="card-sub">%</div>
  </div>
  <div class="card">
    <div class="card-label">Tariff</div>
    <div class="card-value" id="tariff">--</div>
    <div class="card-sub">EUR/kWh</div>
  </div>
</div>

<div class="section-title">Decisions</div>
<div class="grid">
  <div class="card">
    <div class="card-label">Battery</div>
    <div id="batDecision"><span class="badge badge-gray">--</span></div>
  </div>
  <div class="card">
    <div class="card-label">EV Charger</div>
    <div id="evDecision"><span class="badge badge-gray">--</span></div>
  </div>
</div>

<div class="reason-card">
  <div class="card-label">Last decision reason</div>
  <p id="reason">--</p>
</div>

<div class="updated" id="updated"></div>

<script>
const BASE = window.location.pathname.replace(/[/]$/, "");

function badge(val, map) {
  const cfg = map[val] || { cls: "badge-gray", label: val || "--" };
  return `<span class="badge badge-${cfg.cls}">${cfg.label}</span>`;
}

const BAT_MAP = {
  charge:    { cls: "green",  label: "Charging" },
  discharge: { cls: "yellow", label: "Discharging" },
  standby:   { cls: "blue",   label: "Standby" },
  idle:      { cls: "gray",   label: "Idle" },
};
const EV_MAP = {
  charge: { cls: "green", label: "Charging" },
  pause:  { cls: "gray",  label: "Paused" },
};

async function refresh() {
  try {
    const r = await fetch(BASE + "/api/state");
    const d = await r.json();
    document.getElementById("solar").textContent = d.solar_w ?? "--";
    document.getElementById("grid").textContent = d.grid_w ?? "--";
    document.getElementById("surplus").textContent = d.solar_surplus_w ?? "--";
    document.getElementById("batSoc").textContent = d.battery_soc != null ? d.battery_soc + "%" : "--";
    document.getElementById("evSoc").textContent = d.ev_soc != null ? d.ev_soc + "%" : "--";
    document.getElementById("tariff").textContent = d.tariff != null ? d.tariff.toFixed(3) : "--";
    document.getElementById("batDecision").innerHTML = badge(d.battery, BAT_MAP);
    document.getElementById("evDecision").innerHTML = badge(d.ev, EV_MAP);
    document.getElementById("reason").textContent = d.reason || "--";
    document.getElementById("updated").textContent = "Updated: " + (d.updated_at || "--");
    // Update mode buttons
    document.querySelectorAll(".mode-btn").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.mode === d.mode);
    });
  } catch(e) { console.error(e); }
}

document.querySelectorAll(".mode-btn").forEach(btn => {
  btn.addEventListener("click", async () => {
    await fetch(BASE + "/api/mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: btn.dataset.mode }),
    });
    await refresh();
  });
});

refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""
