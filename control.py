"""Shell adapter between Home Assistant and the pure ``ems_core`` library.

Reads sensors/config into ``ems_core`` dataclasses (``build_appliance_inputs``,
``build_energy_input``) and applies the core's decisions via HA services
(``apply_commands``). All Home-Assistant-specific I/O lives here so the core
stays pure.
"""
from __future__ import annotations

import logging

from homeassistant.const import STATE_ON
from homeassistant.core import split_entity_id

from .const import (
    CONF_GRID_EXPORT, CONF_SOLAR_PRODUCTION, CONF_HOUSE_LOAD,
    CONF_PEAK_TARIFF_SENSOR, CONF_NAME, CONF_ENTITY_ID, CONF_PRIORITY,
    CONF_MIN_AMPS, CONF_MAX_AMPS, CONF_FIXED_AMPS, CONF_FIXED_AMPS_HYSTERESIS_SECONDS,
    CONF_POWER_SENSOR, CONF_PARTIAL_MIN_EXCESS, CONF_CONNECTED_SENSOR,
    CONF_HYSTERESIS_ON_OFF_SECONDS, CONF_AVERAGING_WINDOW, CONF_MIN_SOC_OVERRIDE,
    CONF_IGNORE_BATTERY_PROTECTION, CONF_SOC_HYSTERESIS_PERCENT, CONF_IS_BATTERY,
    CONF_BATTERY_SOC_SENSOR, CONF_MIN_SOC, CONF_BATTERY_PROTECTION_MODE,
    CONF_RUNTIME_MODE, CONF_RUNTIME_MODES,
    DEFAULT_MIN_AMPS, DEFAULT_MAX_AMPS, DEFAULT_PRIORITY, DEFAULT_HYSTERESIS,
    DEFAULT_AVERAGING, DEFAULT_SOC_HYSTERESIS, DEFAULT_IGNORE_PROTECTION,
    DEFAULT_FIXED_AMPS_HYSTERESIS_SECONDS, DEFAULT_PARTIAL_MIN_EXCESS,
    DEFAULT_MIN_SOC, DEFAULT_BATTERY_MODE, DEFAULT_MODE,
    DEFAULT_COST_PEAK, DEFAULT_COST_OFFPEAK, DEFAULT_GAIN_EXPORT,
    MODE_UNMANAGED,
)
from .helpers import read_sensor, read_sensor_optional, UNAVAILABLE_STATES
from .ems_core import ApplianceInput, EnergyInput

_LOGGER = logging.getLogger(__name__)


def _resolve_mode(coordinator, app: dict) -> str:
    """Resolve an appliance's persisted runtime mode (or the default)."""
    appliance_id = app.get("subentry_id") or app.get("id")
    if not appliance_id:
        _LOGGER.warning("Appliance %s has no subentry_id; using default mode", app.get(CONF_NAME))
        return DEFAULT_MODE
    return coordinator.get_appliance_persistent_setting(
        appliance_id, CONF_RUNTIME_MODE, CONF_RUNTIME_MODES, DEFAULT_MODE,
    )


def _is_on(hass, entity_id: str | None) -> bool:
    if not entity_id:
        return False
    state = hass.states.get(entity_id)
    return state is not None and state.state == STATE_ON


def _is_connected(hass, entity_id: str | None) -> bool:
    if not entity_id:
        return True
    state = hass.states.get(entity_id)
    return bool(state and state.state == "on")


def _measured_amps(hass, entity_id: str | None, voltage: float) -> float | None:
    if not entity_id:
        return None
    watts = read_sensor_optional(hass, entity_id)
    if watts is None or voltage <= 0:
        return None
    return watts / voltage


def build_energy_input(hass, config: dict, voltage: float) -> EnergyInput:
    """Read the grid/solar snapshot and tariff context into an EnergyInput."""
    peak_entity = config.get(CONF_PEAK_TARIFF_SENSOR)
    if not peak_entity:
        is_peak = True  # no sensor ⇒ assume peak (off_peak False), as before
    else:
        st = hass.states.get(peak_entity)
        is_peak = bool(st and st.state == "on")
    return EnergyInput(
        grid_w=read_sensor_optional(hass, config.get(CONF_GRID_EXPORT)),
        solar_w=read_sensor(hass, config.get(CONF_SOLAR_PRODUCTION)),
        house_load_w=read_sensor(hass, config.get(CONF_HOUSE_LOAD)),
        voltage=voltage,
        off_peak=not is_peak,
        cost_peak=config.get("cost_peak", DEFAULT_COST_PEAK),
        cost_offpeak=config.get("cost_offpeak", DEFAULT_COST_OFFPEAK),
        gain_export=config.get("gain_export", DEFAULT_GAIN_EXPORT),
    )


def build_appliance_inputs(hass, appliances, coordinator, voltage: float):
    """Resolve each appliance's sensors/config/mode into ApplianceInput objects.

    Returns ``(inputs, raw_by_name)`` where ``raw_by_name`` maps the appliance
    name to its raw config dict (used later to apply commands to real entities).
    """
    inputs: list[ApplianceInput] = []
    raw_by_name: dict[str, dict] = {}
    for app in appliances:
        name = app[CONF_NAME]
        is_battery = app.get(CONF_IS_BATTERY, False)
        fixed_entity = app.get(CONF_FIXED_AMPS)
        inputs.append(
            ApplianceInput(
                name=name,
                priority=app.get(CONF_PRIORITY, DEFAULT_PRIORITY),
                min_amps=app.get(CONF_MIN_AMPS, DEFAULT_MIN_AMPS),
                max_amps=app.get(CONF_MAX_AMPS, DEFAULT_MAX_AMPS),
                mode=_resolve_mode(coordinator, app),
                has_fixed_amps=bool(fixed_entity),
                fixed_amps_value=read_sensor_optional(hass, fixed_entity) if fixed_entity else None,
                fixed_amps_hold_seconds=app.get(
                    CONF_FIXED_AMPS_HYSTERESIS_SECONDS, DEFAULT_FIXED_AMPS_HYSTERESIS_SECONDS
                ),
                partial_min_excess=app.get(CONF_PARTIAL_MIN_EXCESS, DEFAULT_PARTIAL_MIN_EXCESS),
                hysteresis_seconds=app.get(CONF_HYSTERESIS_ON_OFF_SECONDS, DEFAULT_HYSTERESIS),
                averaging_window_seconds=app.get(CONF_AVERAGING_WINDOW, DEFAULT_AVERAGING),
                connected=_is_connected(hass, app.get(CONF_CONNECTED_SENSOR)),
                entity_on=_is_on(hass, app.get(CONF_ENTITY_ID)),
                measured_amps=_measured_amps(hass, app.get(CONF_POWER_SENSOR), voltage),
                is_battery=is_battery,
                ignore_battery_protection=app.get(
                    CONF_IGNORE_BATTERY_PROTECTION, DEFAULT_IGNORE_PROTECTION
                ),
                min_soc_override=app.get(CONF_MIN_SOC_OVERRIDE),
                soc_hysteresis_percent=app.get(CONF_SOC_HYSTERESIS_PERCENT, DEFAULT_SOC_HYSTERESIS),
                battery_soc=read_sensor_optional(hass, app.get(CONF_BATTERY_SOC_SENSOR)) if is_battery else None,
                battery_min_soc=app.get(CONF_MIN_SOC, DEFAULT_MIN_SOC) if is_battery else None,
                battery_protection_mode=app.get(CONF_BATTERY_PROTECTION_MODE, DEFAULT_BATTERY_MODE) if is_battery else None,
            )
        )
        raw_by_name[name] = app
    return inputs, raw_by_name


async def _write_fixed_amps(hass, entity_id: str, value: float, name: str) -> None:
    # The fixed_amps entity may be an input_number or a number entity; both
    # domains expose an identical set_value service.
    domain = split_entity_id(entity_id)[0]
    try:
        await hass.services.async_call(
            domain, "set_value",
            {"entity_id": entity_id, "value": value}, blocking=True,
        )
        _LOGGER.debug("Updated %s fixed_amps entity to %.1f amps", name, value)
    except Exception as err:
        _LOGGER.warning("Failed to update fixed_amps entity %s: %s", entity_id, err)


async def apply_commands(hass, active, fixed_amps_commands, inputs, raw_by_name) -> None:
    """Apply the core's decisions to Home Assistant.

    Writes the fixed_amps setpoints, then switches managed appliances on/off to
    match ``active`` (skipping unmanaged / missing / unavailable entities).
    """
    for cmd in fixed_amps_commands:
        raw = raw_by_name.get(cmd.name)
        entity_id = raw.get(CONF_FIXED_AMPS) if raw else None
        if entity_id:
            await _write_fixed_amps(hass, entity_id, cmd.value, cmd.name)

    active_set = set(active)
    _LOGGER.debug("apply states: active=%s", active_set)
    for app in inputs:
        if app.mode == MODE_UNMANAGED:
            continue
        raw = raw_by_name.get(app.name)
        entity_id = raw.get(CONF_ENTITY_ID) if raw else None
        if not entity_id:
            continue
        state = hass.states.get(entity_id)
        if state is None or state.state in UNAVAILABLE_STATES:
            _LOGGER.debug("apply states: %s unavailable, skipped", entity_id)
            continue

        should_be_on = app.name in active_set
        domain = split_entity_id(entity_id)[0]
        currently_on = state.state == STATE_ON

        if should_be_on and not currently_on:
            _LOGGER.info("turning ON %s (%s)", entity_id, app.name)
            await hass.services.async_call(domain, "turn_on", {"entity_id": entity_id}, blocking=True)
        elif not should_be_on and currently_on:
            _LOGGER.info("turning OFF %s (%s)", entity_id, app.name)
            await hass.services.async_call(domain, "turn_off", {"entity_id": entity_id}, blocking=True)
