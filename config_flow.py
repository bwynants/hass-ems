from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
)

from homeassistant.config_entries import (
    ConfigSubentryFlow,
    SubentryFlowResult,
    OptionsFlow,
)

from .const import (
    DOMAIN,
    SUBENTRY_TYPE_APPLIANCE,
    BATTERY_PROTECTION_MODES,
    DEFAULT_PRIORITY,
    DEFAULT_MIN_AMPS,
    DEFAULT_MAX_AMPS,
    DEFAULT_FIXED_AMPS_HYSTERESIS_SECONDS,
    DEFAULT_MIN_SOC,
    DEFAULT_BATTERY_MODE,
    DEFAULT_HYSTERESIS,
    DEFAULT_AVERAGING,
    DEFAULT_IGNORE_PROTECTION,
    CONF_SOLAR_PRODUCTION,
    CONF_HOUSE_LOAD,
    CONF_GRID_EXPORT,
    CONF_BATTERY_SOC_SENSOR,
    CONF_PEAK_TARIFF_SENSOR,
    CONF_VOLTAGE,
    CONF_MIN_SOC,
    CONF_BATTERY_PROTECTION_MODE,
    CONF_NAME,
    CONF_ENTITY_ID,
    CONF_PRIORITY,
    CONF_MIN_AMPS,
    CONF_MAX_AMPS,
    CONF_FIXED_AMPS,
    CONF_FIXED_AMPS_HYSTERESIS_SECONDS,
    CONF_POWER_SENSOR,
    CONF_PARTIAL_MIN_EXCESS,
    CONF_CONNECTED_SENSOR,
    CONF_HYSTERESIS_ON_OFF_SECONDS,
    CONF_AVERAGING_WINDOW,
    CONF_MIN_SOC_OVERRIDE,
    CONF_IGNORE_BATTERY_PROTECTION,
    CONF_SOC_HYSTERESIS_PERCENT,
    CONF_IS_BATTERY,
    DEFAULT_IS_BATTERY,
    CONF_DEMAND_POWER_CURRENT_PEAK_SENSOR,
    CONF_DEMAND_POWER_CONSUMED_SENSOR,
    CONF_DEMAND_MONTH_MAX_SENSOR,
    CONF_MINIMAL_PEAK_WARNING,
    CONF_PEAK_HIGH_SAFETY,
    CONF_PEAK_CRITICAL_SAFETY,
    DEFAULT_MINIMAL_PEAK_WARNING,
    DEFAULT_PEAK_HIGH_SAFETY,
    DEFAULT_PEAK_CRITICAL_SAFETY,
)

_LOGGER = logging.getLogger(__name__)

# Entity selectors
SENSOR_ENTITY_SELECTOR = EntitySelector(
    EntitySelectorConfig(domain=["sensor", "input_number"])
)
NUMBER_ENTITY_SELECTOR = EntitySelector(
    EntitySelectorConfig(domain=["number", "input_number"])
)
SWITCH_ENTITY_SELECTOR = EntitySelector(
    EntitySelectorConfig(domain=["switch", "input_boolean", "light", "climate", "fan"])
)
BINARY_SENSOR_ENTITY_SELECTOR = EntitySelector(
    EntitySelectorConfig(domain="binary_sensor")
)
# Sensor-only selector for the quarterly-demand sources: input_number is
# deliberately NOT offered (those values are entered as plain numbers).
SENSOR_ONLY_SELECTOR = EntitySelector(
    EntitySelectorConfig(domain="sensor")
)

class SolarApplianceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Energy Management System."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        errors: dict[str, str] = {}

        if user_input is not None:
            if not errors:
                self._data.update(user_input)
                return self.async_create_entry(
                    title="Energy Management System",
                    data=self._data,
                )

        data_schema = vol.Schema({
            vol.Optional(CONF_GRID_EXPORT): SENSOR_ENTITY_SELECTOR,
            vol.Required(CONF_SOLAR_PRODUCTION): SENSOR_ENTITY_SELECTOR,
            vol.Optional(CONF_HOUSE_LOAD): SENSOR_ENTITY_SELECTOR,
            vol.Optional(CONF_DEMAND_POWER_CURRENT_PEAK_SENSOR): SENSOR_ONLY_SELECTOR,
            vol.Optional(CONF_DEMAND_POWER_CONSUMED_SENSOR): SENSOR_ONLY_SELECTOR,
            vol.Optional(CONF_DEMAND_MONTH_MAX_SENSOR): SENSOR_ONLY_SELECTOR,
            vol.Optional(CONF_PEAK_TARIFF_SENSOR): BINARY_SENSOR_ENTITY_SELECTOR,
            vol.Optional(CONF_VOLTAGE): NUMBER_ENTITY_SELECTOR,
            vol.Optional(CONF_MINIMAL_PEAK_WARNING, default=DEFAULT_MINIMAL_PEAK_WARNING): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional(CONF_PEAK_HIGH_SAFETY, default=DEFAULT_PEAK_HIGH_SAFETY): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional(CONF_PEAK_CRITICAL_SAFETY, default=DEFAULT_PEAK_CRITICAL_SAFETY): vol.All(vol.Coerce(float), vol.Range(min=0)),
        })

        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )


    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: config_entries.ConfigEntry
    ) -> dict[str, type]:
        """Return supported subentry types."""
        return {SUBENTRY_TYPE_APPLIANCE: OptionsFlowHandler}

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> SolarApplianceOptionsFlow:
        """Get the options flow handler."""
        return SolarApplianceOptionsFlow()


class SolarApplianceOptionsFlow(OptionsFlow):
    """Handle options flow for the main integration."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle options flow init."""
        # Load current data on first entry into options
        if not hasattr(self, "data") or not self.data:
            self.data = dict(self.config_entry.data)

        return await self.async_step_user(user_input)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle main options editing."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if not errors:
                self.data.update(user_input)
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=self.data
                )
                return self.async_create_entry(data={})

        # Build schema with current values as defaults
        d = self.data

        data_schema = vol.Schema({
            vol.Optional(
                CONF_GRID_EXPORT,
                default=d.get(CONF_GRID_EXPORT)
            ): SENSOR_ENTITY_SELECTOR,
            vol.Required(
                CONF_SOLAR_PRODUCTION, 
                default=d.get(CONF_SOLAR_PRODUCTION)
            ): SENSOR_ENTITY_SELECTOR,
            vol.Optional(
                CONF_HOUSE_LOAD, 
                default=d.get(CONF_HOUSE_LOAD)
            ): SENSOR_ENTITY_SELECTOR,
            vol.Optional(
                CONF_DEMAND_POWER_CURRENT_PEAK_SENSOR,
                description={"suggested_value": d.get(CONF_DEMAND_POWER_CURRENT_PEAK_SENSOR)}
            ): SENSOR_ONLY_SELECTOR,
            vol.Optional(
                CONF_DEMAND_POWER_CONSUMED_SENSOR,
                description={"suggested_value": d.get(CONF_DEMAND_POWER_CONSUMED_SENSOR)}
            ): SENSOR_ONLY_SELECTOR,
            vol.Optional(
                CONF_DEMAND_MONTH_MAX_SENSOR,
                description={"suggested_value": d.get(CONF_DEMAND_MONTH_MAX_SENSOR)}
            ): SENSOR_ONLY_SELECTOR,
            vol.Optional(
                CONF_PEAK_TARIFF_SENSOR,
                description={"suggested_value": d.get(CONF_PEAK_TARIFF_SENSOR)}
            ): BINARY_SENSOR_ENTITY_SELECTOR,
            vol.Optional(
                CONF_VOLTAGE,
                description={"suggested_value":d.get(CONF_VOLTAGE)}
            ): NUMBER_ENTITY_SELECTOR,
            vol.Optional(
                CONF_MINIMAL_PEAK_WARNING,
                default=d.get(CONF_MINIMAL_PEAK_WARNING, DEFAULT_MINIMAL_PEAK_WARNING)
            ): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional(
                CONF_PEAK_HIGH_SAFETY,
                default=d.get(CONF_PEAK_HIGH_SAFETY, DEFAULT_PEAK_HIGH_SAFETY)
            ): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional(
                CONF_PEAK_CRITICAL_SAFETY,
                default=d.get(CONF_PEAK_CRITICAL_SAFETY, DEFAULT_PEAK_CRITICAL_SAFETY)
            ): vol.All(vol.Coerce(float), vol.Range(min=0)),
        })

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors
        )

# ---------------------------------------------------------------------------
# Appliance Subentry Flow (Add / Edit / Remove)
# ---------------------------------------------------------------------------
class OptionsFlowHandler(ConfigSubentryFlow):
    """Handle appliance subentries (create, reconfigure, remove)."""

    def __init__(self) -> None:
        """Initialize the subentry flow."""
        super().__init__()
        self._data: dict[str, Any] = {}
        self._subentry_id: str | None = None

    # ---------------------- Create new appliance ----------------------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle creation of a new appliance."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if not errors:
                self._data.update(user_input)
                title = self._data.get(CONF_NAME, "Appliance")
                return self.async_create_entry(
                    title=title,
                    data=self._data,
                )

        schema = vol.Schema({
            vol.Required(CONF_NAME): str,
            vol.Required(CONF_ENTITY_ID): SWITCH_ENTITY_SELECTOR,
            vol.Required(CONF_PRIORITY, default=DEFAULT_PRIORITY): vol.All(int, vol.Range(0, 100)),
            vol.Required(CONF_MIN_AMPS, default=DEFAULT_MIN_AMPS): vol.All(int, vol.Range(2, 20)),
            vol.Required(CONF_MAX_AMPS, default=DEFAULT_MAX_AMPS): vol.All(int, vol.Range(2, 20)),
            vol.Optional(CONF_FIXED_AMPS): NUMBER_ENTITY_SELECTOR,
            vol.Optional(CONF_FIXED_AMPS_HYSTERESIS_SECONDS, default=DEFAULT_FIXED_AMPS_HYSTERESIS_SECONDS): vol.All(int, vol.Range(min=0)),
            vol.Optional(CONF_POWER_SENSOR): SENSOR_ENTITY_SELECTOR,
            vol.Optional(CONF_PARTIAL_MIN_EXCESS): vol.All(int, vol.Range(0, 5000)),
            vol.Optional(CONF_CONNECTED_SENSOR): BINARY_SENSOR_ENTITY_SELECTOR,
            # Hysteresis
            vol.Optional(CONF_HYSTERESIS_ON_OFF_SECONDS, default=DEFAULT_HYSTERESIS): vol.All(int, vol.Range(min=0)),
            vol.Optional(CONF_AVERAGING_WINDOW, default=DEFAULT_AVERAGING): vol.All(int, vol.Range(min=30)),
            # SoC
            vol.Optional(CONF_MIN_SOC_OVERRIDE): vol.All(int, vol.Range(0, 100)),
            vol.Optional(CONF_IGNORE_BATTERY_PROTECTION, default=DEFAULT_IGNORE_PROTECTION): bool,
            vol.Optional(CONF_SOC_HYSTERESIS_PERCENT): vol.All(int, vol.Range(0, 20)),
            # Battery: when enabled, this appliance provides the SoC sensor and
            # protection threshold used to gate the other appliances.
            vol.Optional(CONF_IS_BATTERY, default=DEFAULT_IS_BATTERY): bool,
            vol.Optional(CONF_BATTERY_SOC_SENSOR): SENSOR_ENTITY_SELECTOR,
            vol.Optional(CONF_MIN_SOC, default=DEFAULT_MIN_SOC): vol.All(int, vol.Range(0, 100)),
            vol.Optional(CONF_BATTERY_PROTECTION_MODE, default=DEFAULT_BATTERY_MODE): vol.In(BATTERY_PROTECTION_MODES),
        })

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    # ---------------------- Reconfigure existing ----------------------
    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Start reconfiguration."""
        subentry = self._get_reconfigure_subentry()
        if subentry is None:
            return self.async_abort(reason="reconfigure_not_supported")

        self._data = dict(subentry.data)
        self._subentry_id = getattr(subentry, "subentry_id", None) or getattr(subentry, "id", None)

        return await self.async_step_reconfigure_basic(user_input)

    async def async_step_reconfigure_basic(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show form and handle update for reconfigure."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._data.update(user_input)
            # Optional entity fields are omitted from user_input when the user
            # clears them. Drop any that were left empty so they can be removed.
            for key in (
                CONF_FIXED_AMPS,
                CONF_POWER_SENSOR,
                CONF_PARTIAL_MIN_EXCESS,
                CONF_CONNECTED_SENSOR,
                CONF_MIN_SOC_OVERRIDE,
                CONF_SOC_HYSTERESIS_PERCENT,
                CONF_BATTERY_SOC_SENSOR,
            ):
                if key not in user_input:
                    self._data.pop(key, None)
            title = self._data.get(CONF_NAME, "Appliance")

            try:
                return self.async_update_and_abort(
                    self._get_entry(),
                    self._get_reconfigure_subentry(),
                    title=title,
                    data=self._data,
                )
            except (AttributeError, TypeError):
                _LOGGER.warning("async_update_and_abort not available, using fallback")
                try:
                    entry = self.hass.config_entries.async_get_entry(self.handler)
                    if entry and hasattr(self.hass.config_entries, "async_update_subentry"):
                        subentry_id = self._subentry_id
                        if subentry_id and subentry_id in getattr(entry, "subentries", {}):
                            self.hass.config_entries.async_update_subentry(
                                entry, entry.subentries[subentry_id],
                                data=self._data, title=title
                            )
                            return self.async_abort(reason="reconfigure_successful")
                except Exception as err:
                    _LOGGER.exception("Failed to update subentry: %s", err)

            return self.async_abort(reason="reconfigure_not_supported")

        # Show form
        d = self._data
        schema = vol.Schema({
            vol.Required(CONF_NAME, default=d.get(CONF_NAME, "Appliance")): str,
            vol.Required(CONF_ENTITY_ID, default=d.get(CONF_ENTITY_ID)): SWITCH_ENTITY_SELECTOR,
            vol.Required(CONF_PRIORITY, default=d.get(CONF_PRIORITY, DEFAULT_PRIORITY)): vol.All(int, vol.Range(0, 100)),
            vol.Required(CONF_MIN_AMPS, default=d.get(CONF_MIN_AMPS, DEFAULT_MIN_AMPS)): vol.All(int, vol.Range(2, 20)),
            vol.Required(CONF_MAX_AMPS, default=d.get(CONF_MAX_AMPS, DEFAULT_MAX_AMPS)): vol.All(int, vol.Range(2, 20)),
            vol.Optional(CONF_FIXED_AMPS, description={"suggested_value": d.get(CONF_FIXED_AMPS)}): NUMBER_ENTITY_SELECTOR,
            vol.Optional(CONF_FIXED_AMPS_HYSTERESIS_SECONDS, default=d.get(CONF_FIXED_AMPS_HYSTERESIS_SECONDS, DEFAULT_FIXED_AMPS_HYSTERESIS_SECONDS)): vol.All(int, vol.Range(min=0)),
            vol.Optional(CONF_POWER_SENSOR, description={"suggested_value": d.get(CONF_POWER_SENSOR)}): SENSOR_ENTITY_SELECTOR,
            vol.Optional(CONF_PARTIAL_MIN_EXCESS, description={"suggested_value": d.get(CONF_PARTIAL_MIN_EXCESS)}): vol.All(int, vol.Range(0, 5000)),
            vol.Optional(CONF_CONNECTED_SENSOR, description={"suggested_value": d.get(CONF_CONNECTED_SENSOR)}): BINARY_SENSOR_ENTITY_SELECTOR,
            vol.Optional(CONF_HYSTERESIS_ON_OFF_SECONDS, default=d.get(CONF_HYSTERESIS_ON_OFF_SECONDS, DEFAULT_HYSTERESIS)): vol.All(int, vol.Range(min=0)),
            vol.Optional(CONF_AVERAGING_WINDOW, default=d.get(CONF_AVERAGING_WINDOW, DEFAULT_AVERAGING)): vol.All(int, vol.Range(min=30)),
            vol.Optional(CONF_MIN_SOC_OVERRIDE, description={"suggested_value": d.get(CONF_MIN_SOC_OVERRIDE)}): vol.All(int, vol.Range(0, 100)),
            vol.Optional(CONF_IGNORE_BATTERY_PROTECTION, default=d.get(CONF_IGNORE_BATTERY_PROTECTION, DEFAULT_IGNORE_PROTECTION)): bool,
            vol.Optional(CONF_SOC_HYSTERESIS_PERCENT, description={"suggested_value": d.get(CONF_SOC_HYSTERESIS_PERCENT)}): vol.All(int, vol.Range(0, 20)),
            # Battery
            vol.Optional(CONF_IS_BATTERY, default=d.get(CONF_IS_BATTERY, DEFAULT_IS_BATTERY)): bool,
            vol.Optional(CONF_BATTERY_SOC_SENSOR, description={"suggested_value": d.get(CONF_BATTERY_SOC_SENSOR)}): SENSOR_ENTITY_SELECTOR,
            vol.Optional(CONF_MIN_SOC, default=d.get(CONF_MIN_SOC, DEFAULT_MIN_SOC)): vol.All(int, vol.Range(0, 100)),
            vol.Optional(CONF_BATTERY_PROTECTION_MODE, default=d.get(CONF_BATTERY_PROTECTION_MODE, DEFAULT_BATTERY_MODE)): vol.In(BATTERY_PROTECTION_MODES),
        })

        return self.async_show_form(
            step_id="reconfigure_basic", data_schema=schema, errors=errors
        )

    # ---------------------- Remove appliance ----------------------
    async def async_step_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle removal of an appliance subentry."""
        if user_input is None:
            subentry = self._get_reconfigure_subentry()
            name = subentry.data.get(CONF_NAME, "this appliance") if subentry else "this appliance"

            return self.async_show_form(
                step_id="remove",
                data_schema=vol.Schema({}),
                description_placeholders={"name": name},
            )

        # Confirmed removal
        try:
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                data={},
            )
        except (AttributeError, TypeError):
            _LOGGER.warning("async_update_and_abort failed for removal, using direct method")
            try:
                entry = self.hass.config_entries.async_get_entry(self.handler)
                subentry = self._get_reconfigure_subentry()
                if entry and subentry:
                    subentry_id = getattr(subentry, "subentry_id", None) or getattr(subentry, "id", None)
                    if subentry_id:
                        await self.hass.config_entries.async_remove_subentry(
                            entry.entry_id, subentry_id
                        )
                        return self.async_abort(reason="subentry_removed")
            except Exception as err:
                _LOGGER.exception("Failed to remove subentry: %s", err)

        return self.async_abort(reason="remove_failed")