"""Binary sensor platform for Energy Management System (quarterly demand)."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
)
from .const import DOMAIN
from .entity import EMSEntity
from .coordinator import SolarApplianceCoordinator
from .demand_coordinator import QuarterlyDemandCoordinator

# (data key, translation key) for the quarterly-demand warning binary sensors.
_DEMAND_BINARY_SENSORS: list[tuple[str, str]] = [
    ("high", "quarterly_demand_high"),
    ("critical", "quarterly_demand_critical"),
]


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator: SolarApplianceCoordinator = hass.data[DOMAIN][entry.entry_id]
    demand: QuarterlyDemandCoordinator = coordinator.demand_coordinator
    async_add_entities(
        QuarterlyDemandBinarySensor(demand, entry.entry_id, key, translation_key)
        for key, translation_key in _DEMAND_BINARY_SENSORS
    )


class QuarterlyDemandBinarySensor(EMSEntity, BinarySensorEntity):
    """A quarter-hour demand warning flag (High / Critical)."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        coordinator: QuarterlyDemandCoordinator,
        entry_id: str,
        key: str,
        translation_key: str,
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{key}"

    @property
    def available(self) -> bool:
        return bool(
            super().available
            and self.coordinator.data
            and self.coordinator.data.get("configured")
        )

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data and self.coordinator.data.get(self._key))
