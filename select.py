"""Select platform for runtime mode changes (persistent)."""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN, MODES, DEFAULT_MODE, CONF_RUNTIME_MODE, CONF_RUNTIME_MODES, CONF_NAME, MANUFACTURER, get_appliances
from .coordinator import SolarApplianceCoordinator

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities):
    coordinator: SolarApplianceCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        SolarApplianceModeSelect(coordinator, app)
        for app in get_appliances(entry)
    ]
    async_add_entities(entities)


class SolarApplianceModeSelect(CoordinatorEntity, SelectEntity):
    """Persistent mode selector for each appliance."""

    # Shared translation key so the mode options are translatable; the entity
    # name stays per-appliance (user-provided), so has_entity_name is not used.
    _attr_translation_key = "appliance_mode"

    def __init__(self, coordinator: SolarApplianceCoordinator, appliance):
        super().__init__(coordinator)
        self.appliance = appliance
        self._attr_name = f"{appliance[CONF_NAME]} Mode"
        # Prefer stable subentry id for uniqueness/persistence, fall back to name
        self._appliance_id = appliance.get("subentry_id") or appliance.get("id")
        self._attr_unique_id = f"{DOMAIN}_{self._appliance_id}_mode" if self._appliance_id else f"{DOMAIN}_{appliance[CONF_NAME]}_mode"
        self._attr_options = MODES
        self._appliance_name = appliance[CONF_NAME]

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.entry_id)},
            name="EMS",
            manufacturer=MANUFACTURER,
        )

    @property
    def current_option(self) -> str:
        """Return current mode (from persistent storage)."""
        if not self._appliance_id:
            _LOGGER.warning("Appliance %s has no subentry_id; using default mode", self._appliance_name)
            return DEFAULT_MODE

        return self.coordinator.get_appliance_persistent_setting(
            self._appliance_id,
            CONF_RUNTIME_MODE,
            CONF_RUNTIME_MODES,
            DEFAULT_MODE,
        )

    async def async_select_option(self, option: str) -> None:
        """Change mode and save persistently."""
        if option not in MODES:
            return
        entry = self.coordinator.config_entry
        appliance_id = self._appliance_id
        if not appliance_id:
            _LOGGER.warning("Cannot persist runtime mode for %s: missing subentry_id", self._appliance_name)
            return

        # Persist to the subentry when available
        if await self.coordinator.async_set_appliance_persistent_setting(
            self._appliance_id,
            CONF_RUNTIME_MODE,
            option,
            title=self._appliance_name,
        ):
            return

        # If subentry update isn't available or failed, fall back to options to avoid losing user change
        entry = self.coordinator.config_entry
        options = dict(entry.options)
        runtime_modes = dict(options.get(CONF_RUNTIME_MODES, {}))
        runtime_modes[self._appliance_id] = option
        options[CONF_RUNTIME_MODES] = runtime_modes
        self.hass.config_entries.async_update_entry(entry, options=options)
        await self.coordinator.async_refresh()