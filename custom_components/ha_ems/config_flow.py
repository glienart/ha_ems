"""Config flow for HA EMS — entity picker UI."""
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
# Step schemas
# ---------------------------------------------------------------------------

def _step1_schema() -> vol.Schema:
    """Power sensors — solar, grid, house."""
    return vol.Schema({
        vol.Required(CONF_SOLAR_POWER): selector.selector({"entity": {"domain": "sensor"}}),
        vol.Required(CONF_GRID_POWER): selector.selector({"entity": {"domain": "sensor"}}),
        vol.Optional(CONF_HOUSE_POWER): selector.selector({"entity": {"domain": "sensor"}}),
    })


def _step2_schema() -> vol.Schema:
    """Battery configuration."""
    return vol.Schema({
        vol.Required(CONF_BATTERY_SOC): selector.selector({"entity": {"domain": "sensor"}}),
        vol.Required(CONF_BATTERY_CHARGE_SWITCH): selector.selector({"entity": {"domain": ["switch", "input_boolean"]}}),
        vol.Required(CONF_BATTERY_DISCHARGE_SWITCH): selector.selector({"entity": {"domain": ["switch", "input_boolean"]}}),
        vol.Optional(CONF_BATTERY_STANDBY_SWITCH): selector.selector({"entity": {"domain": ["switch", "input_boolean"]}}),
        vol.Required(CONF_BATTERY_MAX_CHARGE_POWER, default=3000): vol.Coerce(int),
        vol.Required(CONF_BATTERY_MAX_DISCHARGE_POWER, default=3000): vol.Coerce(int),
        vol.Required(CONF_BATTERY_MIN_SOC, default=DEFAULT_BATTERY_MIN_SOC): vol.All(int, vol.Range(min=0, max=50)),
        vol.Required(CONF_BATTERY_MAX_SOC, default=DEFAULT_BATTERY_MAX_SOC): vol.All(int, vol.Range(min=50, max=100)),
    })


def _step3_schema() -> vol.Schema:
    """EV charger configuration."""
    return vol.Schema({
        vol.Optional(CONF_EV_CHARGER_SWITCH): selector.selector({"entity": {"domain": ["switch", "input_boolean"]}}),
        vol.Optional(CONF_EV_SOC): selector.selector({"entity": {"domain": "sensor"}}),
        vol.Required(CONF_EV_TARGET_SOC, default=DEFAULT_EV_TARGET_SOC): vol.All(int, vol.Range(min=20, max=100)),
        vol.Required(CONF_EV_DEPARTURE_TIME, default=DEFAULT_EV_DEPARTURE_TIME): cv.string,
        vol.Required(CONF_EV_MAX_CHARGE_POWER, default=7400): vol.Coerce(int),
    })


def _step4_schema() -> vol.Schema:
    """Tariff configuration."""
    return vol.Schema({
        vol.Optional(CONF_TARIFF_SENSOR): selector.selector({"entity": {"domain": "sensor"}}),
        vol.Required(CONF_CHEAP_TARIFF_THRESHOLD, default=DEFAULT_CHEAP_THRESHOLD): vol.Coerce(float),
        vol.Required(CONF_EXPENSIVE_TARIFF_THRESHOLD, default=DEFAULT_EXPENSIVE_THRESHOLD): vol.Coerce(float),
        vol.Required(CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL): vol.All(int, vol.Range(min=10, max=3600)),
    })


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------

class HAEmsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Multi-step config flow for HA EMS."""

    VERSION = 1
    _data: dict = {}

    async def async_step_user(self, user_input=None):
        """Step 1 — power sensors."""
        errors = {}
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_battery()
        return self.async_show_form(
            step_id="user",
            data_schema=_step1_schema(),
            errors=errors,
            description_placeholders={"step": "1/4 — Power sensors"},
        )

    async def async_step_battery(self, user_input=None):
        """Step 2 — battery."""
        errors = {}
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_ev()
        return self.async_show_form(
            step_id="battery",
            data_schema=_step2_schema(),
            errors=errors,
            description_placeholders={"step": "2/4 — Battery"},
        )

    async def async_step_ev(self, user_input=None):
        """Step 3 — EV charger."""
        errors = {}
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_tariff()
        return self.async_show_form(
            step_id="ev",
            data_schema=_step3_schema(),
            errors=errors,
            description_placeholders={"step": "3/4 — EV charger (optional)"},
        )

    async def async_step_tariff(self, user_input=None):
        """Step 4 — tariff."""
        errors = {}
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title=NAME, data=self._data)
        return self.async_show_form(
            step_id="tariff",
            data_schema=_step4_schema(),
            errors=errors,
            description_placeholders={"step": "4/4 — Tariff"},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HAEmsOptionsFlow(config_entry)


# ---------------------------------------------------------------------------
# Options flow (re-configure after setup)
# ---------------------------------------------------------------------------

class HAEmsOptionsFlow(config_entries.OptionsFlow):
    """Allow reconfiguration from the UI without removing the entry."""

    def __init__(self, config_entry):
        self._entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # Merge entry data + existing options as defaults
        current = {**self._entry.data, **self._entry.options}

        schema = vol.Schema({
            vol.Required(CONF_BATTERY_MIN_SOC, default=current.get(CONF_BATTERY_MIN_SOC, DEFAULT_BATTERY_MIN_SOC)): vol.All(int, vol.Range(min=0, max=50)),
            vol.Required(CONF_BATTERY_MAX_SOC, default=current.get(CONF_BATTERY_MAX_SOC, DEFAULT_BATTERY_MAX_SOC)): vol.All(int, vol.Range(min=50, max=100)),
            vol.Required(CONF_EV_TARGET_SOC, default=current.get(CONF_EV_TARGET_SOC, DEFAULT_EV_TARGET_SOC)): vol.All(int, vol.Range(min=20, max=100)),
            vol.Required(CONF_EV_DEPARTURE_TIME, default=current.get(CONF_EV_DEPARTURE_TIME, DEFAULT_EV_DEPARTURE_TIME)): cv.string,
            vol.Required(CONF_CHEAP_TARIFF_THRESHOLD, default=current.get(CONF_CHEAP_TARIFF_THRESHOLD, DEFAULT_CHEAP_THRESHOLD)): vol.Coerce(float),
            vol.Required(CONF_EXPENSIVE_TARIFF_THRESHOLD, default=current.get(CONF_EXPENSIVE_TARIFF_THRESHOLD, DEFAULT_EXPENSIVE_THRESHOLD)): vol.Coerce(float),
            vol.Required(CONF_UPDATE_INTERVAL, default=current.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)): vol.All(int, vol.Range(min=10, max=3600)),
        })

        return self.async_show_form(step_id="init", data_schema=schema)
