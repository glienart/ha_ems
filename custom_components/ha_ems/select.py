"""Select entity for EMS mode."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME, VERSION, EMS_MODES, MODE_AUTO
from . import EmsCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EmsCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EmsModeSelect(coordinator, entry)])


class EmsModeSelect(CoordinatorEntity, SelectEntity):
    """Dropdown to change the EMS operating mode."""

    coordinator: EmsCoordinator

    def __init__(self, coordinator: EmsCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_name = "Mode"
        self._attr_unique_id = f"{entry.entry_id}_mode"
        self._attr_options = EMS_MODES
        self._attr_current_option = MODE_AUTO
        self._attr_icon = "mdi:tune"
        self._attr_has_entity_name = True
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=NAME,
            manufacturer="HA EMS",
            model="Energy Management System",
            sw_version=VERSION,
        )

    @property
    def current_option(self) -> str:
        return self.coordinator.mode

    async def async_select_option(self, option: str) -> None:
        self.coordinator.mode = option
        await self.coordinator.async_request_refresh()
