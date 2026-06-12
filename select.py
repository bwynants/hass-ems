"""Select platform for runtime mode changes (persistent)."""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN, MODES, DEFAULT_MODE, CONF_RUNTIME_MODES, CONF_NAME, MANUFACTURER, get_appliances
from .coordinator import SolarApplianceCoordinator

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities):
    coordinator: SolarApplianceCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        SolarApplianceModeSelect(coordinator, app, entry)
        for app in get_appliances(entry)
    ]
    async_add_entities(entities)


class SolarApplianceModeSelect(CoordinatorEntity, SelectEntity):
    """Persistent mode selector for each appliance."""

    def __init__(self, coordinator: SolarApplianceCoordinator, appliance, config_entry):
        super().__init__(coordinator)
        self.appliance = appliance
        self.config_entry = config_entry
        self._attr_name = f"{appliance[CONF_NAME]} Mode"

        self._attr_unique_id = f"{DOMAIN}_{appliance[CONF_NAME]}_mode"
        self._attr_options = MODES
        self._appliance_name = appliance[CONF_NAME]

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
    def current_option(self) -> str:
        """Return current mode (from persistent storage)."""
        runtime_modes = self.config_entry.options.get(CONF_RUNTIME_MODES, {})
        return runtime_modes.get(self._appliance_name, DEFAULT_MODE)

    async def async_select_option(self, option: str) -> None:
        """Change mode and save persistently."""
        if option not in MODES:
            return

        # Update options
        options = dict(self.config_entry.options)
        runtime_modes = dict(options.get(CONF_RUNTIME_MODES, {}))
        runtime_modes[self._appliance_name] = option
        options[CONF_RUNTIME_MODES] = runtime_modes

        # Save to config entry
        self.hass.config_entries.async_update_entry(self.config_entry, options=options)

        # Refresh coordinator
        await self.coordinator.async_refresh()