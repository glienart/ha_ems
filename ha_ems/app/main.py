"""
HA EMS Add-on -- FastAPI application.
"""
from __future__ import annotations

import asyncio

import httpx
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import ha_client, settings as settings_module
from .energy_html import ENERGY_HTML
from .energy_log import EnergyLogger
from .epex import fetch_prices, resolve_zone
from .optimizer import EmsOptimizer, EmsSnapshot, EvSnapshot
from .forecast import fetch_solar_forecast, ConsumptionHistory, SolarCalibration
from .scheduler import build_schedule, current_scheduled_action
from .settings import EmsSettings

logging.basicConfig(level=logging.INFO)
_LOGGER = logging.getLogger(__name__)

_settings: EmsSettings = settings_module.load()
_optimizer = EmsOptimizer()
_last_state: dict = {}
_loop_task: Optional[asyncio.Task] = None
_epex_data: dict = {}
_epex_task: Optional[asyncio.Task] = None
_solar_forecast: dict = {}
_solar_fetched_at: Optional[datetime] = None
_schedule: list = []
_schedule_built_at: Optional[str] = None

# Forecast.Solar free tier is heavily rate-limited (~12 calls/day), so we cache
# the forecast and only refresh it every few hours instead of on every schedule
# rebuild (which runs every 30 min).
SOLAR_REFRESH_INTERVAL = timedelta(hours=6)
_consumption_history: ConsumptionHistory = ConsumptionHistory()
_solar_calib: SolarCalibration = SolarCalibration()
_energy_logger: EnergyLogger = EnergyLogger()
_schedule_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _loop_task, _epex_task, _schedule_task
    settings_module.save_runtime(_settings)
    _LOGGER.info("Settings persisted to disk on startup")
    _loop_task = asyncio.create_task(ems_loop())
    _LOGGER.info("EMS optimizer loop started")
    if _settings.epex_token:
        _epex_task = asyncio.create_task(epex_loop())
        _LOGGER.info("EPEX price loop started (zone %s)", _settings.epex_zone)
    _schedule_task = asyncio.create_task(schedule_loop())
    _LOGGER.info("24h schedule loop started")
    yield
    if _loop_task:
        _loop_task.cancel()
    if _epex_task:
        _epex_task.cancel()
    if _schedule_task:
        _schedule_task.cancel()
    # Flush state to disk so nothing is lost on shutdown/restart
    try:
        _consumption_history.save()
        _solar_calib.save()
        _energy_logger.flush()
    except Exception as exc:
        _LOGGER.error("Shutdown flush error: %s", exc)
    _LOGGER.info("EMS loops stopped")


app = FastAPI(title="HA EMS", lifespan=lifespan)


async def ems_loop():
    while True:
        try:
            await run_optimizer()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            _LOGGER.error("Optimizer loop error: %s", exc)
        await asyncio.sleep(_settings.update_interval)


async def epex_loop():
    global _epex_data
    while True:
        try:
            data = await fetch_prices(resolve_zone(_settings.epex_zone), _settings.epex_token)
            if data:
                _epex_data = data
                await _publish_epex(data)
                _LOGGER.info("EPEX price updated: %.4f EUR/kWh (zone %s)",
                             data.get("current_price") or 0, _settings.epex_zone)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            _LOGGER.error("EPEX loop error: %s", exc)
        await asyncio.sleep(15 * 60)


async def rebuild_schedule() -> None:
    """Rebuild the 24h battery schedule from forecasts + EPEX prices."""
    global _solar_forecast, _solar_fetched_at, _schedule, _schedule_built_at
    s = _settings
    if s.panel_kwp > 0:
        now = datetime.now()
        stale = (
            _solar_fetched_at is None
            or not _solar_forecast
            or (now - _solar_fetched_at) >= SOLAR_REFRESH_INTERVAL
        )
        if stale:
            try:
                fc = await fetch_solar_forecast(
                    s.latitude, s.longitude, s.panel_tilt, s.panel_azimuth, s.panel_kwp
                )
                if fc:  # keep the previous forecast on empty/error (e.g. 429)
                    _solar_forecast = fc
                    _solar_fetched_at = now
            except Exception as exc:
                _LOGGER.error("Solar forecast error: %s", exc)
    if not _epex_data:
        _LOGGER.debug("No EPEX data — skipping schedule build")
        return
    all_epex = _epex_data.get("prices_today", []) + _epex_data.get("prices_tomorrow", [])
    if not all_epex:
        return
    buy_prices  = [{**p, "price_eur_kwh": p["price_eur_kwh"]*s.tariff_a_consumption+s.tariff_b_consumption} for p in all_epex]
    sell_prices = [{**p, "price_eur_kwh": p["price_eur_kwh"]*s.tariff_a_injection+s.tariff_b_injection} for p in all_epex]
    bat_soc = await ha_client.get_float(s.battery_soc_sensor, default=50.0) or 50.0
    # Keep _solar_forecast raw (for learning); feed the house-calibrated copy to the planner.
    calibrated_solar = _solar_calib.apply(_solar_forecast) if _solar_forecast else {}
    try:
        _schedule = build_schedule(
            now=datetime.now(),
            solar_forecast=calibrated_solar,
            consumption_forecast=_consumption_history.forecast_next_24h(datetime.now()),
            epex_buy_prices=buy_prices,
            epex_sell_prices=sell_prices,
            battery_soc_pct=bat_soc,
            battery_capacity_kwh=s.battery_capacity_kwh,
            battery_min_soc=s.battery_min_soc,
            battery_max_soc=s.battery_max_soc,
            battery_max_charge_kw=s.battery_max_charge_w / 1000,
            battery_max_discharge_kw=s.battery_max_discharge_w / 1000,
            n_cheap_slots=s.cheap_lookahead_slots,
        )
        _schedule_built_at = datetime.now().isoformat()
        _LOGGER.info("24h schedule built: %d slots", len(_schedule))
    except Exception as exc:
        _LOGGER.error("Schedule build error: %s", exc)


async def schedule_loop():
    """Rebuild the 24h schedule every 30 minutes."""
    await asyncio.sleep(15)  # brief startup delay for EPEX data
    while True:
        try:
            await rebuild_schedule()
            _consumption_history.save()  # persist rolling history each cycle
            _solar_calib.save()          # persist learned solar correction factors
            _energy_logger.flush()       # ensure energy log is on disk
        except asyncio.CancelledError:
            break
        except Exception as exc:
            _LOGGER.error("Schedule loop error: %s", exc)
        await asyncio.sleep(30 * 60)


async def _publish_epex(data: dict) -> None:
    def _fmt(v):
        return str(round(v, 4)) if v is not None else "unavailable"

    sensors = {
        "sensor.ha_ems_epex_current_price":   (data.get("current_price"),   "EPEX Current Price",   "EUR/kWh"),
        "sensor.ha_ems_epex_next_slot_price":  (data.get("next_slot_price"), "EPEX Next Slot Price", "EUR/kWh"),
        "sensor.ha_ems_epex_today_min":        (data.get("today_min"),       "EPEX Today Min",       "EUR/kWh"),
        "sensor.ha_ems_epex_today_max":        (data.get("today_max"),       "EPEX Today Max",       "EUR/kWh"),
        "sensor.ha_ems_epex_today_avg":        (data.get("today_avg"),       "EPEX Today Avg",       "EUR/kWh"),
        "sensor.ha_ems_epex_tomorrow_min":     (data.get("tomorrow_min"),    "EPEX Tomorrow Min",    "EUR/kWh"),
        "sensor.ha_ems_epex_tomorrow_max":     (data.get("tomorrow_max"),    "EPEX Tomorrow Max",    "EUR/kWh"),
    }
    for entity_id, (value, name, unit) in sensors.items():
        await ha_client.set_entity_state(entity_id, _fmt(value),
            {"friendly_name": name, "unit_of_measurement": unit,
             "icon": "mdi:currency-eur", "device_class": "monetary", "state_class": "measurement"})
    if data.get("prices_today"):
        await ha_client.set_entity_state(
            "sensor.ha_ems_epex_current_price", _fmt(data.get("current_price")),
            {"friendly_name": "EPEX Current Price", "unit_of_measurement": "EUR/kWh",
             "icon": "mdi:currency-eur", "device_class": "monetary", "state_class": "measurement",
             "zone": _settings.epex_zone, "slot_minutes": data.get("slot_minutes"),
             "today_min": data.get("today_min"), "today_max": data.get("today_max"),
             "today_avg": data.get("today_avg"), "tomorrow_min": data.get("tomorrow_min"),
             "tomorrow_max": data.get("tomorrow_max"),
             "prices_today": data.get("prices_today", []),
             "prices_tomorrow": data.get("prices_tomorrow", [])})


async def run_optimizer():
    global _last_state
    s = _settings

    solar_w   = await ha_client.get_float(s.solar_power_sensor)
    grid_w    = await ha_client.get_float(s.grid_power_sensor)
    house_w   = await ha_client.get_float(s.house_power_sensor) if s.house_power_sensor else None
    bat_soc   = await ha_client.get_float(s.battery_soc_sensor, default=50.0)
    bat_power = await ha_client.get_float(s.battery_power_sensor) if s.battery_power_sensor else None
    tariff    = await ha_client.get_float(s.tariff_sensor) if s.tariff_sensor else None

    if _epex_data and _epex_data.get("current_price") is not None:
        tariff = _epex_data["current_price"]

    epex_raw = tariff
    if tariff is not None:
        effective_consumption = tariff * s.tariff_a_consumption + s.tariff_b_consumption
        effective_injection   = tariff * s.tariff_a_injection   + s.tariff_b_injection
    else:
        effective_consumption = None
        effective_injection   = None

    ev_snapshots: list[EvSnapshot] = []
    for ev_cfg in s.evs:
        soc_sensor = ev_cfg.get("soc_sensor", "")
        charger_sw = ev_cfg.get("charger_switch", "")
        ev_soc = await ha_client.get_float(soc_sensor) if soc_sensor else None
        ev_on  = await ha_client.get_bool(charger_sw)  if charger_sw  else False
        connected = ev_on or (ev_soc is not None and ev_soc < 100)
        ev_snapshots.append(EvSnapshot(
            name=ev_cfg.get("name", "EV"),
            charger_switch=charger_sw,
            soc_pct=ev_soc,
            target_soc=float(ev_cfg.get("target_soc", 80)),
            departure_time=ev_cfg.get("departure_time", "07:00"),
            max_charge_w=float(ev_cfg.get("max_charge_w", 7400)),
            capacity_kwh=float(ev_cfg.get("capacity_kwh", 40)),
            connected=connected,
        ))

    # Use house sensor directly; no calculation
    _eff_house = house_w if house_w is not None else None

    _energy_logger.record(grid_w, effective_consumption, effective_injection, _settings.update_interval, house_w=_eff_house)
    if _eff_house is not None and _eff_house > 0:
        _consumption_history.record(datetime.now(), _eff_house)

    # Solar self-calibration: learn how this house's real PV compares to the
    # generic Forecast.Solar prediction for the current hour.
    if s.panel_kwp > 0 and s.solar_power_sensor:
        _now = datetime.now()
        _solar_calib.observe(_now, solar_w, _solar_forecast.get(_now.strftime("%Y-%m-%dT%H:00")))

    # Get scheduled action for this hour from 24h plan
    _sched_slot = current_scheduled_action(_schedule, datetime.now())
    _sched_bat  = _sched_slot.battery_action if _sched_slot else None

    snap = EmsSnapshot(
        solar_power_w=solar_w,
        grid_power_w=grid_w,
        house_power_w=house_w,
        battery_soc_pct=bat_soc,
        battery_min_soc=s.battery_min_soc,
        battery_max_soc=s.battery_max_soc,
        battery_max_charge_w=s.battery_max_charge_w,
        battery_max_discharge_w=s.battery_max_discharge_w,
        evs=ev_snapshots,
        tariff_eur_kwh=effective_consumption,
        cheap_threshold=s.cheap_threshold,
        expensive_threshold=s.expensive_threshold,
        cheap_hysteresis=s.cheap_hysteresis,
        expensive_hysteresis=s.expensive_hysteresis,
        cheap_lookahead_slots=s.cheap_lookahead_slots,
        epex_prices_today=_epex_data.get("prices_today", []) if _epex_data else [],
        mode=s.mode,
        now=datetime.now(),
        scheduled_battery=_sched_bat,
    )

    decision = _optimizer.decide(snap)

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

    for ev_dec in decision.ev_decisions:
        sw = ev_dec.get("charger_switch", "")
        if not sw:
            continue
        if ev_dec["decision"] == "charge":
            await ha_client.turn_on(sw)
        else:
            await ha_client.turn_off(sw)

    await ha_client.set_entity_state("sensor.ha_ems_mode", s.mode,
        {"friendly_name": "EMS Mode", "icon": "mdi:tune"})
    await ha_client.set_entity_state("sensor.ha_ems_battery_decision", bat,
        {"friendly_name": "EMS Battery Decision", "icon": "mdi:battery-charging"})
    await ha_client.set_entity_state("sensor.ha_ems_solar_surplus", str(round(decision.solar_surplus_w)),
        {"friendly_name": "EMS Solar Surplus", "unit_of_measurement": "W", "icon": "mdi:solar-power"})
    await ha_client.set_entity_state("sensor.ha_ems_reason", decision.reason,
        {"friendly_name": "EMS Last Reason"})

    for ev_snap, ev_dec in zip(ev_snapshots, decision.ev_decisions):
        safe_name = ev_snap.name.lower().replace(" ", "_").replace("-", "_")
        await ha_client.set_entity_state(
            f"sensor.ha_ems_ev_{safe_name}_decision", ev_dec["decision"],
            {"friendly_name": f"EMS {ev_snap.name} Decision", "icon": "mdi:car-electric"})

    ev_state_list = []
    for ev_snap, ev_dec in zip(ev_snapshots, decision.ev_decisions):
        ev_state_list.append({
            "name": ev_snap.name,
            "decision": ev_dec["decision"],
            "soc": round(ev_snap.soc_pct) if ev_snap.soc_pct is not None else None,
            "connected": ev_snap.connected,
        })

    _last_state = {
        "mode": s.mode,
        "battery": bat,
        "evs": ev_state_list,
        "solar_surplus_w": round(decision.solar_surplus_w),
        "net_power_w": round(decision.net_power_w),
        "solar_w": round(solar_w),
        "grid_w": round(grid_w),
        "battery_soc": round(bat_soc),
        "tariff": round(effective_consumption, 4) if effective_consumption is not None else None,
        "tariff_injection": round(effective_injection, 4) if effective_injection is not None else None,
        "epex_raw": round(epex_raw, 4) if epex_raw is not None else None,
        "battery_w": round(bat_power) if bat_power is not None else None,
        "house_w": round(house_w) if house_w is not None else None,
        "epex_price": _epex_data.get("current_price") if _epex_data else None,
        "reason": decision.reason,
        "updated_at": datetime.now().isoformat(),
    }
    _LOGGER.info("EMS: %s", decision.reason)


@app.get("/api/state")
async def api_state():
    return JSONResponse(_last_state)


@app.get("/chart.js")
async def serve_chartjs():
    return FileResponse("/app/chart.umd.min.js", media_type="application/javascript")


@app.get("/api/epex")
async def api_epex():
    if not _epex_data:
        return JSONResponse({"error": "No EPEX data -- check token and zone in settings"})
    data = dict(_epex_data)
    data["zone"] = _settings.epex_zone  # so the dashboard can show the active zone
    return JSONResponse(data)


@app.get("/api/energy/history")
async def api_energy_history(period: str = "hourly", date: str = ""):
    return JSONResponse(_energy_logger.get_history(period, date or None))


@app.get("/api/forecast")
async def api_forecast():
    return JSONResponse({
        "schedule": [
            {
                "hour": s.hour.isoformat(),
                "hour_label": s.hour.strftime("%H:00"),
                "solar_w": round(s.solar_forecast_w),
                "consumption_w": round(s.consumption_forecast_w),
                "buy_price": round(s.epex_buy_price, 4) if s.epex_buy_price is not None else None,
                "battery_action": s.battery_action,
                "battery_kw": s.battery_kw,
                "reason": s.reason,
            }
            for s in _schedule
        ],
        "built_at": _schedule_built_at,
        "has_solar_forecast": bool(_solar_forecast),
        "has_history": _consumption_history.has_enough_data,
        "solar_calibration": {
            "mean_factor": _solar_calib.mean_factor,
            "hours_learned": _solar_calib.hours_learned,
        },
    })


@app.get("/api/power_history")
async def api_power_history():
    now_local = datetime.now()
    start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    roles = {
        "solar":   _settings.solar_power_sensor,
        "grid":    _settings.grid_power_sensor,
        "battery": _settings.battery_power_sensor,
        "house":   _settings.house_power_sensor,
    }
    id_to_role = {v: k for k, v in roles.items() if v}
    sensors = list(id_to_role.keys())
    if not sensors:
        return JSONResponse({"series": {}})

    url = f"{ha_client.HA_API}/history/period/{start.isoformat()}"
    params = {"filter_entity_id": ",".join(sensors), "minimal_response": "true",
              "significant_changes_only": "false"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, headers=ha_client._headers(), params=params)
            if r.status_code != 200:
                return JSONResponse({"series": {}})
            data = r.json()
    except Exception as exc:
        _LOGGER.error("power_history error: %s", exc)
        return JSONResponse({"series": {}})

    series: dict[str, list] = {}
    for entity_history in data:
        if not entity_history:
            continue
        entity_id = entity_history[0].get("entity_id", "")
        role = id_to_role.get(entity_id)
        if not role:
            continue
        points = []
        for state in entity_history:
            raw = state.get("state") or state.get("s", "")
            ts  = state.get("last_changed") or state.get("lc") or state.get("lu") or ""
            try:
                val = float(raw)
                if isinstance(ts, (int, float)):
                    ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                points.append([ts, round(val, 1)])
            except (ValueError, TypeError):
                pass
        if points:
            series[role] = points
    return JSONResponse({"series": series})


@app.get("/api/settings")
async def api_get_settings():
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
    evs: Optional[list] = None
    tariff_sensor: Optional[str] = None
    tariff_a_consumption: Optional[float] = None
    tariff_b_consumption: Optional[float] = None
    tariff_a_injection: Optional[float] = None
    tariff_b_injection: Optional[float] = None
    cheap_threshold: Optional[float] = None
    expensive_threshold: Optional[float] = None
    cheap_hysteresis: Optional[float] = None
    expensive_hysteresis: Optional[float] = None
    cheap_lookahead_slots: Optional[int] = None
    update_interval: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    panel_kwp: Optional[float] = None
    panel_tilt: Optional[int] = None
    panel_azimuth: Optional[int] = None
    battery_capacity_kwh: Optional[float] = None


@app.post("/api/settings")
async def api_update_settings(body: SettingsUpdate):
    global _settings, _loop_task, _solar_fetched_at
    data = body.model_dump(exclude_none=True)
    if "evs" in data:
        _settings.evs = data.pop("evs")
    for k, v in data.items():
        if hasattr(_settings, k):
            setattr(_settings, k, v)
    settings_module.save_runtime(_settings)
    if _loop_task and "update_interval" in data:
        _loop_task.cancel()
        _loop_task = asyncio.create_task(ems_loop())
    if any(k in data for k in ("panel_kwp","latitude","longitude","panel_tilt","panel_azimuth","battery_capacity_kwh")):
        _solar_fetched_at = None  # invalidate cache so the rebuild refetches solar
        asyncio.create_task(rebuild_schedule())
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


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(DASHBOARD_HTML)


@app.get("/energy", response_class=HTMLResponse)
async def energy_dashboard():
    return HTMLResponse(ENERGY_HTML)


@app.get("/api/entities")
async def api_entities(device_class: str = ""):
    url = f"{ha_client.HA_API}/states"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
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
            entities.append({"entity_id": eid,
                             "friendly_name": attrs.get("friendly_name", eid),
                             "state": s.get("state", ""),
                             "device_class": dc,
                             "unit": attrs.get("unit_of_measurement", "")})
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
<script src="chart.js"></script>
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
  nav{display:flex;justify-content:space-between;align-items:center;margin-bottom:1.25rem;border-bottom:1px solid var(--border)}
  .tabs{display:flex;gap:.25rem}
  .nav-btn{padding:.6rem .9rem;margin-bottom:-1px;border:none;border-bottom:2px solid transparent;background:none;color:var(--muted);cursor:pointer;font-size:.9rem;font-weight:500;border-radius:0}
  .nav-btn:hover{color:var(--text)}
  .nav-btn.active{color:var(--accent);border-bottom-color:var(--accent)}
  .nav-settings{font-size:1.1rem;padding:.3rem .6rem;margin:0;border:1px solid var(--border)!important;border-radius:.5rem}
  .nav-settings.active{color:var(--accent)}
  .page{display:none}.page.active{display:block}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.75rem;margin-bottom:1rem}
  .card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:1rem;box-shadow:0 1px 3px rgba(0,0,0,.06)}
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
  .combo{position:relative}
  .combo-input{width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:.4rem .6rem;border-radius:.4rem;font-size:.82rem}
  .combo-input:focus{outline:none;border-color:var(--accent)}
  .combo-list{position:absolute;top:100%;left:0;right:0;background:var(--card);border:1px solid var(--border);border-radius:.4rem;max-height:190px;overflow-y:auto;z-index:300;display:none;list-style:none;padding:0;margin:2px 0 0;box-shadow:0 4px 16px rgba(0,0,0,.15)}
  .combo-list li{padding:.3rem .5rem;cursor:pointer;display:flex;align-items:center;gap:.4rem;font-size:.78rem;border-bottom:1px solid var(--border)}
  .combo-list li:last-child{border-bottom:none}
  .combo-list li:hover,.combo-list li.hl{background:var(--border)}
  .cl-name{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text)}
  .cl-unit{font-size:.64rem;padding:.1rem .3rem;border-radius:.25rem;background:var(--border);color:var(--muted);white-space:nowrap;flex-shrink:0}
  .cl-sub{font-size:.68rem;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block;margin-top:.1rem}
  .cl-none{color:var(--muted);font-style:italic}
  .field{margin-bottom:.6rem}
  .field label{display:block;font-size:.78rem;color:var(--muted);margin-bottom:.2rem}
  .field select,.field input{width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:.4rem .6rem;border-radius:.4rem;font-size:.82rem}
  .field select:focus,.field input:focus{outline:none;border-color:var(--accent)}
  .save-btn{background:var(--accent);color:#fff;border:none;padding:.4rem 1rem;border-radius:.5rem;cursor:pointer;font-size:.82rem;font-weight:600;margin-top:.25rem}
  .save-btn:hover{opacity:.9}
  .toast{position:fixed;bottom:1rem;right:1rem;background:var(--accent);color:#fff;padding:.5rem 1rem;border-radius:.5rem;font-size:.85rem;display:none;z-index:999}
  .ha-e-wrap{position:relative;height:400px;max-width:400px;margin:0 auto .75rem}
  .ha-e-node{position:absolute;display:flex;flex-direction:column;align-items:center;gap:.25rem;z-index:1}
  .ha-e-node.solar{top:0;left:calc(50% - 40px)}
  .ha-e-node.grid{top:calc(50% - 40px);left:0}
  .ha-e-node.home{top:calc(50% - 40px);right:0}
  .ha-e-node.battery{bottom:0;left:calc(50% - 40px)}
  .ha-e-circle{width:80px;height:80px;border-radius:50%;border:2px solid var(--border);background:var(--card);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1px;font-size:.72rem;box-shadow:0 1px 4px rgba(0,0,0,.08)}
  .ha-e-circle.c-solar{border-color:#ff9800;color:#ff9800}
  .ha-e-circle.c-grid{border-color:#488fc2;color:#488fc2}
  .ha-e-circle.c-home{border-color:var(--accent);color:var(--accent);border-width:3px}
  .ha-e-circle.c-battery{border-color:#4db6ac;color:#4db6ac}
  .ha-e-val{font-size:.82rem;font-weight:700;color:var(--text)}
  .ha-e-sub{font-size:.65rem;color:var(--muted)}
  .ha-e-label{font-size:.68rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
  .ha-e-node.solar{flex-direction:column}
  .ha-e-node.solar .ha-e-label{order:-1}
  .ha-e-node.grid .ha-e-label,.ha-e-node.battery .ha-e-label{order:1}
  .ha-e-node.home{align-items:center}
  .ha-e-node.home .ha-e-label{order:1}
  .ha-e-lines{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:0;overflow:visible}
  .ha-e-path{fill:none;stroke-width:2;stroke:transparent;vector-effect:non-scaling-stroke;transition:stroke 0.4s}
  .d-solar{fill:#ff9800}.d-return{fill:#ff9800}.d-grid{fill:#488fc2}
  .d-bat-home{fill:#4db6ac}.d-bat-grid{fill:#4db6ac}
  .epex-pills{display:grid;grid-template-columns:repeat(4,1fr);gap:.5rem;margin-bottom:.75rem}
  @media(max-width:600px){.epex-pills{grid-template-columns:repeat(2,1fr)}}
  @media(max-width:600px){.epex-pills .pill-val{font-size:.8rem}}
  .pill{background:var(--bg);border:1px solid var(--border);border-radius:.5rem;padding:.4rem .6rem;text-align:center}
  .pill-label{font-size:.62rem;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
  .pill-val{font-size:.95rem;font-weight:700;margin-top:.1rem}
  .day-toggle{display:flex;gap:.4rem}
  .day-btn{padding:.2rem .6rem;border-radius:.4rem;border:1px solid var(--border);background:none;color:var(--muted);cursor:pointer;font-size:.75rem}
  .day-btn.active{background:var(--accent);border-color:var(--accent);color:#fff}
  .hist-controls{display:flex;justify-content:space-between;align-items:center;gap:.75rem;flex-wrap:wrap;margin-bottom:.75rem}
  .hist-nav{display:flex;align-items:center;gap:.4rem}
  .hist-date{background:var(--bg);border:1px solid var(--border);color:var(--text);padding:.3rem .5rem;border-radius:.4rem;font-size:.8rem}
  .chart-wrap{position:relative;height:160px;margin-bottom:.5rem}
  .price-table{width:100%;border-collapse:collapse;font-size:.75rem}
  .price-table th{color:var(--muted);font-size:.62rem;text-transform:uppercase;padding:.25rem .4rem;border-bottom:1px solid var(--border);text-align:left;position:sticky;top:0;background:var(--card)}
  .price-table td{padding:.25rem .4rem;border-bottom:1px solid var(--border)}
  .price-table tr.cur td{background:#d1fae5;color:#065f46;font-weight:700}
  @media(prefers-color-scheme:dark){.price-table tr.cur td{background:#064e3b;color:#10b981}}
  .pbar{height:5px;border-radius:2px;margin-top:2px}
  .energy-layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,300px);gap:.75rem}
  .energy-layout > *{min-width:0;overflow:hidden}
  @media(max-width:700px){.energy-layout{grid-template-columns:1fr}}
  .dual-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.75rem;margin-bottom:.75rem}
  .dual-grid > *{min-width:0;overflow:hidden}
  @media(max-width:760px){.dual-grid{grid-template-columns:1fr}}
  .flow-chart-grid{display:grid;grid-template-columns:minmax(0,300px) minmax(0,1fr);gap:.75rem;margin-bottom:.75rem;align-items:start}
  .flow-chart-grid > *{min-width:0;overflow:hidden}
  @media(max-width:720px){.flow-chart-grid{grid-template-columns:1fr}}
  .ev-fields-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:.4rem}
  @media(max-width:600px){.ev-fields-grid{grid-template-columns:repeat(2,1fr)}}
  .no-epex{font-size:.82rem;color:var(--muted);text-align:center;padding:1.25rem 0;line-height:1.7}
  .no-epex a{color:var(--accent);text-decoration:none;font-weight:500}
  .no-epex a:hover{text-decoration:underline}
</style>
</head>
<body>
<nav>
  <div class="tabs">
    <button class="nav-btn active" data-page="energy" onclick="showPage('energy')">Energy</button>
    <button class="nav-btn" data-page="analyse" onclick="showPage('analyse')">Analyse</button>
  </div>
  <button class="nav-btn nav-settings" data-page="settings" id="btn-settings" onclick="showPage('settings')" title="Settings">&#x270E;</button>
</nav>

<!-- SETTINGS PAGE -->
<div id="page-settings" class="page">
  <div class="settings-grid">

    <div class="settings-group">
      <div class="sg-head"><h3>Power sensors</h3><button class="pencil-btn" onclick="toggleEdit('sg-power')" title="Edit">&#x270E;</button></div>
      <div id="sg-power-view">
        <div class="sg-row"><span class="sg-key">Solar</span><span id="v_solar_power_sensor" class="sg-val">&#8212;</span></div>
        <div class="sg-row"><span class="sg-key">Grid</span><span id="v_grid_power_sensor" class="sg-val">&#8212;</span></div>
        <div class="sg-row"><span class="sg-key">House</span><span id="v_house_power_sensor" class="sg-val">&#8212;</span></div>
        <div class="sg-row"><span class="sg-key">Battery power</span><span id="v_battery_power_sensor" class="sg-val">&#8212;</span></div>
      </div>
      <div id="sg-power-edit" class="sg-edit" style="display:none">
        <div class="field"><label>Solar production (W)</label><div class="combo"><input class="combo-input" id="s_solar_power_sensor" placeholder="Search W sensor..." autocomplete="off"><ul class="combo-list" id="sl_solar_power_sensor"></ul></div></div>
        <div class="field"><label>Grid power (W, + = import)</label><div class="combo"><input class="combo-input" id="s_grid_power_sensor" placeholder="Search W sensor..." autocomplete="off"><ul class="combo-list" id="sl_grid_power_sensor"></ul></div></div>
        <div class="field"><label>House consumption (W, optional)</label><div class="combo"><input class="combo-input" id="s_house_power_sensor" placeholder="Search W sensor..." autocomplete="off"><ul class="combo-list" id="sl_house_power_sensor"></ul></div></div>
        <div class="field"><label>Battery power (W, + charge / - discharge)</label><div class="combo"><input class="combo-input" id="s_battery_power_sensor" placeholder="Search W sensor..." autocomplete="off"><ul class="combo-list" id="sl_battery_power_sensor"></ul></div></div>
        <button class="save-btn" onclick="saveGroup(['solar_power_sensor','grid_power_sensor','house_power_sensor','battery_power_sensor'],'sg-power')">Save</button>
      </div>
    </div>

    <div class="settings-group">
      <div class="sg-head"><h3>Battery</h3><button class="pencil-btn" onclick="toggleEdit('sg-bat')" title="Edit">&#x270E;</button></div>
      <div id="sg-bat-view">
        <div class="sg-row"><span class="sg-key">SOC sensor</span><span id="v_battery_soc_sensor" class="sg-val">&#8212;</span></div>
        <div class="sg-row"><span class="sg-key">Charge switch</span><span id="v_battery_charge_switch" class="sg-val">&#8212;</span></div>
        <div class="sg-row"><span class="sg-key">Discharge switch</span><span id="v_battery_discharge_switch" class="sg-val">&#8212;</span></div>
        <div class="sg-row"><span class="sg-key">Max charge / discharge</span><span id="v_battery_max_charge_w" class="sg-val">&#8212;</span></div>
        <div class="sg-row"><span class="sg-key">SOC range</span><span id="v_battery_soc_range" class="sg-val">&#8212;</span></div>
      </div>
      <div id="sg-bat-edit" class="sg-edit" style="display:none">
        <div class="field"><label>Battery SOC (%)</label><div class="combo"><input class="combo-input" id="s_battery_soc_sensor" placeholder="Search % sensor..." autocomplete="off"><ul class="combo-list" id="sl_battery_soc_sensor"></ul></div></div>
        <div class="field"><label>Charge switch</label><div class="combo"><input class="combo-input" id="s_battery_charge_switch" placeholder="Search switch..." autocomplete="off"><ul class="combo-list" id="sl_battery_charge_switch"></ul></div></div>
        <div class="field"><label>Discharge switch</label><div class="combo"><input class="combo-input" id="s_battery_discharge_switch" placeholder="Search switch..." autocomplete="off"><ul class="combo-list" id="sl_battery_discharge_switch"></ul></div></div>
        <div class="field"><label>Standby switch (optional)</label><div class="combo"><input class="combo-input" id="s_battery_standby_switch" placeholder="Search switch..." autocomplete="off"><ul class="combo-list" id="sl_battery_standby_switch"></ul></div></div>
        <div class="field"><label>Max charge (W)</label><input type="number" id="s_battery_max_charge_w" min="100" max="20000"></div>
        <div class="field"><label>Max discharge (W)</label><input type="number" id="s_battery_max_discharge_w" min="100" max="20000"></div>
        <div class="field"><label>Min SOC (%)</label><input type="number" id="s_battery_min_soc" min="0" max="50"></div>
        <div class="field"><label>Max SOC (%)</label><input type="number" id="s_battery_max_soc" min="50" max="100"></div>
        <button class="save-btn" onclick="saveGroup(['battery_soc_sensor','battery_charge_switch','battery_discharge_switch','battery_standby_switch','battery_max_charge_w','battery_max_discharge_w','battery_min_soc','battery_max_soc'],'sg-bat')">Save</button>
      </div>
    </div>

    <div class="settings-group">
      <div class="sg-head"><h3>EV Fleet</h3><button class="pencil-btn" onclick="toggleEdit('sg-ev')" title="Edit">&#x270E;</button></div>
      <div id="sg-ev-view">
        <div id="ev-fleet-view"></div>
        <div class="sg-row" id="ev-fleet-empty" style="display:none">
          <span class="sg-key" style="color:var(--muted);font-style:italic">No vehicles configured</span>
        </div>
      </div>
      <div id="sg-ev-edit" class="sg-edit" style="display:none">
        <div id="ev-fleet-edit"></div>
        <div style="display:flex;gap:.5rem;margin-top:.5rem">
          <button class="save-btn" style="background:var(--border);color:var(--text)" onclick="addEv()">+ Add vehicle</button>
          <button class="save-btn" onclick="saveEvFleet()">Save fleet</button>
        </div>
      </div>
    </div>

    <div class="settings-group">
      <div class="sg-head"><h3>Tariff &amp; optimizer</h3><button class="pencil-btn" onclick="toggleEdit('sg-tariff')" title="Edit">&#x270E;</button></div>
      <div id="sg-tariff-view">
        <div class="sg-row"><span class="sg-key">Price sensor</span><span id="v_tariff_sensor" class="sg-val">&#8212;</span></div>
        <div class="sg-row"><span class="sg-key">Cheap &lt;</span><span id="v_cheap_threshold" class="sg-val">&#8212;</span></div>
        <div class="sg-row"><span class="sg-key">Expensive &gt;</span><span id="v_expensive_threshold" class="sg-val">&#8212;</span></div>
        <div class="sg-row"><span class="sg-key">Update interval</span><span id="v_update_interval" class="sg-val">&#8212;</span></div>
        <div id="v_axb_block" style="display:none">
          <div class="sg-row"><span class="sg-key">Conso a (&#xD7;EPEX)</span><span id="v_tariff_a_consumption" class="sg-val">&#8212;</span></div>
          <div class="sg-row"><span class="sg-key">Conso b (&#8364;/kWh)</span><span id="v_tariff_b_consumption" class="sg-val">&#8212;</span></div>
          <div class="sg-row"><span class="sg-key">Inject a (&#xD7;EPEX)</span><span id="v_tariff_a_injection" class="sg-val">&#8212;</span></div>
          <div class="sg-row"><span class="sg-key">Inject b (&#8364;/kWh)</span><span id="v_tariff_b_injection" class="sg-val">&#8212;</span></div>
        </div>
        <div class="sg-row"><span class="sg-key">Hysteresis cheap/exp</span><span id="v_cheap_hysteresis" class="sg-val">&#8212;</span></div>
        <div class="sg-row"><span class="sg-key">Look-ahead slots</span><span id="v_cheap_lookahead_slots" class="sg-val">&#8212;</span></div>
      </div>
      <div id="sg-tariff-edit" class="sg-edit" style="display:none">
        <div class="field"><label>Price sensor (EUR/kWh, optional)</label><div class="combo"><input class="combo-input" id="s_tariff_sensor" placeholder="Search price sensor..." autocomplete="off"><ul class="combo-list" id="sl_tariff_sensor"></ul></div></div>
        <div class="field"><label>Cheap threshold (&#8364;/kWh)</label><input type="number" id="s_cheap_threshold" step="0.01" min="0" max="1"></div>
        <div class="field"><label>Expensive threshold (&#8364;/kWh)</label><input type="number" id="s_expensive_threshold" step="0.01" min="0" max="1"></div>
        <div class="field"><label>Update interval (s)</label><input type="number" id="s_update_interval" min="10" max="3600"></div>
        <details id="axb_details" style="margin:.5rem 0">
          <summary style="font-size:.8rem;color:var(--muted);cursor:pointer">Prix effectif = a × EPEX + b <span style="font-size:.75rem">(optionnel, défaut a=1 b=0)</span></summary>
          <div style="margin-top:.5rem">
            <div class="field"><label>Consommation a (multiplicateur EPEX)</label><input type="number" id="s_tariff_a_consumption" step="0.001" min="0" max="10"></div>
            <div class="field"><label>Consommation b (fixe, &#8364;/kWh)</label><input type="number" id="s_tariff_b_consumption" step="0.001" min="-1" max="1"></div>
            <div class="field"><label>Injection a (multiplicateur EPEX)</label><input type="number" id="s_tariff_a_injection" step="0.001" min="0" max="10"></div>
            <div class="field"><label>Injection b (fixe, &#8364;/kWh)</label><input type="number" id="s_tariff_b_injection" step="0.001" min="-1" max="1"></div>
          </div>
        </details>
        <div class="field"><label>Hystérésis cheap (&#8364;/kWh)</label><input type="number" id="s_cheap_hysteresis" step="0.001" min="0" max="0.1"></div>
        <div class="field"><label>Hystérésis expensive (&#8364;/kWh)</label><input type="number" id="s_expensive_hysteresis" step="0.001" min="0" max="0.1"></div>
        <div class="field"><label>Look-ahead slots (N meilleurs slots EPEX)</label><input type="number" id="s_cheap_lookahead_slots" min="0" max="24"></div>
        <button class="save-btn" onclick="saveGroup(['tariff_sensor','cheap_threshold','expensive_threshold','cheap_hysteresis','expensive_hysteresis','cheap_lookahead_slots','update_interval','tariff_a_consumption','tariff_b_consumption','tariff_a_injection','tariff_b_injection'],'sg-tariff')">Save</button>
      </div>
    </div>


    <!-- Forecast & Panel -->
    <div class="settings-group">
      <div class="sg-head"><h3>Forecast &amp; Panel</h3><button class="pencil-btn" onclick="toggleEdit('sg-panel')" title="Edit">&#x270E;</button></div>
      <div id="sg-panel-view">
        <div class="sg-row"><span class="sg-key">Location</span><span id="v_panel_location" class="sg-val">&#8212;</span></div>
        <div class="sg-row"><span class="sg-key">Panel</span><span id="v_panel_spec" class="sg-val">&#8212;</span></div>
        <div class="sg-row"><span class="sg-key">Battery capacity</span><span id="v_battery_capacity_kwh" class="sg-val">&#8212;</span></div>
      </div>
      <div id="sg-panel-edit" class="sg-edit" style="display:none">
        <div class="field"><label>Latitude</label><input type="number" id="s_latitude" step="0.0001"></div>
        <div class="field"><label>Longitude</label><input type="number" id="s_longitude" step="0.0001"></div>
        <div class="field"><label>Panel power (kWp)</label><input type="number" id="s_panel_kwp" step="0.1" min="0"></div>
        <div class="field"><label>Panel tilt (°, 0=horiz)</label><input type="number" id="s_panel_tilt" step="1" min="0" max="90"></div>
        <div class="field"><label>Panel azimuth (° from S: 0=S -90=E 90=W)</label><input type="number" id="s_panel_azimuth" step="1" min="-180" max="180"></div>
        <div class="field"><label>Battery capacity (kWh)</label><input type="number" id="s_battery_capacity_kwh" step="0.5" min="0"></div>
        <button class="save-btn" onclick="saveGroup(['latitude','longitude','panel_kwp','panel_tilt','panel_azimuth','battery_capacity_kwh'],'sg-panel')">Save</button>
      </div>
    </div>

  </div>
</div>

<!-- ENERGY PAGE (operational: mode + live + flow + history) -->
<div id="page-energy" class="page active">
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
    <div id="ev-soc-cards" style="display:contents"></div>
    <div class="card"><div class="card-label">Buy price</div><div class="card-value" id="tariff">--</div><div class="card-sub" id="tariff-sell-sub">&#8364;/kWh</div></div>
  </div>
  <div class="section-title">Decisions</div>
  <div class="grid">
    <div class="card"><div class="card-label">Battery</div><div id="batDecision"><span class="badge badge-gray">--</span></div></div>
    <div id="ev-decision-cards" style="display:contents"></div>
  </div>
  <div class="reason-card"><div class="card-label">Last decision reason</div><p id="reason">--</p></div>
  <!-- Flow + Chart -->
  <div class="flow-chart-grid">
    <div class="card">
      <div class="card-label" style="margin-bottom:.5rem;text-align:center">Live flow</div>
      <div class="ha-e-wrap" style="height:320px">
        <div class="ha-e-node solar">
          <span class="ha-e-label">Solar</span>
          <div class="ha-e-circle c-solar">
            <svg viewBox="0 0 24 24" width="22" height="22"><path fill="#ff9800" d="M11.45,2V5.55L15,3.77L11.45,2M10.45,8L8,10.46L11.75,11.71L10.45,8M2,11.45L3.77,15L5.55,11.45H2M10,2H2V10C2.57,10.17 3.17,10.25 3.77,10.25C7.35,10.26 10.26,7.35 10.27,3.75C10.26,3.16 10.17,2.57 10,2M17,22V16H14L19,7V13H22L17,22Z"/></svg>
            <span class="ha-e-val" id="ev-solar">-- W</span>
          </div>
        </div>
        <div class="ha-e-node grid">
          <div class="ha-e-circle c-grid">
            <svg viewBox="0 0 24 24" width="22" height="22"><path fill="#488fc2" d="M8.28,5.45L6.5,4.55L7.76,2H16.23L17.5,4.55L15.72,5.44L15,4H9L8.28,5.45M18.62,8H14.09L13.3,5H10.7L9.91,8H5.38L4.1,10.55L5.89,11.44L6.62,10H17.38L18.1,11.45L19.89,10.56L18.62,8M17.77,22H15.7L15.46,21.1L12,15.9L8.53,21.1L8.3,22H6.23L9.12,11H11.19L10.83,12.35L12,14.1L13.16,12.35L12.81,11H14.88L17.77,22M11.4,15L10.5,13.65L9.32,18.13L11.4,15M14.68,18.12L13.5,13.64L12.6,15L14.68,18.12Z"/></svg>
            <span class="ha-e-val" id="ev-grid">-- W</span>
            <span class="ha-e-sub" id="ev-grid-dir">--</span>
          </div>
          <span class="ha-e-label">Grid</span>
        </div>
        <div class="ha-e-node home">
          <div class="ha-e-circle c-home">
            <svg viewBox="0 0 24 24" width="22" height="22"><path fill="currentColor" d="M10,20V14H14V20H19V12H22L12,3L2,12H5V20H10Z"/></svg>
            <span class="ha-e-val" id="ev-home">-- W</span>
          </div>
          <span class="ha-e-label">Home</span>
        </div>
        <div class="ha-e-node battery">
          <div class="ha-e-circle c-battery">
            <svg viewBox="0 0 24 24" width="20" height="20"><path fill="#4db6ac" d="M16.67,4H15V2H9V4H7.33A1.33,1.33 0 0,0 6,5.33V20.67C6,21.4 6.6,22 7.33,22H16.67A1.33,1.33 0 0,0 18,20.67V5.33C18,4.6 17.4,4 16.67,4Z"/></svg>
            <span class="ha-e-val" id="ev-bat">-- %</span>
            <span class="ha-e-sub" id="ev-bat-dec">--</span>
          </div>
          <span class="ha-e-label">Battery</span>
        </div>
        <svg class="ha-e-lines" viewBox="0 0 100 100" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">
          <path id="epl-solar"      class="ha-e-path" d="M50,10 C50,35 90,35 90,50"/>
          <path id="epl-return"     class="ha-e-path" d="M50,10 C50,35 10,35 10,50"/>
          <path id="epl-grid"       class="ha-e-path" d="M10,50 H90"/>
          <path id="epl-bat-home"   class="ha-e-path" d="M50,90 C50,65 90,65 90,50"/>
          <path id="epl-bat-grid"   class="ha-e-path" d="M50,90 C50,65 10,65 10,50"/>
          <path id="epl-solar-bat"  class="ha-e-path" d="M50,10 C50,35 50,65 50,90"/>
          <path id="epl-grid-bat"   class="ha-e-path" d="M10,50 C10,65 50,65 50,90"/>
          <circle r="1.8" class="d-solar"     id="edot-solar"     style="display:none"><animateMotion dur="2.8s" repeatCount="indefinite" calcMode="linear"><mpath xlink:href="#epl-solar"/></animateMotion></circle>
          <circle r="1.8" class="d-return"    id="edot-return"    style="display:none"><animateMotion dur="3.2s" repeatCount="indefinite" calcMode="linear"><mpath xlink:href="#epl-return"/></animateMotion></circle>
          <circle r="1.8" class="d-grid"      id="edot-grid"      style="display:none"><animateMotion dur="4s"   repeatCount="indefinite" calcMode="linear"><mpath xlink:href="#epl-grid"/></animateMotion></circle>
          <circle r="1.8" class="d-bat-home"  id="edot-bat-home"  style="display:none"><animateMotion dur="3.5s" repeatCount="indefinite" calcMode="linear"><mpath xlink:href="#epl-bat-home"/></animateMotion></circle>
          <circle r="1.8" class="d-bat-grid"  id="edot-bat-grid"  style="display:none"><animateMotion dur="4s"   repeatCount="indefinite" calcMode="linear"><mpath xlink:href="#epl-bat-grid"/></animateMotion></circle>
          <circle r="1.8" class="d-solar"     id="edot-solar-bat" style="display:none"><animateMotion dur="3s"   repeatCount="indefinite" calcMode="linear"><mpath xlink:href="#epl-solar-bat"/></animateMotion></circle>
          <circle r="1.8" class="d-grid"      id="edot-grid-bat"  style="display:none"><animateMotion dur="3.5s" repeatCount="indefinite" calcMode="linear"><mpath xlink:href="#epl-grid-bat"/></animateMotion></circle>
        </svg>
      </div>
    </div>
    <div class="card">
      <div class="card-label" style="margin-bottom:.5rem">Power sources</div>
      <div style="position:relative;height:340px;width:100%;overflow:hidden">
        <canvas id="power-chart" style="width:100%!important"></canvas>
      </div>
    </div>
  </div>

  <!-- Consommation & Coût (history) -->
  <div class="section-title" style="margin-top:.75rem">Consommation &amp; Co&ucirc;t</div>
  <div class="dual-grid">
    <div class="card">
      <div class="card-label" style="margin-bottom:.5rem">Consommation (kWh)</div>
      <div class="chart-wrap" style="height:320px"><canvas id="kwhChart"></canvas></div>
      <div class="updated" id="kwh-updated"></div>
    </div>
    <div class="card">
      <div class="card-label" style="margin-bottom:.5rem">Prix pay&eacute; (&euro;)</div>
      <div class="chart-wrap" style="height:320px"><canvas id="priceChart"></canvas></div>
      <div class="updated" id="price-updated"></div>
    </div>
  </div>
  <!-- Global history filter — drives both charts above -->
  <div class="hist-controls">
    <div class="day-toggle">
      <button class="day-btn active" data-period="hourly"  onclick="setHistPeriod('hourly',this)">Heure</button>
      <button class="day-btn"        data-period="daily"   onclick="setHistPeriod('daily',this)">Jour</button>
      <button class="day-btn"        data-period="monthly" onclick="setHistPeriod('monthly',this)">Année</button>
    </div>
    <div class="hist-nav">
      <button class="day-btn" onclick="stepHist(-1)" title="Précédent">&#8249;</button>
      <input type="date" id="histDate" class="hist-date" onchange="loadEnergyHistory()">
      <button class="day-btn" onclick="stepHist(1)" title="Suivant">&#8250;</button>
    </div>
  </div>
  <div class="updated" id="updated"></div>
</div>

<!-- ANALYSE PAGE (EPEX market + 24h battery plan) -->
<div id="page-analyse" class="page">
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
          No EPEX data &mdash; add your ENTSO-E token<br>in <strong>Add-on &rarr; Configuration</strong>.<br><br>
          <a href="https://transparency.entsoe.eu/usrm/user/createPublicUser" target="_blank" rel="noopener">&rarr; Get a free ENTSO-E token</a>
        </div>
        <div class="updated" id="ep-zone"></div>
      </div>
    </div>
    <div>
      <div class="card" style="max-height:340px;overflow:hidden">
        <div class="card-label" id="sched-title" style="margin-bottom:.4rem">Schedule &mdash; Today</div>
        <div style="max-height:290px;overflow-y:auto">
          <table class="price-table">
            <thead><tr><th>Time</th><th>€/kWh</th><th></th></tr></thead>
            <tbody id="sched-body"></tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <!-- 24h forecast chart (solar + consumption) -->
  <div class="section-title" style="margin-top:.75rem">Pr&eacute;visions 24h <span id="fc-calib" style="font-size:.7rem;font-weight:400;color:var(--muted);text-transform:none;letter-spacing:0"></span></div>
  <div class="card">
    <div class="chart-wrap" style="height:200px"><canvas id="forecastChart"></canvas></div>
  </div>

  <!-- 24h optimized battery plan (from /api/forecast) -->
  <div class="section-title" style="margin-top:.75rem">Plan batterie 24h <span id="plan-status" style="font-size:.7rem;font-weight:400;color:var(--muted);text-transform:none;letter-spacing:0"></span></div>
  <div class="card">
    <div id="plan-empty" class="no-epex" style="display:none">
      Aucun plan disponible &mdash; n&eacute;cessite les prix EPEX et la configuration panneaux/batterie.
    </div>
    <div id="plan-wrap" style="max-height:360px;overflow-y:auto">
      <table class="price-table">
        <thead><tr><th>Heure</th><th>Action</th><th>kW</th><th>&euro;/kWh</th><th>Raison</th></tr></thead>
        <tbody id="plan-body"></tbody>
      </table>
    </div>
  </div>

</div>

<div class="toast" id="toast">Saved!</div>

<script>
const BASE = window.location.pathname.replace(/[\/]+$/, "");

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
  } catch(e) {}
})();

// Apply the (HA) theme colours to all Chart.js charts, so graphs match the
// dashboard in both light and dark mode instead of using hard-coded greys.
(function(){
  if (typeof Chart === 'undefined') return;
  const css = getComputedStyle(document.documentElement);
  const muted  = css.getPropertyValue('--muted').trim()  || '#6b7280';
  const border = css.getPropertyValue('--border').trim() || 'rgba(128,128,128,0.2)';
  Chart.defaults.color = muted;          // tick labels + legend text
  Chart.defaults.borderColor = border;   // grid lines + axis borders
  Chart.defaults.font.family = 'system-ui, -apple-system, sans-serif';
})();

let _epexData = null, _epexChartInst = null, _epexDay = 'today';

function showPage(name) {
  const valid = { energy: 1, analyse: 1, settings: 1 };
  if (!valid[name]) name = "energy";
  document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
  document.querySelectorAll(".nav-btn").forEach(b => b.classList.toggle("active", b.dataset.page === name));
  const pg = document.getElementById("page-" + name);
  if (pg) pg.classList.add("active");
  // Keep the page in the URL hash so a refresh lands on the same tab.
  if (location.hash !== "#" + name) location.hash = name;
  if (name === "settings") loadSettings();
  if (name === "energy") { loadPowerChart(); loadEnergyHistory(); }
  if (name === "analyse") { if (!_epexData) loadEpex(); loadForecast(); }
}
window.addEventListener("hashchange", () => showPage((location.hash || "").replace("#", "")));

function showToast() {
  const t = document.getElementById("toast");
  t.style.display = "block";
  setTimeout(() => t.style.display = "none", 2500);
}

function badge(val, map) {
  const cfg = map[val] || {cls:"gray", label: val||"--"};
  return '<span class="badge badge-'+cfg.cls+'">'+cfg.label+'</span>';
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
    document.getElementById("tariff").textContent = d.tariff!=null ? d.tariff.toFixed(3) : "--";
    const sellSub = document.getElementById("tariff-sell-sub");
    if (sellSub) sellSub.textContent = d.tariff_injection!=null ? "€/kWh buy · sell "+d.tariff_injection.toFixed(3) : "€/kWh";
    document.getElementById("batDecision").innerHTML = badge(d.battery, BAT_MAP);
    renderEvCards(d.evs || []);
    document.getElementById("reason").textContent  = d.reason || "--";
    document.getElementById("updated").textContent = "Updated: "+(d.updated_at||"--");
    document.querySelectorAll(".mode-btn").forEach(b => b.classList.toggle("active", b.dataset.mode===d.mode));
    updateFlow(d);
  } catch(e) { console.error(e); }
}

function updateFlow(d) {
  const set  = (id,v) => { const el=document.getElementById(id); if(el) el.textContent=v; };
  const show = (id,on) => { const el=document.getElementById(id); if(el) el.style.display = on ? 'inline' : 'none'; };
  const setFlow = (pathId, dotId, active, color) => {
    const p = document.getElementById(pathId);
    const d2 = document.getElementById(dotId);
    if (p)  p.style.stroke  = active ? color : 'transparent';
    if (d2) d2.style.display = active ? 'inline' : 'none';
  };

  const solar   = d.solar_w ?? 0;
  const grid    = d.grid_w  ?? 0;
  const batSoc  = d.battery_soc;
  const batDec  = d.battery || 'idle';
  const batW    = d.battery_w;

  // battery_w: negative = charging, positive = discharging (Solis convention)
  const batDischarging = batW != null ? batW >  50  : batDec === 'discharge';
  const batCharging    = batW != null ? batW < -50  : batDec === 'charge';

  // house_w comes directly from sensor; fallback to energy balance only if sensor not configured
  const homeEst = d.house_w ?? Math.max(0, solar + grid + (batW ?? 0));

  const gridDir = grid > 0 ? 'Import' : grid < 0 ? 'Export' : 'Idle';
  const batSub  = batW!=null ? (batW>50?'↑ '+batW+' W':batW<-50?'↓ '+Math.abs(batW)+' W':'idle') : batDec;

  // Flow diagram nodes
  set('ev-solar',    solar > 0 ? solar+' W' : '0 W');
  set('ev-grid',     Math.abs(grid)+' W');
  set('ev-grid-dir', gridDir);
  set('ev-home',     homeEst > 0 ? Math.round(homeEst)+' W' : '-- W');
  set('ev-bat',      batSoc!=null ? batSoc+'%' : '--');
  set('ev-bat-dec',  batSub);

  // Stat cards on energy tab
  set('ec-solar',    (d.solar_w ?? '--')+' W');
  set('ec-grid',     Math.abs(grid)+' W');
  set('ec-grid-dir', gridDir);
  set('ec-home',     homeEst > 0 ? Math.round(homeEst)+' W' : '-- W');
  set('ec-bat',      batSoc!=null ? batSoc+'%' : '--');
  set('ec-bat-dec',  batSub);

  // Animated flow paths: show path stroke + dot only when flow is active
  // PV→Grid export is orange (solar power), Grid→Home import is blue
  setFlow('epl-solar',    'edot-solar',    solar > 50,                   '#ff9800');
  setFlow('epl-return',   'edot-return',   grid < -50,                   '#ff9800');
  setFlow('epl-grid',     'edot-grid',     grid >  50,                   '#488fc2');
  setFlow('epl-bat-home', 'edot-bat-home', batDischarging,               '#4db6ac');
  setFlow('epl-bat-grid', 'edot-bat-grid', batDischarging && grid < -50, '#4db6ac');
  setFlow('epl-solar-bat', 'edot-solar-bat', batCharging,                '#ff9800');
  setFlow('epl-grid-bat',  'edot-grid-bat',  batCharging && grid > 50,   '#488fc2');
}

document.querySelectorAll(".mode-btn").forEach(btn => {
  btn.addEventListener("click", async () => {
    await fetch(BASE+"/api/mode", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:btn.dataset.mode})});
    await refresh();
  });
});

// Settings
const FIELD_UNITS = {
  solar_power_sensor:        ['W','kW','watt','watts'],
  grid_power_sensor:         ['W','kW','watt','watts'],
  house_power_sensor:        ['W','kW','watt','watts'],
  battery_power_sensor:      ['W','kW','watt','watts'],
  battery_soc_sensor:        ['%'],
  tariff_sensor:             ['EUR/kWh','€/kWh','$/kWh','USD/kWh','ct/kWh'],
};
const SWITCH_FIELDS = ['battery_charge_switch','battery_discharge_switch','battery_standby_switch'];
const COMBO_FIELDS  = [...Object.keys(FIELD_UNITS), ...SWITCH_FIELDS];

function shorten(val) { return val ? (val.split('.').slice(1).join('.')||val) : '—'; }

function setupCombo(fieldKey, allEntities, currentVal, isSwitch, wantedUnits) {
  const input = document.getElementById('s_'+fieldKey);
  const list  = document.getElementById('sl_'+fieldKey);
  if (!input || !list) return;

  if (isSwitch === undefined || isSwitch === null) isSwitch = SWITCH_FIELDS.includes(fieldKey);
  if (!wantedUnits) wantedUnits = FIELD_UNITS[fieldKey] || [];

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
      '<li data-id="" class="cl-none">— none —</li>' +
      matches.map(e => {
        const name = (e.friendly_name && e.friendly_name !== e.entity_id) ? e.friendly_name : e.entity_id;
        const unit = e.unit ? '<span class="cl-unit">'+e.unit+'</span>' : '';
        return '<li data-id="'+e.entity_id+'"><span class="cl-name">'+name+'</span><span class="cl-sub">'+e.entity_id+'</span>'+unit+'</li>';
      }).join('');
    list.style.display = 'block';
  }

  input.addEventListener('focus', () => { input.value = ''; renderList(''); });
  input.addEventListener('input', () => { input.dataset.value = ''; renderList(input.value); });
  input.addEventListener('keydown', ev => { if (ev.key === 'Escape') { list.style.display = 'none'; input.blur(); } });
  input.addEventListener('blur', () => {
    setTimeout(() => { list.style.display = 'none'; }, 200);
    const sel = input.dataset.value;
    input.value = sel ? findName(sel) : '';
  });
  list.addEventListener('mousedown', ev => {
    ev.preventDefault();
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

  const COMBO_FIELDS_NO_EV = COMBO_FIELDS.slice();
  for (const key of COMBO_FIELDS_NO_EV) {
    setupCombo(key, _allEntities, settingsRes[key] || '');
    const v = document.getElementById("v_"+key);
    if (v) v.textContent = shorten(settingsRes[key]);
  }

  const numFmt = {
    battery_max_charge_w:   v=>v+' W',      battery_max_discharge_w: v=>v+' W',
    battery_min_soc:        v=>v+'%',       battery_max_soc:         v=>v+'%',
    cheap_threshold:        v=>v+' €/kWh', expensive_threshold: v=>v+' €/kWh',
    cheap_hysteresis:       v=>v+' €/kWh', expensive_hysteresis: v=>v+' €/kWh',
    cheap_lookahead_slots:  v=>v+' slots',
    update_interval:        v=>v+'s',
    tariff_a_consumption:   v=>'×'+v,  tariff_b_consumption:    v=>(v>=0?'+':'')+v+' €/kWh',
    tariff_a_injection:     v=>'×'+v,  tariff_b_injection:      v=>(v>=0?'+':'')+v+' €/kWh',
  };
  for (const [key,fmt] of Object.entries(numFmt)) {
    const el = document.getElementById("s_"+key); if (el&&settingsRes[key]!=null) el.value = settingsRes[key];
    const v  = document.getElementById("v_"+key); if (v&&settingsRes[key]!=null)  v.textContent = fmt(settingsRes[key]);
  }
  const socRange  = document.getElementById("v_battery_soc_range");
  if (socRange)  socRange.textContent  = (settingsRes.battery_min_soc??'?')+'% – '+(settingsRes.battery_max_soc??'?')+'%';
  const maxCharge = document.getElementById("v_battery_max_charge_w");
  if (maxCharge) maxCharge.textContent = (settingsRes.battery_max_charge_w??'?')+' W / '+(settingsRes.battery_max_discharge_w??'?')+' W';
  const hystEl = document.getElementById("v_cheap_hysteresis");
  if (hystEl) hystEl.textContent = (settingsRes.cheap_hysteresis??'?')+' / '+(settingsRes.expensive_hysteresis??'?')+' €/kWh';
  const lookaheadEl = document.getElementById("v_cheap_lookahead_slots");
  if (lookaheadEl) lookaheadEl.textContent = (settingsRes.cheap_lookahead_slots??'?')+' slots';
  // Show Ax+B block only when values differ from defaults (a=1, b=0)
  const axbNonDefault = (settingsRes.tariff_a_consumption??1)!=1||(settingsRes.tariff_b_consumption??0)!=0
                      ||(settingsRes.tariff_a_injection??1)!=1  ||(settingsRes.tariff_b_injection??0)!=0;
  const axbViewBlock = document.getElementById("v_axb_block");
  if (axbViewBlock) axbViewBlock.style.display = axbNonDefault ? '' : 'none';
  const axbDetails = document.getElementById("axb_details");
  if (axbDetails) axbDetails.open = axbNonDefault;


  // Panel & Forecast settings
  const panelFmt = {
    battery_capacity_kwh: v => v + ' kWh',
    panel_kwp:            v => v + ' kWp',
    panel_tilt:           v => v + '°',
    panel_azimuth:        v => v + '°',
    latitude:             v => v,
    longitude:            v => v,
  };
  for (const [key, fmt] of Object.entries(panelFmt)) {
    const el = document.getElementById('s_' + key);
    if (el && settingsRes[key] != null) el.value = settingsRes[key];
  }
  const locEl = document.getElementById('v_panel_location');
  if (locEl) locEl.textContent =
    (settingsRes.latitude ?? 0) !== 0 || (settingsRes.longitude ?? 0) !== 0
      ? `${settingsRes.latitude ?? '?'} / ${settingsRes.longitude ?? '?'}`
      : 'Not configured';
  const specEl = document.getElementById('v_panel_spec');
  if (specEl) specEl.textContent = settingsRes.panel_kwp > 0
    ? `${settingsRes.panel_kwp} kWp · Tilt ${settingsRes.panel_tilt ?? 35}° · Az ${settingsRes.panel_azimuth ?? 0}°`
    : 'Not configured';
  const capEl = document.getElementById('v_battery_capacity_kwh');
  if (capEl) capEl.textContent = (settingsRes.battery_capacity_kwh ?? '?') + ' kWh';

  _currentEvs = settingsRes.evs || [];
  renderEvFleet(_currentEvs);
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
  const intKeys   = ['battery_max_charge_w','battery_max_discharge_w','battery_min_soc','battery_max_soc','update_interval','cheap_lookahead_slots','panel_tilt','panel_azimuth'];
  const floatKeys = ['cheap_threshold','expensive_threshold','cheap_hysteresis','expensive_hysteresis','tariff_a_consumption','tariff_b_consumption','tariff_a_injection','tariff_b_injection','latitude','longitude','panel_kwp','battery_capacity_kwh'];
for (const key of keys) {
    let val;
    if (COMBO_FIELDS.includes(key)) {
      val = getComboValue(key);
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

// EV Fleet
let _currentEvs = [];
let _evCounter = 0;

function renderEvFleet(evs) {
  const viewEl  = document.getElementById('ev-fleet-view');
  const emptyEl = document.getElementById('ev-fleet-empty');
  if (!viewEl) return;
  if (!evs.length) {
    viewEl.innerHTML = '';
    if (emptyEl) emptyEl.style.display = '';
  } else {
    if (emptyEl) emptyEl.style.display = 'none';
    viewEl.innerHTML = evs.map((ev,i) =>
      '<div class="sg-row"><span class="sg-key">🚗 '+(ev.name||'EV '+(i+1))+'</span><span class="sg-val">'+(shorten(ev.charger_switch)||'—')+'</span></div>'
    ).join('');
  }
  _evCounter = evs.length;
  const editEl = document.getElementById('ev-fleet-edit');
  if (!editEl) return;
  editEl.innerHTML = evs.map((ev,i) => _evEntryHtml(i, ev)).join('');
  evs.forEach((ev,i) => _setupEvCombos(i, ev));
}

function _evEntryHtml(i, ev) {
  ev = ev || {};
  return '<div class="ev-entry" id="ev-entry-'+i+'" data-idx="'+i+'" style="border:1px solid var(--border);border-radius:.5rem;padding:.6rem;margin-bottom:.5rem">'
    +'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.4rem">'
    +'<strong style="font-size:.8rem">Vehicle '+(i+1)+'</strong>'
    +'<button class="pencil-btn" onclick="removeEv('+i+')" title="Remove">✕</button>'
    +'</div>'
    +'<div class="field"><label>Name</label><input type="text" id="ev_name_'+i+'" value="'+(ev.name||'')+'"></div>'
    +'<div class="field"><label>Charger switch</label><div class="combo"><input class="combo-input" id="s_ev_charger_'+i+'" placeholder="Search switch..." autocomplete="off"><ul class="combo-list" id="sl_ev_charger_'+i+'"></ul></div></div>'
    +'<div class="field"><label>SOC sensor (%)</label><div class="combo"><input class="combo-input" id="s_ev_soc_'+i+'" placeholder="Search % sensor..." autocomplete="off"><ul class="combo-list" id="sl_ev_soc_'+i+'"></ul></div></div>'
    +'<div class="ev-fields-grid">'
    +'<div class="field"><label>Target SOC (%)</label><input type="number" id="ev_target_soc_'+i+'" value="'+(ev.target_soc!=null?ev.target_soc:80)+'" min="20" max="100"></div>'
    +'<div class="field"><label>Departure</label><input type="time" id="ev_departure_'+i+'" value="'+(ev.departure_time||'07:00')+'"></div>'
    +'<div class="field"><label>Max (W)</label><input type="number" id="ev_max_w_'+i+'" value="'+(ev.max_charge_w!=null?ev.max_charge_w:7400)+'" min="1000" max="22000"></div>'
    +'<div class="field"><label>Capacité (kWh)</label><input type="number" id="ev_capacity_'+i+'" value="'+(ev.capacity_kwh!=null?ev.capacity_kwh:40)+'" min="5" max="200"></div>'
    +'</div></div>';
}

function _setupEvCombos(i, ev) {
  ev = ev || {};
  setupCombo('ev_charger_'+i, _allEntities, ev.charger_switch||'', true, null);
  setupCombo('ev_soc_'+i,     _allEntities, ev.soc_sensor||'',    false, ['%']);
}

function addEv() {
  const i = _evCounter++;
  const editEl = document.getElementById('ev-fleet-edit');
  if (!editEl) return;
  editEl.insertAdjacentHTML('beforeend', _evEntryHtml(i, {}));
  _setupEvCombos(i, {});
}

function removeEv(idx) {
  const entry = document.getElementById('ev-entry-'+idx);
  if (entry) entry.remove();
}

async function saveEvFleet() {
  const evs = [];
  document.querySelectorAll('.ev-entry').forEach(function(entry) {
    const i = entry.dataset.idx;
    evs.push({
      name:           document.getElementById('ev_name_'+i) ? document.getElementById('ev_name_'+i).value : 'EV',
      charger_switch: getComboValue('ev_charger_'+i),
      soc_sensor:     getComboValue('ev_soc_'+i),
      target_soc:     parseInt((document.getElementById('ev_target_soc_'+i)||{value:80}).value),
      departure_time: (document.getElementById('ev_departure_'+i)||{value:'07:00'}).value,
      max_charge_w:   parseInt((document.getElementById('ev_max_w_'+i)||{value:7400}).value),
      capacity_kwh:   parseFloat((document.getElementById('ev_capacity_'+i)||{value:40}).value),
    });
  });
  await fetch(BASE+'/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({evs: evs})});
  await loadSettings();
  toggleEdit('sg-ev');
  showToast();
}

function renderEvCards(evs) {
  const socEl = document.getElementById('ev-soc-cards');
  const decEl = document.getElementById('ev-decision-cards');
  if (!socEl || !decEl) return;
  if (!evs.length) { socEl.innerHTML = ''; decEl.innerHTML = ''; return; }
  socEl.innerHTML = evs.map(function(ev) {
    return '<div class="card"><div class="card-label">'+ev.name+'</div>'
      +'<div class="card-value">'+(ev.soc!=null ? ev.soc+'%' : '--')+'</div>'
      +'<div class="card-sub">'+(ev.connected ? 'Connected' : 'Disconnected')+'</div></div>';
  }).join('');
  decEl.innerHTML = evs.map(function(ev) {
    return '<div class="card"><div class="card-label">'+ev.name+'</div>'
      +'<div>'+badge(ev.decision, EV_MAP)+'</div></div>';
  }).join('');
}

refresh();
setInterval(refresh, 10000);

// ENERGY TAB
async function loadEpex() {
  try { _epexData = await fetch(BASE+'/api/epex').then(r=>r.json()); } catch(e){}
  renderEpex();
}

function ct(v) { return v != null ? v.toFixed(4)+' €/kWh' : '--'; }

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
  const vals   = slots.map(s=>+s.price_eur_kwh.toFixed(4));
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
      responsive:true,maintainAspectRatio:false,animation:{duration:500},
      plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>' '+c.parsed.y.toFixed(4)+' €/kWh'}}},
      scales:{
        x:{ticks:{color:getComputedStyle(document.documentElement).getPropertyValue('--muted').trim()||'#6b7280',maxTicksLimit:10,font:{size:9}},grid:{display:false}},
        y:{ticks:{color:getComputedStyle(document.documentElement).getPropertyValue('--muted').trim()||'#6b7280',font:{size:9},callback:v=>v+' €/kWh'},grid:{color:getComputedStyle(document.documentElement).getPropertyValue('--border').trim()||'#e5e7eb'}}
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
    return '<tr class="'+(isCur?'cur':'')+'"><td>'+t+'</td><td>'+s.price_eur_kwh.toFixed(4)+'</td><td><div class="pbar" style="width:'+Math.max(4,pct)+'%;background:'+col+'"></div></td></tr>';
  }).join('');
  const cur=tbody.querySelector('tr.cur');
  if(cur) setTimeout(()=>cur.scrollIntoView({block:'nearest',behavior:'smooth'}),100);
}

setInterval(()=>{ if(_epexData) renderEpex(); }, 60*1000);
setInterval(loadEpex, 15*60*1000);

// ── 24h optimized battery plan (/api/forecast) ──
const PLAN_MAP = {charge:{cls:"green",label:"Charge"},discharge:{cls:"yellow",label:"Décharge"},idle:{cls:"gray",label:"Repos"}};
let _forecastChartInst = null;
async function loadForecast() {
  try { renderForecast(await fetch(BASE+'/api/forecast').then(r=>r.json())); }
  catch(e) { console.error('Forecast:', e); }
}
function renderForecast(d) {
  const body = document.getElementById('plan-body');
  if (!body) return;
  const empty = document.getElementById('plan-empty');
  const wrap  = document.getElementById('plan-wrap');
  const status= document.getElementById('plan-status');
  const sched = (d && d.schedule) || [];
  if (!sched.length) {
    if (empty) empty.style.display = 'block';
    if (wrap)  wrap.style.display  = 'none';
    if (status) status.textContent = '';
    return;
  }
  if (empty) empty.style.display = 'none';
  if (wrap)  wrap.style.display  = '';
  const now = new Date();
  body.innerHTML = sched.map(function(s) {
    const h = new Date(s.hour);
    const isCur = h.getHours() === now.getHours() && h.toDateString() === now.toDateString();
    const kw = s.battery_kw ? s.battery_kw.toFixed(2) : '';
    const price = s.buy_price != null ? s.buy_price.toFixed(4) : '--';
    return '<tr class="'+(isCur?'cur':'')+'"><td>'+s.hour_label+'</td>'
      +'<td>'+badge(s.battery_action, PLAN_MAP)+'</td>'
      +'<td>'+kw+'</td><td>'+price+'</td>'
      +'<td style="color:var(--muted);font-size:.7rem;max-width:160px;white-space:normal">'+(s.reason||'')+'</td></tr>';
  }).join('');
  if (status) {
    const bits = [];
    if (d.built_at) bits.push('màj '+new Date(d.built_at).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}));
    bits.push(d.has_solar_forecast ? '☀ prévision solaire' : 'sans prévision solaire');
    bits.push(d.has_history ? 'historique conso OK' : 'conso par défaut');
    status.textContent = '· ' + bits.join(' · ');
  }
  // Solar/consumption forecast chart
  const fcCtx = document.getElementById('forecastChart');
  if (fcCtx && typeof Chart !== 'undefined') {
    const flabels = sched.map(s=>s.hour_label);
    const fsolar  = sched.map(s=>+((s.solar_w||0)/1000).toFixed(3));
    const fconso  = sched.map(s=>+((s.consumption_w||0)/1000).toFixed(3));
    if (_forecastChartInst) _forecastChartInst.destroy();
    _forecastChartInst = new Chart(fcCtx.getContext('2d'), {
      type:'line',
      data:{labels:flabels,datasets:[
        {label:'Solaire prévu (kW)', data:fsolar, borderColor:'rgb(255,152,0)', backgroundColor:'rgba(255,152,0,0.15)', fill:true,  tension:0.4, pointRadius:0, borderWidth:1.5},
        {label:'Conso prévue (kW)',  data:fconso, borderColor:'rgb(120,120,120)', backgroundColor:'rgba(120,120,120,0.08)', fill:false, tension:0.4, pointRadius:0, borderWidth:1.5, borderDash:[5,3]},
      ]},
      options:{
        responsive:true, maintainAspectRatio:false, animation:{duration:400},
        plugins:{ legend:{labels:{font:{size:10},boxWidth:10,usePointStyle:true}},
                  tooltip:{mode:'index',intersect:false,callbacks:{label:c=>` ${c.dataset.label}: ${c.parsed.y.toFixed(2)} kW`}} },
        scales:{ x:{ticks:{maxTicksLimit:12,font:{size:9}},grid:{display:false}},
                 y:{beginAtZero:true,ticks:{font:{size:9},callback:v=>v+' kW'}} }
      }
    });
  }
  const fcCalib = document.getElementById('fc-calib');
  if (fcCalib) {
    const c = d.solar_calibration;
    fcCalib.textContent = (c && c.hours_learned > 0)
      ? '· calibration maison ×' + c.mean_factor + ' (' + c.hours_learned + ' h apprises)'
      : '· calibration solaire en apprentissage…';
  }
  const cur = body.querySelector('tr.cur');
  if (cur) setTimeout(function(){ cur.scrollIntoView({block:'nearest'}); }, 100);
}
setInterval(function(){ if(document.getElementById('page-analyse').classList.contains('active')) loadForecast(); }, 5*60*1000);

// POWER HISTORY CHART
let _powerChart = null;

async function loadPowerChart() {
  try {
    const data = await fetch(BASE+'/api/power_history').then(r=>r.json());
    renderPowerChart(data.series || {});
  } catch(e) { console.error('Power chart:', e); }
}

// Align all series to common 5-min buckets so mode:'index' tooltip works correctly
function bucketSeries(rawSeries) {
  const BUCKET = 5 * 60 * 1000;
  const allTimes = new Set();
  for (const pts of Object.values(rawSeries)) {
    for (const p of pts) allTimes.add(Math.round(new Date(p[0]).getTime() / BUCKET) * BUCKET);
  }
  const times = [...allTimes].sort(function(a,b){return a-b;});
  const result = {};
  for (const role of Object.keys(rawSeries)) {
    const pts = rawSeries[role];
    const map = new Map();
    for (const p of pts) {
      const t = Math.round(new Date(p[0]).getTime() / BUCKET) * BUCKET;
      // average if multiple points fall in same bucket
      if (map.has(t)) map.set(t, (map.get(t) + p[1]) / 2);
      else map.set(t, p[1]);
    }
    result[role] = times.map(function(t) {
      return { x: t, y: map.has(t) ? +(map.get(t)/1000).toFixed(3) : null };
    });
  }
  return result;
}

function renderPowerChart(series) {
  const ctx = document.getElementById('power-chart');
  if (!ctx || typeof Chart === 'undefined') return;

  const now = Date.now();
  const midnight = new Date(); midnight.setHours(0,0,0,0);
  const t0 = midnight.getTime();

  const bucketed = bucketSeries(series);

  const COLORS = {
    solar:   { line:'rgb(255,152,0)',   fill:'rgba(255,152,0,0.15)'   },
    grid:    { line:'rgb(72,143,194)',  fill:'rgba(72,143,194,0.15)'  },
    battery: { line:'rgb(77,182,172)',  fill:'rgba(77,182,172,0.15)'  },
    house:   { line:'rgb(80,80,80)',    fill:'rgba(80,80,80,0.08)'    },
  };
  const LABELS = { solar:'Solar', grid:'Grid', battery:'Battery', house:'Consumption' };

  function toDataset(role) {
    const pts = bucketed[role];
    if (!pts || !pts.length) return null;
    const c = COLORS[role];
    // Battery: null means idle (unavailable sensor) → replace with 0 instead of interpolating
    const data = role === 'battery'
      ? pts.map(p => ({ x: p.x, y: p.y === null ? 0 : p.y }))
      : pts;
    return {
      label: LABELS[role],
      data,
      borderColor: c.line,
      backgroundColor: c.fill,
      fill: role !== 'house',
      tension: 0.4,
      spanGaps: role !== 'battery',
      borderWidth: role === 'house' ? 2 : 1.5,
      borderDash: role === 'house' ? [5,3] : [],
      pointRadius: 0,
    };
  }

  const datasets = ['solar','grid','battery','house'].map(toDataset).filter(Boolean);
  if (!datasets.length) return;

  if (_powerChart) _powerChart.destroy();
  _powerChart = new Chart(ctx, {
    type: 'line',
    data: { datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      parsing: false, normalized: true,
      scales: {
        x: {
          type: 'linear', min: t0, max: now,
          ticks: { stepSize: 3600000, maxTicksLimit: 13,
            callback: function(v) { const d=new Date(v); return d.getHours()+':'+(d.getMinutes()<10?'0':'')+d.getMinutes(); }
          },
          grid: { color: 'rgba(128,128,128,0.1)' },
        },
        y: {
          grid: { color: 'rgba(128,128,128,0.1)' },
          ticks: { callback: function(v) { return v+' kW'; } },
        },
      },
      plugins: {
        legend: { labels: { boxWidth: 10, padding: 14, usePointStyle: true } },
        tooltip: {
          mode: 'index', intersect: false,
          filter: function(item) { return item.parsed.y != null; },
          callbacks: {
            title: function(items) {
              if (!items.length) return '';
              const d = new Date(items[0].parsed.x);
              return d.getHours()+':'+(d.getMinutes()<10?'0':'')+d.getMinutes();
            },
            label: function(c) {
              if (c.parsed.y == null) return null;
              return ' '+c.dataset.label+': '+c.parsed.y.toFixed(2)+' kW';
            },
          },
        },
      },
      interaction: { mode: 'index', intersect: false },
    },
  });
}

setInterval(loadPowerChart, 10 * 60 * 1000);

// ── Energy history (Consommation & Coût): ONE global period + date for both charts ──
let _histPeriod = 'hourly', _histDate = '', _kwhChartInst = null, _priceChartInst = null;
function _isoDate(d){ return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0'); }
async function loadEnergyHistory() {
  const inp = document.getElementById('histDate');
  if (inp) { if (!inp.value) inp.value = _histDate || _isoDate(new Date()); _histDate = inp.value; }
  else if (!_histDate) { _histDate = _isoDate(new Date()); }
  try {
    const data = await fetch(BASE+'/api/energy/history?period='+_histPeriod+'&date='+_histDate).then(r=>r.json());
    renderKwhChart(data); renderPriceChart(data);
  } catch(e) { const el=document.getElementById('kwh-updated'); if(el) el.textContent='Erreur'; }
}
function setHistPeriod(period, btn) {
  _histPeriod = period;
  document.querySelectorAll('.hist-controls .day-btn[data-period]').forEach(b=>b.classList.toggle('active', b.dataset.period===period));
  loadEnergyHistory();
}
function stepHist(dir) {
  const inp = document.getElementById('histDate');
  const base = (inp && inp.value) ? new Date(inp.value+'T00:00:00') : new Date();
  if (_histPeriod === 'hourly')      base.setDate(base.getDate()+dir);
  else if (_histPeriod === 'daily')  base.setMonth(base.getMonth()+dir);
  else                               base.setFullYear(base.getFullYear()+dir);
  _histDate = _isoDate(base);
  if (inp) inp.value = _histDate;
  loadEnergyHistory();
}
function renderKwhChart(data) {
  if (!data || !data.items) return;
  const labels   = data.items.map(i=>i.label);
  const house    = data.items.map(i=>+(i.kwh_house||0).toFixed(3));
  const imported = data.items.map(i=>+(i.kwh_in||0).toFixed(3));
  const exported = data.items.map(i=>+(i.kwh_out||0).toFixed(3));
  const hasHouse = house.some(v=>v>0);
  // Stack the consumption breakdown above the axis: part covered by own solar
  // (consumption minus grid import) + grid import. Export is drawn below zero.
  const selfSolar = house.map((h,i)=>+Math.max(0, h - imported[i]).toFixed(3));
  const negExport = exported.map(v=>+(-v).toFixed(3));
  const ctx = document.getElementById('kwhChart').getContext('2d');
  if (_kwhChartInst) _kwhChartInst.destroy();
  const datasets = hasHouse
    ? [
        { label:'Autoconso. solaire', data:selfSolar, backgroundColor:'rgba(245,158,11,0.8)', borderRadius:2, stack:'e' },
        { label:'Importé (kWh)',      data:imported,  backgroundColor:'rgba(124,77,255,0.7)', borderRadius:2, stack:'e' },
        { label:'Exporté (kWh)',      data:negExport, backgroundColor:'rgba(16,185,129,0.7)', borderRadius:2, stack:'e' },
      ]
    : [
        { label:'Importé (kWh)', data:imported,  backgroundColor:'rgba(124,77,255,0.75)', borderRadius:2, stack:'e' },
        { label:'Exporté (kWh)', data:negExport,  backgroundColor:'rgba(16,185,129,0.75)', borderRadius:2, stack:'e' },
      ];
  _kwhChartInst = new Chart(ctx, {
    type:'bar', data:{labels,datasets},
    options:{
      responsive:true, maintainAspectRatio:false, animation:{duration:400},
      plugins:{ legend:{display:true,labels:{font:{size:10}}},
                tooltip:{callbacks:{label:c=>` ${c.dataset.label}: ${Math.abs(c.parsed.y).toFixed(3)} kWh`}} },
      scales:{
        x:{stacked:true,ticks:{maxTicksLimit:12,font:{size:9}},grid:{display:false}},
        y:{stacked:true,ticks:{font:{size:9},callback:v=>v.toFixed(2)+' kWh'}}
      }
    }
  });
  const el = document.getElementById('kwh-updated');
  if (el) { const t=data.totals; el.textContent=data.items.length+' barres · '+(t.kwh_house>0?'conso '+t.kwh_house.toFixed(2)+' kWh':'import '+t.kwh_in.toFixed(2)+' kWh'); }
}

// ── Prix payé (rendered together with the kWh chart by loadEnergyHistory) ──
function renderPriceChart(data) {
  if (!data || !data.items) return;
  const labels   = data.items.map(i=>i.label);
  const costs    = data.items.map(i=>+(i.cost||0).toFixed(4));
  const revenues = data.items.map(i=>+(i.revenue||0).toFixed(4));
  const negCosts = costs.map(v=>+(-v).toFixed(4));   // cost drawn below the axis
  const ctx = document.getElementById('priceChart').getContext('2d');
  if (_priceChartInst) _priceChartInst.destroy();
  _priceChartInst = new Chart(ctx, {
    type:'bar',
    data:{labels,datasets:[
      {label:'Revenu (€)', data:revenues, backgroundColor:'rgba(16,185,129,0.75)', borderRadius:2, stack:'p'},
      {label:'Coût (€)',   data:negCosts, backgroundColor:'rgba(239,68,68,0.75)',  borderRadius:2, stack:'p'},
    ]},
    options:{
      responsive:true, maintainAspectRatio:false, animation:{duration:400},
      plugins:{ legend:{display:true,labels:{font:{size:10}}},
                tooltip:{callbacks:{label:c=>` ${c.dataset.label}: ${Math.abs(c.parsed.y).toFixed(4)} €`}} },
      scales:{
        x:{stacked:true,ticks:{maxTicksLimit:12,font:{size:9}},grid:{display:false}},
        y:{stacked:true,ticks:{font:{size:9},callback:v=>v.toFixed(3)+' €'}}
      }
    }
  });
  const el = document.getElementById('price-updated');
  if (el) { const net=data.totals.net_cost; el.textContent=data.items.length+' barres · net '+(net>=0?'':'-')+Math.abs(net).toFixed(2)+' €'; }
}
setInterval(()=>{ if(document.getElementById('page-energy').classList.contains('active')) loadEnergyHistory(); }, 60000);

// Initial route from the URL hash (so a refresh restores the current tab).
showPage((location.hash || "").replace("#", "") || "energy");
</script>
</body>
</html>
"""
