"""Sensor platform for Energy Management System."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import UnitOfPower, UnitOfElectricCurrent
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN, MANUFACTURER
from .coordinator import SolarApplianceCoordinator
from .demand_coordinator import QuarterlyDemandCoordinator

_LOGGER = logging.getLogger(__name__)

# (data key, translation key, extra attributes {attr_name: data_key}) for the
# quarterly-demand power sensors.
_DEMAND_SENSORS: list[tuple[str, str, dict[str, str] | None]] = [
    ("estimate", "quarterly_demand_estimate", None),
    ("month_max", "quarterly_demand_month_max", {"previous_month_max": "previous_month_max"}),
    ("high_limit", "quarterly_demand_high_limit", None),
    ("critical_limit", "quarterly_demand_critical_limit", None),
    ("max_power_to_peak", "max_power_to_peak_limit", None),
]


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator: SolarApplianceCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        SolarApplianceStatusSensor(coordinator, entry.entry_id),
        ManagedConsumptionSensor(coordinator, entry.entry_id),
        SpareAmpsSensor(coordinator, entry.entry_id),
    ]
    demand: QuarterlyDemandCoordinator = coordinator.demand_coordinator
    entities.extend(
        QuarterlyDemandSensor(demand, entry.entry_id, key, translation_key, extra_attrs)
        for key, translation_key, extra_attrs in _DEMAND_SENSORS
    )
    async_add_entities(entities)


class SolarApplianceStatusSensor(CoordinatorEntity, SensorEntity):
    """Main status sensor."""

    _attr_has_entity_name = True
    _attr_translation_key = "status"
    _attr_native_unit_of_measurement = None
    _attr_icon = "mdi:solar-power"

    def __init__(self, coordinator: SolarApplianceCoordinator, entry_id: str):
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_status"


    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.entry_id)},
            name="EMS",
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
        demand = getattr(self.coordinator.demand_coordinator, "data", None) or {}

        return {
            "excess_amps": data.get("excess_amps"),
            "soc": data.get("soc"),
            "active_appliances": data.get("active_appliances", []),
            "auto_switching": data.get("auto_switching"),
            "demand_high": demand.get("high"),
            "demand_critical": demand.get("critical"),
        }


class ManagedConsumptionSensor(CoordinatorEntity, SensorEntity):
    """Amps the managed appliances are currently drawing."""

    _attr_has_entity_name = True
    _attr_translation_key = "managed_consumption"
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:power-plug"

    def __init__(self, coordinator: SolarApplianceCoordinator, entry_id: str):
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_managed_consumption"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.entry_id)},
            name="EMS",
            manufacturer=MANUFACTURER,
        )

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("managed_amps")


class SpareAmpsSensor(CoordinatorEntity, SensorEntity):
    """Total surplus amps available, including what the managed appliances draw.

    This is the gross budget (grid surplus + managed appliances' add-back) the
    optimizer allocates from — i.e. the amps available to spend.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "spare_amps"
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:flash"

    def __init__(self, coordinator: SolarApplianceCoordinator, entry_id: str):
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_spare_amps"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.entry_id)},
            name="EMS",
            manufacturer=MANUFACTURER,
        )

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("excess_amps")


class QuarterlyDemandSensor(CoordinatorEntity, SensorEntity):
    """A single quarter-hour demand power value (kW)."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: QuarterlyDemandCoordinator,
        entry_id: str,
        key: str,
        translation_key: str,
        extra_attrs: dict[str, str] | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        self._extra_attrs = extra_attrs or {}
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.entry_id)},
            name="EMS",
            manufacturer=MANUFACTURER,
        )

    @property
    def available(self) -> bool:
        return bool(
            super().available
            and self.coordinator.data
            and self.coordinator.data.get("configured")
        )

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get(self._key)

    @property
    def extra_state_attributes(self):
        if not self._extra_attrs or not self.coordinator.data:
            return None
        return {
            attr: self.coordinator.data.get(data_key)
            for attr, data_key in self._extra_attrs.items()
        }