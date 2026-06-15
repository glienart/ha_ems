"""Sensor entities for HA EMS."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass, SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN, NAME, VERSION,
    DECISION_BATTERY, DECISION_EV, SOLAR_SURPLUS, NET_POWER, CURRENT_MODE,
)
from . import EmsCoordinator
from .epex_sensor import EPEX_SENSOR_CLASSES


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EmsCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        EmsBatteryDecisionSensor(coordinator, entry),
        EmsEvDecisionSensor(coordinator, entry),
        EmsSolarSurplusSensor(coordinator, entry),
        EmsNetPowerSensor(coordinator, entry),
        EmsReasonSensor(coordinator, entry),
    ]

    # Register EPEX sensors if the coordinator was initialised
    if coordinator.epex is not None:
        entities.extend(cls(coordinator.epex, entry) for cls in EPEX_SENSOR_CLASSES)

    async_add_entities(entities)


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=NAME,
        manufacturer="HA EMS",
        model="Energy Management System",
        sw_version=VERSION,
    )


class _EmsBaseSensor(CoordinatorEntity, SensorEntity):
    coordinator: EmsCoordinator

    def __init__(self, coordinator: EmsCoordinator, entry: ConfigEntry, key: str, name: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = _device_info(entry)
        self._attr_has_entity_name = True

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._key)


class EmsBatteryDecisionSensor(_EmsBaseSensor):
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, DECISION_BATTERY, "Battery Decision")
        self._attr_icon = "mdi:battery-charging"


class EmsEvDecisionSensor(_EmsBaseSensor):
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, DECISION_EV, "EV Decision")
        self._attr_icon = "mdi:car-electric"


class EmsSolarSurplusSensor(_EmsBaseSensor):
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, SOLAR_SURPLUS, "Solar Surplus")
        self._attr_native_unit_of_measurement = "W"
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:solar-power"


class EmsNetPowerSensor(_EmsBaseSensor):
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, NET_POWER, "Grid Net Power")
        self._attr_native_unit_of_measurement = "W"
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:transmission-tower"


class EmsReasonSensor(_EmsBaseSensor):
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "reason", "Last Decision Reason")
        self._attr_icon = "mdi:information-outline"
