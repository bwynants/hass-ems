"""Switch platform for Energy Management System."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.components.switch import SwitchEntity
from .const import DOMAIN
from .entity import EMSEntity
from .coordinator import SolarApplianceCoordinator

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator: SolarApplianceCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SolarApplianceAutoSwitch(coordinator, entry)])


class SolarApplianceAutoSwitch(EMSEntity, SwitchEntity):
    """Switch to enable or disable automatic appliance control."""

    _attr_translation_key = "auto_switching"
    _attr_icon = "mdi:autorenew"

    def __init__(
        self, coordinator: SolarApplianceCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_auto_switching"

    @property
    def is_on(self) -> bool:
        """Return true if automatic switching is enabled."""
        return self.coordinator.enabled

    async def async_turn_on(self) -> None:
        """Enable automatic switching."""
        await self._async_set_enabled(True)

    async def async_turn_off(self) -> None:
        """Disable automatic switching."""
        await self._async_set_enabled(False)

    async def _async_set_enabled(self, value: bool) -> None:
        # Use coordinator API so the global switch state is only changed
        # via this explicit method and not by incidental code paths.
        await self.coordinator.async_set_auto_switching(value)
        self.async_write_ha_state()
