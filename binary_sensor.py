"""Binary sensor platform: quarterly-demand flags and per-appliance force-charge state."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
)
from .const import DOMAIN, CONF_NAME, CONF_EV_SOC_SENSOR, get_appliances
from .entity import EMSEntity, EMSApplianceEntity
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
    # Per-appliance force-charge flag, EV chargers only. One call per appliance:
    # config_subentry_id is per-call and ties the entity to the appliance device.
    for app in get_appliances(entry):
        if not app.get(CONF_EV_SOC_SENSOR):
            continue
        async_add_entities(
            [EVForceChargingBinarySensor(coordinator, app)],
            config_subentry_id=app["subentry_id"],
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


class EVForceChargingBinarySensor(EMSApplianceEntity, BinarySensorEntity):
    """On while the car-SoC rule overrides this appliance's mode to `forced`.

    "On" means EMS *wants* to force-charge; the charger can still be held off
    by the quarter-hour peak gate — which is exactly the diagnostic needed when
    a plugged-in car isn't charging. Hence no BATTERY_CHARGING device class: it
    would read "Charging" while nothing flows.
    """

    _attr_translation_key = "force_charging"
    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator: SolarApplianceCoordinator, appliance) -> None:
        super().__init__(coordinator)
        self._appliance_id = appliance["subentry_id"]
        self._appliance_name = appliance[CONF_NAME]
        self._attr_unique_id = f"{DOMAIN}_{self._appliance_id}_force_charging"

    @property
    def available(self) -> bool:
        # None during the startup warm-up: no decision has been taken yet.
        data = self.coordinator.data
        return bool(
            super().available
            and data
            and data.get("forced_appliance_ids") is not None
        )

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data or {}
        return self._appliance_id in (data.get("forced_appliance_ids") or [])
