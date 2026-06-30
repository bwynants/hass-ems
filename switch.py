"""Switch platform for Energy Management System."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.core import split_entity_id
from .const import (
    DOMAIN,
    CONF_NAME,
    CONF_ENTITY_ID,
    CONF_ENABLED,
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

    _attr_has_entity_name = True
    _attr_translation_key = "auto_switching"
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
            name="EMS",
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
        # Use coordinator API so the global switch state is only changed
        # via this explicit method and not by incidental code paths.
        await self.coordinator.async_set_auto_switching(value)
        self.async_write_ha_state()


class SolarApplianceSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to show/control appliance state."""

    def __init__(self, coordinator: SolarApplianceCoordinator, appliance):
        super().__init__(coordinator)
        self.appliance = appliance
        self._attr_name = appliance[CONF_NAME]
        # Use stable subentry id for unique_id when available
        self._appliance_id = appliance.get("subentry_id") or appliance.get("id")
        if self._appliance_id:
            self._attr_unique_id = f"{DOMAIN}_{self._appliance_id}_switch"
        else:
            _LOGGER.warning("Appliance %s has no subentry_id; falling back to name for unique_id", appliance[CONF_NAME])
            self._attr_unique_id = f"{DOMAIN}_{appliance[CONF_NAME]}_switch"
        self._attr_icon = "mdi:power-plug"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.entry_id)},
            name="EMS",
            manufacturer=MANUFACTURER,
        )

    @property
    def is_on(self) -> bool:
        """Return true if appliance is enabled for the optimizer (manual toggle)."""
        return bool(self.appliance.get(CONF_ENABLED, True))

    async def async_turn_on(self):
        """Enable this appliance to be considered by the optimizer."""
        await self._set_enabled(True)
        # If global auto switching is disabled, the appliance switch should act
        # as a direct controller and drive the underlying entity.
        if not self.coordinator.enabled:
            entity_id = self.appliance.get(CONF_ENTITY_ID)
            if not entity_id:
                _LOGGER.debug("No entity_id for %s; cannot turn on", self.appliance.get(CONF_NAME))
                return
            domain = split_entity_id(entity_id)[0]
            await self.hass.services.async_call(
                domain,
                "turn_on",
                {"entity_id": entity_id},
                blocking=True,
            )
            await self.coordinator.async_refresh()


    async def async_turn_off(self):
        """Disable this appliance so the optimizer will skip it."""
        await self._set_enabled(False)
        # If global auto switching is disabled, operate the underlying entity
        # directly when the appliance switch is toggled.
        if not self.coordinator.enabled:
            entity_id = self.appliance.get(CONF_ENTITY_ID)
            if not entity_id:
                _LOGGER.debug("No entity_id for %s; cannot turn off", self.appliance.get(CONF_NAME))
                return
            domain = split_entity_id(entity_id)[0]
            await self.hass.services.async_call(
                domain,
                "turn_off",
                {"entity_id": entity_id},
                blocking=True,
            )
            await self.coordinator.async_refresh()

    async def _set_enabled(self, value: bool) -> None:
        appliance_id = self.appliance.get("subentry_id") or self.appliance.get("id")
        if appliance_id and await self.coordinator.async_set_appliance_persistent_setting(
            appliance_id,
            CONF_ENABLED,
            value,
            title=self.appliance.get(CONF_NAME),
        ):
            return

        _LOGGER.warning(
            "Could not persist enabled flag to subentry and no fallback will be applied for %s",
            self.appliance.get(CONF_NAME),
        )

