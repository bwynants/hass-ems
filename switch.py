"""Switch platform for Solar Appliance Manager."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity import DeviceInfo
from .const import (
    DOMAIN,
    CONF_NAME,
    CONF_ENTITY_ID,
    CONF_AUTO_SWITCHING,
    MANUFACTURER,
    get_appliances,
)
from .coordinator import SolarApplianceCoordinator

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator: SolarApplianceCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = [SolarApplianceAutoSwitch(coordinator, entry)]
    entities.extend(
        SolarApplianceSwitch(coordinator, app) for app in get_appliances(entry)
    )
    async_add_entities(entities)


class SolarApplianceAutoSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to enable or disable automatic appliance control."""

    _attr_name = "Auto Switching"
    _attr_icon = "mdi:autorenew"

    def __init__(
        self, coordinator: SolarApplianceCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_auto_switching"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.entry_id)},
            name="Solar Appliance Manager",
            manufacturer=MANUFACTURER,
        )

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
        self.coordinator.enabled = value
        options = dict(self._entry.options)
        options[CONF_AUTO_SWITCHING] = value
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        await self.coordinator.async_refresh()
        self.async_write_ha_state()


class SolarApplianceSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to show/control appliance state."""

    def __init__(self, coordinator: SolarApplianceCoordinator, appliance):
        super().__init__(coordinator)
        self.appliance = appliance
        self._attr_name = appliance[CONF_NAME]
        self._attr_unique_id = f"{DOMAIN}_{appliance[CONF_NAME]}_switch"
        self._attr_icon = "mdi:power-plug"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.entry_id)},
            name="Solar Appliance Manager",
            manufacturer=MANUFACTURER,
        )

    def _persist(self, key: str, value) -> None:
        """Persist state to config_entry.data so it survives restarts."""
        try:
            new_data = dict(self.coordinator.config_entry.data)
            new_data[key] = value
            self.hass.config_entries.async_update_entry(
                self.coordinator.config_entry, data=new_data
            )
        except Exception as err:
            _LOGGER.warning("Could not persist %s change: %s", key, err)


    @property
    def is_on(self) -> bool:
        """Return true if appliance is currently active."""
        return self.appliance[CONF_NAME] in self.coordinator.data.get("active_appliances", [])

    async def async_turn_on(self):
        """Manually turn on (will be managed by logic afterwards)."""
        await self.hass.services.async_call(
            "switch", "turn_on",
            {"entity_id": self.appliance[CONF_ENTITY_ID]},
            blocking=True
        )

    async def async_turn_off(self):
        """Manually turn off."""
        await self.hass.services.async_call(
            "switch", "turn_off",
            {"entity_id": self.appliance[CONF_ENTITY_ID]},
            blocking=True
        )