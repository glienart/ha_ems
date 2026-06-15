"""EPEX SPOT sensor entities for HA EMS."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME, VERSION
from .epex_coordinator import EpexCoordinator

EPEX_DEVICE_NAME = f"{NAME} — EPEX SPOT"


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_epex")},
        name=EPEX_DEVICE_NAME,
        manufacturer="ENTSO-E / EPEX SPOT",
        model="Day-ahead price feed",
        sw_version=VERSION,
        via_device=(DOMAIN, entry.entry_id),
    )


class _EpexBase(CoordinatorEntity, SensorEntity):
    """Base class for EPEX sensors."""

    coordinator: EpexCoordinator

    def __init__(
        self,
        coordinator: EpexCoordinator,
        entry: ConfigEntry,
        key: str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_epex_{key}"
        self._attr_device_info = _device_info(entry)
        self._attr_has_entity_name = True
        self._attr_native_unit_of_measurement = "EUR/kWh"
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 4

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._key)

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
            and self.coordinator.data.get(self._key) is not None
        )


class EpexCurrentPriceSensor(_EpexBase):
    """Current 15-min slot price in €/kWh."""

    def __init__(self, coordinator: EpexCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "current_price", "EPEX Current Price")
        self._attr_icon = "mdi:currency-eur"

    @property
    def extra_state_attributes(self) -> dict:
        if not self.coordinator.data:
            return {}
        d = self.coordinator.data
        return {
            "zone":              d.get("zone"),
            "slot_minutes":      d.get("slot_minutes"),
            "today_min":         d.get("today_min"),
            "today_max":         d.get("today_max"),
            "today_avg":         d.get("today_avg"),
            "tomorrow_min":      d.get("tomorrow_min"),
            "tomorrow_max":      d.get("tomorrow_max"),
            "tomorrow_avg":      d.get("tomorrow_avg"),
            "prices_today":      d.get("prices_today"),
            "prices_tomorrow":   d.get("prices_tomorrow"),
            "updated_at":        d.get("updated_at"),
        }


class EpexNextSlotPriceSensor(_EpexBase):
    def __init__(self, coordinator: EpexCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "next_slot_price", "EPEX Next Slot Price")
        self._attr_icon = "mdi:clock-fast"


class EpexTodayMinSensor(_EpexBase):
    def __init__(self, coordinator: EpexCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "today_min", "EPEX Today Min")
        self._attr_icon = "mdi:arrow-down-bold"


class EpexTodayMaxSensor(_EpexBase):
    def __init__(self, coordinator: EpexCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "today_max", "EPEX Today Max")
        self._attr_icon = "mdi:arrow-up-bold"


class EpexTodayAvgSensor(_EpexBase):
    def __init__(self, coordinator: EpexCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "today_avg", "EPEX Today Avg")
        self._attr_icon = "mdi:approximately-equal"


class EpexTomorrowMinSensor(_EpexBase):
    def __init__(self, coordinator: EpexCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "tomorrow_min", "EPEX Tomorrow Min")
        self._attr_icon = "mdi:calendar-arrow-down"


class EpexTomorrowMaxSensor(_EpexBase):
    def __init__(self, coordinator: EpexCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "tomorrow_max", "EPEX Tomorrow Max")
        self._attr_icon = "mdi:calendar-arrow-up"


# List used by sensor.py to register all EPEX entities
EPEX_SENSOR_CLASSES = [
    EpexCurrentPriceSensor,
    EpexNextSlotPriceSensor,
    EpexTodayMinSensor,
    EpexTodayMaxSensor,
    EpexTodayAvgSensor,
    EpexTomorrowMinSensor,
    EpexTomorrowMaxSensor,
]
