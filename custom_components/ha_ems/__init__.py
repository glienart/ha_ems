"""HA EMS — Home Energy Management System integration."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    CONF_SOLAR_POWER, CONF_GRID_POWER, CONF_HOUSE_POWER,
    CONF_BATTERY_SOC, CONF_BATTERY_MIN_SOC, CONF_BATTERY_MAX_SOC,
    CONF_BATTERY_MAX_CHARGE_POWER, CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_CHARGE_SWITCH, CONF_BATTERY_DISCHARGE_SWITCH, CONF_BATTERY_STANDBY_SWITCH,
    CONF_EV_CHARGER_SWITCH, CONF_EV_SOC, CONF_EV_TARGET_SOC,
    CONF_EV_DEPARTURE_TIME, CONF_EV_MAX_CHARGE_POWER,
    CONF_TARIFF_SENSOR, CONF_CHEAP_TARIFF_THRESHOLD, CONF_EXPENSIVE_TARIFF_THRESHOLD,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    MODE_AUTO, CURRENT_MODE,
    BAT_CHARGE, BAT_DISCHARGE, BAT_STANDBY, BAT_IDLE,
    EV_CHARGE, EV_PAUSE,
    DECISION_BATTERY, DECISION_EV, SOLAR_SURPLUS, NET_POWER,
)
from .optimizer import EmsOptimizer, EmsSnapshot

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "select"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up HA EMS from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    coordinator = EmsCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload HA EMS config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update — reload so coordinator picks up new config."""
    await hass.config_entries.async_reload(entry.entry_id)


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class EmsCoordinator(DataUpdateCoordinator):
    """
    Polls HA state, runs the optimizer, and applies switch decisions.

    Data dict keys (see const.py):
      DECISION_BATTERY, DECISION_EV, SOLAR_SURPLUS, NET_POWER, CURRENT_MODE
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        cfg = {**entry.data, **entry.options}
        interval = cfg.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )
        self._cfg = cfg
        self._optimizer = EmsOptimizer()
        self._mode = MODE_AUTO  # can be changed by select entity

    @property
    def mode(self) -> str:
        return self._mode

    @mode.setter
    def mode(self, value: str) -> None:
        self._mode = value

    # ------------------------------------------------------------------
    # Core update
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict:
        """Read HA states, run optimizer, apply decisions."""
        snap = await self._build_snapshot()
        decision = self._optimizer.decide(snap)

        await self._apply_battery(decision.battery)
        await self._apply_ev(decision.ev)

        return {
            DECISION_BATTERY: decision.battery,
            DECISION_EV: decision.ev,
            SOLAR_SURPLUS: decision.solar_surplus_w,
            NET_POWER: decision.net_power_w,
            CURRENT_MODE: self._mode,
            "reason": decision.reason,
        }

    # ------------------------------------------------------------------
    # Snapshot builder
    # ------------------------------------------------------------------

    async def _build_snapshot(self) -> EmsSnapshot:
        cfg = self._cfg

        def _float(entity_id: str | None, default: float = 0.0) -> float:
            if not entity_id:
                return default
            state = self.hass.states.get(entity_id)
            if state is None or state.state in ("unknown", "unavailable"):
                return default
            try:
                return float(state.state)
            except (ValueError, TypeError):
                return default

        def _bool(entity_id: str | None) -> bool:
            if not entity_id:
                return False
            state = self.hass.states.get(entity_id)
            return state is not None and state.state == "on"

        solar_w = _float(cfg.get(CONF_SOLAR_POWER))
        grid_w = _float(cfg.get(CONF_GRID_POWER))
        house_w_raw = cfg.get(CONF_HOUSE_POWER)
        house_w = _float(house_w_raw) if house_w_raw else None

        bat_soc = _float(cfg.get(CONF_BATTERY_SOC), default=50.0)
        ev_soc_entity = cfg.get(CONF_EV_SOC)
        ev_soc = _float(ev_soc_entity) if ev_soc_entity else None
        ev_connected = _bool(cfg.get(CONF_EV_CHARGER_SWITCH)) or (ev_soc is not None)

        tariff_entity = cfg.get(CONF_TARIFF_SENSOR)
        tariff = _float(tariff_entity) if tariff_entity else None

        return EmsSnapshot(
            solar_power_w=solar_w,
            grid_power_w=grid_w,
            house_power_w=house_w,
            battery_soc_pct=bat_soc,
            battery_min_soc=cfg.get(CONF_BATTERY_MIN_SOC, 10),
            battery_max_soc=cfg.get(CONF_BATTERY_MAX_SOC, 95),
            battery_max_charge_w=cfg.get(CONF_BATTERY_MAX_CHARGE_POWER, 3000),
            battery_max_discharge_w=cfg.get(CONF_BATTERY_MAX_DISCHARGE_POWER, 3000),
            ev_connected=ev_connected,
            ev_soc_pct=ev_soc,
            ev_target_soc=cfg.get(CONF_EV_TARGET_SOC, 80),
            ev_departure_time=cfg.get(CONF_EV_DEPARTURE_TIME, "07:00"),
            ev_max_charge_w=cfg.get(CONF_EV_MAX_CHARGE_POWER, 7400),
            tariff_eur_kwh=tariff,
            cheap_threshold=cfg.get(CONF_CHEAP_TARIFF_THRESHOLD, 0.10),
            expensive_threshold=cfg.get(CONF_EXPENSIVE_TARIFF_THRESHOLD, 0.25),
            mode=self._mode,
            now=datetime.now(),
        )

    # ------------------------------------------------------------------
    # Actuator helpers
    # ------------------------------------------------------------------

    async def _apply_battery(self, decision: str) -> None:
        cfg = self._cfg
        charge_sw = cfg.get(CONF_BATTERY_CHARGE_SWITCH)
        discharge_sw = cfg.get(CONF_BATTERY_DISCHARGE_SWITCH)
        standby_sw = cfg.get(CONF_BATTERY_STANDBY_SWITCH)

        async def _switch(entity_id: str | None, turn_on: bool) -> None:
            if not entity_id:
                return
            service = "turn_on" if turn_on else "turn_off"
            await self.hass.services.async_call(
                "homeassistant", service, {"entity_id": entity_id}, blocking=False
            )

        if decision == BAT_CHARGE:
            await _switch(charge_sw, True)
            await _switch(discharge_sw, False)
            await _switch(standby_sw, False)
        elif decision == BAT_DISCHARGE:
            await _switch(charge_sw, False)
            await _switch(discharge_sw, True)
            await _switch(standby_sw, False)
        elif decision == BAT_STANDBY:
            await _switch(charge_sw, False)
            await _switch(discharge_sw, False)
            await _switch(standby_sw, True)
        else:  # BAT_IDLE — don't touch switches; let inverter decide
            pass

    async def _apply_ev(self, decision: str) -> None:
        ev_sw = self._cfg.get(CONF_EV_CHARGER_SWITCH)
        if not ev_sw:
            return
        service = "turn_on" if decision == EV_CHARGE else "turn_off"
        await self.hass.services.async_call(
            "homeassistant", service, {"entity_id": ev_sw}, blocking=False
        )
