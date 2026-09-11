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
    DEFAULT_PRIORITY,
    DEFAULT_FIXED_AMPS_HYSTERESIS_SECONDS,
    DEFAULT_HYSTERESIS,
    DEFAULT_AVERAGING,
    CONF_SOLAR_PRODUCTION,
    CONF_HOUSE_LOAD,
    CONF_GRID_EXPORT,
    CONF_PEAK_TARIFF_SENSOR,
    CONF_VOLTAGE,
    CONF_NAME,
    CONF_ENTITY_ID,
    CONF_PRIORITY,
    CONF_FIXED_AMPS,
    CONF_FIXED_AMPS_HYSTERESIS_SECONDS,
    CONF_POWER_SENSOR,
    CONF_PARTIAL_MIN_EXCESS,
    CONF_CONNECTED_SENSOR,
    CONF_EV_SOC_SENSOR,
    CONF_HYSTERESIS_ON_OFF_SECONDS,
    CONF_AVERAGING_WINDOW,
    CONF_DEMAND_POWER_CURRENT_PEAK_SENSOR,
    CONF_DEMAND_POWER_CONSUMED_SENSOR,
    CONF_DEMAND_MONTH_MAX_SENSOR,
    CONF_MINIMAL_PEAK_WARNING,
    CONF_PEAK_HIGH_SAFETY,
    CONF_PEAK_CRITICAL_SAFETY,
    DEFAULT_MINIMAL_PEAK_WARNING,
    DEFAULT_PEAK_HIGH_SAFETY,
    DEFAULT_PEAK_CRITICAL_SAFETY,
    CONF_COST_PEAK,
    CONF_COST_OFFPEAK,
    CONF_GAIN_EXPORT,
    CONF_DEBUG_LOG,
    DEFAULT_DEBUG_LOG,
)

_LOGGER = logging.getLogger(__name__)

# Entity selectors
SENSOR_ENTITY_SELECTOR = EntitySelector(
    EntitySelectorConfig(domain=["sensor", "number", "input_number"])
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

# ---------------------------------------------------------------------------
# Schema-marker builders
#
# Each field behaves differently between the create form (``current is None``)
# and the edit/reconfigure form (``current`` is the existing data). These three
# helpers capture the three prefill styles used across the flows so the create
# and edit schemas can share one definition each.
# ---------------------------------------------------------------------------
def _prefill_marker(key, current, required=False, edit_default=None):
    """Bare on create; ``default=current[key]`` on edit.

    Used for name/entity fields that historically prefilled with ``default=``.
    ``edit_default`` is the fallback when the key is absent on edit.
    """
    cls = vol.Required if required else vol.Optional
    if current is None:
        return cls(key)
    if edit_default is not None:
        return cls(key, default=current.get(key, edit_default))
    return cls(key, default=current.get(key))


def _suggest_marker(key, current, required=False):
    """Bare on create; ``suggested_value=current[key]`` on edit.

    Used for optional entity selectors and optional numerics that must stay
    clearable (suggested_value doesn't force a value back in).
    """
    cls = vol.Required if required else vol.Optional
    if current is None:
        return cls(key)
    return cls(key, description={"suggested_value": current.get(key)})


def _default_marker(key, current, default, required=False):
    """``default=default`` on create; ``default=current.get(key, default)`` on edit.

    Used for numerics/booleans/selects that always carry a concrete default.
    """
    cls = vol.Required if required else vol.Optional
    if current is None:
        return cls(key, default=default)
    return cls(key, default=current.get(key, default))


def _global_schema(current: dict | None) -> vol.Schema:
    """Schema for the global setup (create: current=None) / options (edit) forms."""
    return vol.Schema({
        # Optional entity fields use suggested_value so they stay CLEARABLE on
        # edit (a default= would re-insert the old value on every save).
        _suggest_marker(CONF_GRID_EXPORT, current): SENSOR_ENTITY_SELECTOR,
        _prefill_marker(CONF_SOLAR_PRODUCTION, current, required=True): SENSOR_ENTITY_SELECTOR,
        _suggest_marker(CONF_HOUSE_LOAD, current): SENSOR_ENTITY_SELECTOR,
        _suggest_marker(CONF_DEMAND_POWER_CURRENT_PEAK_SENSOR, current): SENSOR_ONLY_SELECTOR,
        _suggest_marker(CONF_DEMAND_POWER_CONSUMED_SENSOR, current): SENSOR_ONLY_SELECTOR,
        _suggest_marker(CONF_DEMAND_MONTH_MAX_SENSOR, current): SENSOR_ONLY_SELECTOR,
        _suggest_marker(CONF_PEAK_TARIFF_SENSOR, current): BINARY_SENSOR_ENTITY_SELECTOR,
        _suggest_marker(CONF_VOLTAGE, current): SENSOR_ENTITY_SELECTOR,
        _default_marker(CONF_MINIMAL_PEAK_WARNING, current, DEFAULT_MINIMAL_PEAK_WARNING): vol.All(vol.Coerce(float), vol.Range(min=0)),
        _default_marker(CONF_PEAK_HIGH_SAFETY, current, DEFAULT_PEAK_HIGH_SAFETY): vol.All(vol.Coerce(float), vol.Range(min=0)),
        _default_marker(CONF_PEAK_CRITICAL_SAFETY, current, DEFAULT_PEAK_CRITICAL_SAFETY): vol.All(vol.Coerce(float), vol.Range(min=0)),
        # Electricity prices (€/kWh) for the off_peak-mode breakeven decision:
        # selectable sensor/number entities (dynamic tariffs), read each cycle.
        # Unset ⇒ the built-in defaults.
        _suggest_marker(CONF_COST_PEAK, current): SENSOR_ENTITY_SELECTOR,
        _suggest_marker(CONF_COST_OFFPEAK, current): SENSOR_ENTITY_SELECTOR,
        _suggest_marker(CONF_GAIN_EXPORT, current): SENSOR_ENTITY_SELECTOR,
        # Opt-in per-cycle debug log to ems_debug.log (off by default).
        _default_marker(CONF_DEBUG_LOG, current, DEFAULT_DEBUG_LOG): bool,
    })


def _appliance_schema(current: dict | None) -> vol.Schema:
    """Schema for the appliance subentry create (current=None) / reconfigure forms."""
    return vol.Schema({
        _prefill_marker(CONF_NAME, current, required=True, edit_default="Appliance"): str,
        _prefill_marker(CONF_ENTITY_ID, current, required=True): SWITCH_ENTITY_SELECTOR,
        _default_marker(CONF_PRIORITY, current, DEFAULT_PRIORITY, required=True): vol.All(int, vol.Range(0, 100)),
        # Required "ampere to use" number entity; its own min/max become the
        # appliance's amp limits (see control.build_appliance_inputs), so there
        # are no separate min/max fields.
        _prefill_marker(CONF_FIXED_AMPS, current, required=True): NUMBER_ENTITY_SELECTOR,
        _default_marker(CONF_FIXED_AMPS_HYSTERESIS_SECONDS, current, DEFAULT_FIXED_AMPS_HYSTERESIS_SECONDS): vol.All(int, vol.Range(min=0)),
        _suggest_marker(CONF_POWER_SENSOR, current): SENSOR_ENTITY_SELECTOR,
        _suggest_marker(CONF_PARTIAL_MIN_EXCESS, current): vol.All(int, vol.Range(0, 5000)),
        _suggest_marker(CONF_CONNECTED_SENSOR, current): BINARY_SENSOR_ENTITY_SELECTOR,
        # EV charger: the car's SoC sensor (%). Optional and clearable; setting
        # it is what turns on the force-charge-below-SoC feature (the threshold
        # itself is a number entity, not a form field).
        _suggest_marker(CONF_EV_SOC_SENSOR, current): SENSOR_ENTITY_SELECTOR,
        # Hysteresis
        _default_marker(CONF_HYSTERESIS_ON_OFF_SECONDS, current, DEFAULT_HYSTERESIS): vol.All(int, vol.Range(min=0)),
        _default_marker(CONF_AVERAGING_WINDOW, current, DEFAULT_AVERAGING): vol.All(int, vol.Range(min=30)),
    })


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

        return self.async_show_form(
            step_id="user", data_schema=_global_schema(None), errors=errors
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
                # Optional entity fields (suggested_value markers) are omitted
                # from user_input when the user clears them. Drop any that were
                # left empty so they can actually be removed — e.g. clearing the
                # demand sensors disables the quarterly-demand feature.
                for key in (
                    CONF_GRID_EXPORT,
                    CONF_HOUSE_LOAD,
                    CONF_DEMAND_POWER_CURRENT_PEAK_SENSOR,
                    CONF_DEMAND_POWER_CONSUMED_SENSOR,
                    CONF_DEMAND_MONTH_MAX_SENSOR,
                    CONF_PEAK_TARIFF_SENSOR,
                    CONF_VOLTAGE,
                    CONF_COST_PEAK,
                    CONF_COST_OFFPEAK,
                    CONF_GAIN_EXPORT,
                ):
                    if key not in user_input:
                        self.data.pop(key, None)
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=self.data
                )
                # Preserve entry.options — returning {} here would wipe the
                # persisted auto-switching state and runtime-mode fallback map.
                return self.async_create_entry(data=dict(self.config_entry.options))

        return self.async_show_form(
            step_id="user",
            data_schema=_global_schema(self.data),
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

    def _existing_names(self, exclude_subentry_id: str | None = None) -> set[str]:
        """Names of the already-configured appliances (lowercased)."""
        entry = self._get_entry()
        return {
            str(se.data.get(CONF_NAME, "")).strip().lower()
            for se in entry.subentries.values()
            if se.subentry_type == SUBENTRY_TYPE_APPLIANCE
            and se.subentry_id != exclude_subentry_id
        }

    # ---------------------- Create new appliance ----------------------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle creation of a new appliance."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Names must be unique: entities, logs and the UI all identify
            # appliances by name.
            if str(user_input.get(CONF_NAME, "")).strip().lower() in self._existing_names():
                errors[CONF_NAME] = "duplicate_name"
            if not errors:
                self._data.update(user_input)
                title = self._data.get(CONF_NAME, "Appliance")
                return self.async_create_entry(
                    title=title,
                    data=self._data,
                )

        return self.async_show_form(
            step_id="user", data_schema=_appliance_schema(None), errors=errors
        )

    # ---------------------- Reconfigure existing ----------------------
    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Start reconfiguration."""
        try:
            subentry = self._get_reconfigure_subentry()
        except Exception:  # entered without a valid subentry context
            return self.async_abort(reason="reconfigure_not_supported")

        self._data = dict(subentry.data)
        self._subentry_id = subentry.subentry_id

        return await self.async_step_reconfigure_basic(user_input)

    async def async_step_reconfigure_basic(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show form and handle update for reconfigure."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # A rename must not collide with another appliance's name.
            new_name = str(user_input.get(CONF_NAME, "")).strip().lower()
            if new_name in self._existing_names(exclude_subentry_id=self._subentry_id):
                return self.async_show_form(
                    step_id="reconfigure_basic",
                    data_schema=_appliance_schema({**self._data, **user_input}),
                    errors={CONF_NAME: "duplicate_name"},
                )
            self._data.update(user_input)
            # Optional entity fields are omitted from user_input when the user
            # clears them. Drop any that were left empty so they can be removed.
            for key in (
                CONF_POWER_SENSOR,
                CONF_PARTIAL_MIN_EXCESS,
                CONF_CONNECTED_SENSOR,
                CONF_EV_SOC_SENSOR,
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

        return self.async_show_form(
            step_id="reconfigure_basic",
            data_schema=_appliance_schema(self._data),
            errors=errors,
        )

    # ---------------------- Remove appliance ----------------------
    async def async_step_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle removal of an appliance subentry."""
        try:
            subentry = self._get_reconfigure_subentry()
        except Exception:  # entered without a valid subentry context
            return self.async_abort(reason="remove_failed")

        if user_input is None:
            name = subentry.data.get(CONF_NAME, "this appliance")
            return self.async_show_form(
                step_id="remove",
                data_schema=vol.Schema({}),
                description_placeholders={"name": name},
            )

        # Confirmed: actually REMOVE the subentry (updating it to empty data
        # would leave a ghost appliance with no name behind).
        try:
            self.hass.config_entries.async_remove_subentry(
                self._get_entry(), subentry.subentry_id
            )
            return self.async_abort(reason="subentry_removed")
        except Exception as err:
            _LOGGER.exception("Failed to remove subentry: %s", err)
        return self.async_abort(reason="remove_failed")