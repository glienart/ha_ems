"""Config flow for HA EMS -- entity picker UI."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
import homeassistant.helpers.config_validation as cv

from .const import (
    DOMAIN, NAME,
    CONF_SOLAR_POWER, CONF_GRID_POWER,
    CONF_BATTERY_SOC, CONF_BATTERY_CHARGE_SWITCH,
    CONF_BATTERY_DISCHARGE_SWITCH, CONF_BATTERY_STANDBY_SWITCH,
    CONF_BATTERY_MAX_CHARGE_POWER, CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_MIN_SOC, CONF_BATTERY_MAX_SOC,
    CONF_EV_CHARGER_SWITCH, CONF_EV_SOC,
    CONF_EV_TARGET_SOC, CONF_EV_DEPARTURE_TIME, CONF_EV_MAX_CHARGE_POWER,
    CONF_TARIFF_SENSOR, CONF_CHEAP_TARIFF_THRESHOLD, CONF_EXPENSIVE_TARIFF_THRESHOLD,
    CONF_HOUSE_POWER, CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL, DEFAULT_BATTERY_MIN_SOC, DEFAULT_BATTERY_MAX_SOC,
    DEFAULT_EV_TARGET_SOC, DEFAULT_EV_DEPARTURE_TIME,
    DEFAULT_CHEAP_THRESHOLD, DEFAULT_EXPENSIVE_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Selector helpers -- mirrors Energy Dashboard device_class filtering
# ---------------------------------------------------------------------------

def _power_sensor():
    """Instantaneous power sensors (W)."""
    return selector.selector({
        "entity": {
            "filter": [{"domain": "sensor", "device_class": "power"}]
        }
    })


def _soc_sensor():
    """State-of-charge sensors (%)."""
    return selector.selector({
        "entity": {
            "filter": [{"domain": "sensor", "device_class": "battery"}]
        }
    })


def _switch_sel():
    """Switches and input_booleans."""
    return selector.selector({
        "entity": {
            "filter": [
                {"domain": "switch"},
                {"domain": "input_boolean"},
            ]
        }
    })


def _monetary_sensor():
    """Tariff / price sensors. Includes generic sensor fallback."""
    return selector.selector({
        "entity": {
            "filter": [
                {"domain": "sensor", "device_class": "monetary"},
                {"domain": "sensor"},
            ]
        }
    })


def _num(min_val, max_val, unit="", step=1):
    return selector.selector({
        "number": {
            "min": min_val,
            "max": max_val,
            "step": step,
            "unit_of_measurement": unit,
            "mode": "box",
        }
    })


# ---------------------------------------------------------------------------
# Step schemas
# ---------------------------------------------------------------------------

def _step1_schema():
    """Power sensors -- solar, grid, house."""
    return vol.Schema({
        vol.Required(CONF_SOLAR_POWER): _power_sensor(),
        vol.Required(CONF_GRID_POWER): _power_sensor(),
        vol.Optional(CONF_HOUSE_POWER): _power_sensor(),
    })


def _step2_schema():
    """Battery configuration."""
    return vol.Schema({
        vol.Required(CONF_BATTERY_SOC): _soc_sensor(),
        vol.Required(CONF_BATTERY_CHARGE_SWITCH): _switch_sel(),
        vol.Required(CONF_BATTERY_DISCHARGE_SWITCH): _switch_sel(),
        vol.Optional(CONF_BATTERY_STANDBY_SWITCH): _switch_sel(),
        vol.Required(CONF_BATTERY_MAX_CHARGE_POWER, default=3000): _num(100, 20000, "W"),
        vol.Required(CONF_BATTERY_MAX_DISCHARGE_POWER, default=3000): _num(100, 20000, "W"),
        vol.Required(CONF_BATTERY_MIN_SOC, default=DEFAULT_BATTERY_MIN_SOC): _num(0, 50, "%"),
        vol.Required(CONF_BATTERY_MAX_SOC, default=DEFAULT_BATTERY_MAX_SOC): _num(50, 100, "%"),
    })


def _step3_schema():
    """EV charger configuration."""
    return vol.Schema({
        vol.Optional(CONF_EV_CHARGER_SWITCH): _switch_sel(),
        vol.Optional(CONF_EV_SOC): _soc_sensor(),
        vol.Required(CONF_EV_TARGET_SOC, default=DEFAULT_EV_TARGET_SOC): _num(20, 100, "%"),
        vol.Required(CONF_EV_DEPARTURE_TIME, default=DEFAULT_EV_DEPARTURE_TIME): selector.selector({"time": {}}),
        vol.Required(CONF_EV_MAX_CHARGE_POWER, default=7400): _num(1000, 22000, "W"),
    })


def _step4_schema():
    """Tariff configuration."""
    return vol.Schema({
        vol.Optional(CONF_TARIFF_SENSOR): _monetary_sensor(),
        vol.Required(CONF_CHEAP_TARIFF_THRESHOLD, default=DEFAULT_CHEAP_THRESHOLD): _num(0, 1, "EUR/kWh", 0.01),
        vol.Required(CONF_EXPENSIVE_TARIFF_THRESHOLD, default=DEFAULT_EXPENSIVE_THRESHOLD): _num(0, 1, "EUR/kWh", 0.01),
        vol.Required(CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL): _num(10, 3600, "s"),
    })


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------

class HAEmsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Multi-step config flow for HA EMS."""

    VERSION = 1

    def __init__(self):
        self._data = {}

    async def async_step_user(self, user_input=None):
        """Step 1 -- power sensors."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_battery()
        return self.async_show_form(
            step_id="user",
            data_schema=_step1_schema(),
        )

    async def async_step_battery(self, user_input=None):
        """Step 2 -- battery."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_ev()
        return self.async_show_form(
            step_id="battery",
            data_schema=_step2_schema(),
        )

    async def async_step_ev(self, user_input=None):
        """Step 3 -- EV charger."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_tariff()
        return self.async_show_form(
            step_id="ev",
            data_schema=_step3_schema(),
        )

    async def async_step_tariff(self, user_input=None):
        """Step 4 -- tariff."""
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title=NAME, data=self._data)
        return self.async_show_form(
            step_id="tariff",
            data_schema=_step4_schema(),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HAEmsOptionsFlow(config_entry)


# ---------------------------------------------------------------------------
# Options flow (reconfigure without removing the entry)
# ---------------------------------------------------------------------------

class HAEmsOptionsFlow(config_entries.OptionsFlow):
    """Allow reconfiguration from the UI."""

    def __init__(self, config_entry):
        self._entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = {**self._entry.data, **self._entry.options}

        schema = vol.Schema({
            vol.Required(CONF_BATTERY_MIN_SOC, default=current.get(CONF_BATTERY_MIN_SOC, DEFAULT_BATTERY_MIN_SOC)): _num(0, 50, "%"),
            vol.Required(CONF_BATTERY_MAX_SOC, default=current.get(CONF_BATTERY_MAX_SOC, DEFAULT_BATTERY_MAX_SOC)): _num(50, 100, "%"),
            vol.Required(CONF_EV_TARGET_SOC, default=current.get(CONF_EV_TARGET_SOC, DEFAULT_EV_TARGET_SOC)): _num(20, 100, "%"),
            vol.Required(CONF_EV_DEPARTURE_TIME, default=current.get(CONF_EV_DEPARTURE_TIME, DEFAULT_EV_DEPARTURE_TIME)): selector.selector({"time": {}}),
            vol.Required(CONF_CHEAP_TARIFF_THRESHOLD, default=current.get(CONF_CHEAP_TARIFF_THRESHOLD, DEFAULT_CHEAP_THRESHOLD)): _num(0, 1, "EUR/kWh", 0.01),
            vol.Required(CONF_EXPENSIVE_TARIFF_THRESHOLD, default=current.get(CONF_EXPENSIVE_TARIFF_THRESHOLD, DEFAULT_EXPENSIVE_THRESHOLD)): _num(0, 1, "EUR/kWh", 0.01),
            vol.Required(CONF_UPDATE_INTERVAL, default=current.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)): _num(10, 3600, "s"),
        })

        return self.async_show_form(step_id="init", data_schema=schema)
