"""Number platform: per-appliance car-SoC force-charge threshold (persistent)."""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE

from .const import (
    DOMAIN, CONF_NAME, CONF_EV_SOC_SENSOR, CONF_FORCE_CHARGE_BELOW_SOC,
    DEFAULT_FORCE_CHARGE_BELOW_SOC, get_appliances,
)
from .entity import EMSApplianceEntity
from .coordinator import SolarApplianceCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities):
    coordinator: SolarApplianceCoordinator = hass.data[DOMAIN][entry.entry_id]
    # One call per appliance (config_subentry_id is per-call, see select.py),
    # and only for EV chargers: the threshold is meaningless without a car SoC.
    for app in get_appliances(entry):
        if not app.get(CONF_EV_SOC_SENSOR):
            continue
        async_add_entities(
            [EVForceChargeBelowSocNumber(coordinator, app)],
            config_subentry_id=app["subentry_id"],
        )


class EVForceChargeBelowSocNumber(EMSApplianceEntity, NumberEntity):
    """Car SoC (%) below which a plugged-in car is charged as `forced`.

    0 (the default) disables the override. Persisted in the appliance subentry
    like the runtime mode, and applied live on the next cycle — no reload.
    """

    _attr_translation_key = "force_charge_below_soc"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:battery-charging-medium"

    def __init__(self, coordinator: SolarApplianceCoordinator, appliance):
        super().__init__(coordinator)
        self._appliance_id = appliance["subentry_id"]
        self._appliance_name = appliance[CONF_NAME]
        self._attr_unique_id = f"{DOMAIN}_{self._appliance_id}_force_charge_below_soc"

    @property
    def native_value(self) -> float:
        value = self.coordinator.get_appliance_persistent_setting(
            self._appliance_id, CONF_FORCE_CHARGE_BELOW_SOC, None, DEFAULT_FORCE_CHARGE_BELOW_SOC,
        )
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(DEFAULT_FORCE_CHARGE_BELOW_SOC)

    async def async_set_native_value(self, value: float) -> None:
        if not await self.coordinator.async_set_appliance_persistent_setting(
            self._appliance_id,
            CONF_FORCE_CHARGE_BELOW_SOC,
            int(round(value)),
            title=self._appliance_name,
        ):
            _LOGGER.warning(
                "Could not persist force-charge threshold for %s", self._appliance_name
            )
        self.async_write_ha_state()
