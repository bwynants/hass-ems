"""Sensor platform for Solar Appliance Manager."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity import DeviceInfo
from .const import CONF_NAME, DOMAIN, MANUFACTURER
from .coordinator import SolarApplianceCoordinator

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator: SolarApplianceCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SolarApplianceStatusSensor(coordinator)])


class SolarApplianceStatusSensor(CoordinatorEntity, SensorEntity):
    """Main status sensor."""

    _attr_name = "Solar Appliance Manager Status"
    _attr_native_unit_of_measurement = None

    def __init__(self, coordinator: SolarApplianceCoordinator, appliance):
        super().__init__(coordinator)
        self.appliance = appliance
        self._attr_name = appliance[CONF_NAME]
        self._attr_unique_id = f"{DOMAIN}_{appliance[CONF_NAME]}_sensor"
        self._attr_icon = "mdi:solar-power"


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
    def native_value(self):
        active = self.coordinator.data.get("active_appliances", [])
        return f"{len(active)} active"

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data
        config = {**self.coordinator.entry.data, **self.coordinator.entry.options}
        
        return {
            "excess_amps": data.get("excess_amps"),
            "soc": data.get("soc"),
            "active_appliances": data.get("active_appliances", []),
            "capacity_tariff_active": self.coordinator._is_capacity_tariff_active(config),
        }