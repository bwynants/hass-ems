"""Sensor platform for Energy Management System."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import UnitOfPower, UnitOfElectricCurrent
from .const import DOMAIN
from .entity import EMSEntity
from .coordinator import SolarApplianceCoordinator
from .demand_coordinator import QuarterlyDemandCoordinator

# (data key, unique-id suffix, translation key, icon, device class, unit) for
# the value sensors read from the main coordinator: managed consumption and
# spare budget, each in amps and in kW.
_MAIN_SENSORS: list[tuple[str, str, str, str, SensorDeviceClass, str]] = [
    ("managed_amps", "managed_consumption_amps", "managed_consumption_amps", "mdi:power-plug",
     SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE),
    ("excess_amps", "spare_amps", "spare_amps", "mdi:flash",
     SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE),
    ("managed_power", "managed_consumption_power", "managed_consumption_power", "mdi:power-plug",
     SensorDeviceClass.POWER, UnitOfPower.KILO_WATT),
    ("excess_kw", "spare_kw", "spare_kw", "mdi:flash",
     SensorDeviceClass.POWER, UnitOfPower.KILO_WATT),
]

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
    entities: list[SensorEntity] = [SolarApplianceStatusSensor(coordinator, entry.entry_id)]
    entities.extend(
        MainValueSensor(coordinator, entry.entry_id, key, uid_suffix, translation_key, icon, device_class, unit)
        for key, uid_suffix, translation_key, icon, device_class, unit in _MAIN_SENSORS
    )
    demand: QuarterlyDemandCoordinator = coordinator.demand_coordinator
    entities.extend(
        QuarterlyDemandSensor(demand, entry.entry_id, key, translation_key, extra_attrs)
        for key, translation_key, extra_attrs in _DEMAND_SENSORS
    )
    async_add_entities(entities)


class SolarApplianceStatusSensor(EMSEntity, SensorEntity):
    """Main status sensor."""

    _attr_translation_key = "status"
    _attr_native_unit_of_measurement = None
    _attr_icon = "mdi:solar-power"

    def __init__(self, coordinator: SolarApplianceCoordinator, entry_id: str):
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_status"

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
            "active_appliances": data.get("active_appliances", []),
            # Appliances currently force-charged by the car-SoC rule.
            "forced_appliances": data.get("forced_appliances", []),
            # Switched on by EMS but not drawing anything (see binary sensors).
            "not_consuming_appliances": data.get("not_consuming_appliances", []),
            # Throttled, cut or shed for the quarter-hour peak this cycle.
            "peak_limited_appliances": data.get("peak_limited_appliances", []),
            "auto_switching": data.get("auto_switching"),
            "demand_high": demand.get("high"),
            "demand_critical": demand.get("critical"),
            # Startup warm-up: control held until the sources report. Exposed so
            # the reason for a quiet controller is visible without the log.
            "warming_up": bool(data.get("warming_up")),
            "warming_up_waiting_on": data.get("warming_up_waiting_on", []),
        }


class MainValueSensor(EMSEntity, SensorEntity):
    """A single value read from the main coordinator's data.

    Covers "Managed Consumption" (draw of the managed appliances) and
    "Spare Amps"/"Spare Power" (gross surplus budget the optimizer allocates
    from), each exposed in amps and in kW.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: SolarApplianceCoordinator,
        entry_id: str,
        key: str,
        uid_suffix: str,
        translation_key: str,
        icon: str,
        device_class: SensorDeviceClass,
        unit: str,
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_translation_key = translation_key
        self._attr_icon = icon
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{uid_suffix}"

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
        value = self.coordinator.data.get(self._key)
        # The spare/available budget is a surplus — never show it negative. When
        # importing, the internal excess is negative (a deficit); the optimizer
        # keeps that signed value, but the user-facing sensor reads 0 (no spare).
        if value is not None and self._key in ("excess_amps", "excess_kw"):
            return max(0.0, value)
        return value


class QuarterlyDemandSensor(EMSEntity, SensorEntity):
    """A single quarter-hour demand power value (kW)."""

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
