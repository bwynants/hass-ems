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
    CONF_HYSTERESIS_ON_OFF_SECONDS, CONF_AVERAGING_WINDOW,
    CONF_EV_SOC_SENSOR, CONF_FORCE_CHARGE_BELOW_SOC, DEFAULT_FORCE_CHARGE_BELOW_SOC,
    CONF_RUNTIME_MODE, CONF_RUNTIME_MODES,
    CONF_COST_PEAK, CONF_COST_OFFPEAK, CONF_GAIN_EXPORT,
    DEFAULT_MIN_AMPS, DEFAULT_MAX_AMPS, DEFAULT_PRIORITY, DEFAULT_HYSTERESIS,
    DEFAULT_AVERAGING,
    DEFAULT_FIXED_AMPS_HYSTERESIS_SECONDS, DEFAULT_PARTIAL_MIN_EXCESS,
    DEFAULT_MODE,
    DEFAULT_COST_PEAK, DEFAULT_COST_OFFPEAK, DEFAULT_GAIN_EXPORT,
    FIXED_AMPS_STEP, MODE_UNMANAGED,
)
from .helpers import (
    read_sensor, read_sensor_optional, read_binary, read_value,
    read_number_bounds, read_number_step, UNAVAILABLE_STATES,
)
from .ems_core import ApplianceInput, EnergyInput

_LOGGER = logging.getLogger(__name__)


def _resolve_mode(coordinator, app: dict) -> str:
    """Resolve an appliance's persisted runtime mode (or the default)."""
    return coordinator.get_appliance_persistent_setting(
        app["subentry_id"], CONF_RUNTIME_MODE, CONF_RUNTIME_MODES, DEFAULT_MODE,
    )


def _resolve_force_charge_below_soc(coordinator, app: dict) -> float | None:
    """The car-SoC force-charge threshold, or None for a non-EV appliance.

    Only appliances with a SoC sensor have the number entity that sets this;
    for the rest the feature doesn't exist (None), so the core never forces.
    """
    if not app.get(CONF_EV_SOC_SENSOR):
        return None
    value = coordinator.get_appliance_persistent_setting(
        app["subentry_id"], CONF_FORCE_CHARGE_BELOW_SOC, None, DEFAULT_FORCE_CHARGE_BELOW_SOC,
    )
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(DEFAULT_FORCE_CHARGE_BELOW_SOC)


def _is_on(hass, entity_id: str | None, assumed_on: bool) -> bool:
    """The controlled entity's on state.

    When the entity is momentarily unavailable, fall back to ``assumed_on``
    (our own last decision): a running appliance whose switch drops out must
    keep counting toward the managed add-back and stay eligible for
    critical-shed throttling instead of silently vanishing from the budget.
    """
    return read_binary(hass, entity_id, missing=False, unavailable=assumed_on)


def _is_connected(hass, entity_id: str | None) -> bool:
    """Connected unless the sensor explicitly reports off.

    An unavailable sensor is treated as connected: a momentary dropout must not
    force-skip (and thus power-cycle) a running appliance with no hysteresis.
    """
    return read_binary(hass, entity_id, missing=True, unavailable=True)


def _measured_amps(hass, entity_id: str | None, voltage: float) -> float | None:
    if not entity_id:
        return None
    watts = read_sensor_optional(hass, entity_id)
    if watts is None or voltage <= 0:
        return None
    return watts / voltage


def build_energy_input(hass, config: dict, voltage: float) -> EnergyInput:
    """Read the grid/solar snapshot and tariff context into an EnergyInput."""
    # No sensor OR an unavailable sensor ⇒ assume peak (the conservative
    # direction: off_peak-mode appliances stay breakeven-gated).
    is_peak = read_binary(
        hass, config.get(CONF_PEAK_TARIFF_SENSOR), missing=True, unavailable=True
    )
    return EnergyInput(
        grid_w=read_sensor_optional(hass, config.get(CONF_GRID_EXPORT)),
        solar_w=read_sensor(hass, config.get(CONF_SOLAR_PRODUCTION)),
        house_load_w=read_sensor(hass, config.get(CONF_HOUSE_LOAD)),
        voltage=voltage,
        off_peak=not is_peak,
        # Prices come from selectable sensor/number entities (read fresh each
        # cycle, so dynamic tariffs work); legacy plain numbers still resolve.
        cost_peak=read_value(hass, config.get(CONF_COST_PEAK), DEFAULT_COST_PEAK),
        cost_offpeak=read_value(hass, config.get(CONF_COST_OFFPEAK), DEFAULT_COST_OFFPEAK),
        gain_export=read_value(hass, config.get(CONF_GAIN_EXPORT), DEFAULT_GAIN_EXPORT),
    )


def build_appliance_inputs(hass, appliances, coordinator, voltage: float):
    """Resolve each appliance's sensors/config/mode into ApplianceInput objects.

    Returns ``(inputs, raw_by_uid)`` where ``raw_by_uid`` maps the appliance's
    stable uid (config subentry id) to its raw config dict (used later to apply
    commands to real entities).
    """
    inputs: list[ApplianceInput] = []
    raw_by_uid: dict[str, dict] = {}
    for app in appliances:
        name = app[CONF_NAME]
        uid = app["subentry_id"]
        fixed_entity = app.get(CONF_FIXED_AMPS)
        # The "amps to use" (number/input_number) entity carries its own min/max
        # bounds, so those are used as the appliance's min/max amps rather than
        # separate config fields. Fall back to the configured values (or
        # defaults) for on/off appliances, or if the entity omits bounds.
        bounds = read_number_bounds(hass, fixed_entity) if fixed_entity else None
        if bounds is not None:
            min_amps, max_amps = bounds
        else:
            min_amps = app.get(CONF_MIN_AMPS, DEFAULT_MIN_AMPS)
            max_amps = app.get(CONF_MAX_AMPS, DEFAULT_MAX_AMPS)
        # ...and its own step is the setpoint granularity, so EMS writes values
        # the appliance can actually take (a 0.1 A charger is stepped in 0.1 A,
        # not rounded down to the 0.5 A default).
        step = read_number_step(hass, fixed_entity) if fixed_entity else None
        # Our own last on/off decision — the fallback for an unavailable
        # controlled entity.
        runtime = coordinator.appliance_states.get(uid)
        assumed_on = bool(runtime.is_on) if runtime else False
        # Measured draw. Hold the last real reading across a power-sensor
        # dropout so the surplus add-back never falls through to the commanded
        # setpoint (which would run the setpoint away — see energy.actual_amps).
        power_entity = app.get(CONF_POWER_SENSOR)
        has_power_sensor = bool(power_entity)
        measured = _measured_amps(hass, power_entity, voltage)
        if has_power_sensor and runtime is not None:
            if measured is not None:
                runtime.last_measured_amps = measured
            elif runtime.last_measured_amps is not None:
                measured = runtime.last_measured_amps
        # Car SoC (EV chargers). Held across a sensor dropout the same way,
        # but only while the car stays plugged in: cloud car sensors go
        # `unknown` intermittently and one blank cycle must not release the
        # force-charge override. A car swap always passes through "not
        # connected", which clears the hold, so it can't leak to another car.
        connected = _is_connected(hass, app.get(CONF_CONNECTED_SENSOR))
        soc_entity = app.get(CONF_EV_SOC_SENSOR)
        soc = read_sensor_optional(hass, soc_entity) if soc_entity else None
        if soc_entity and runtime is not None:
            if not connected:
                runtime.last_soc = None
            elif soc is not None:
                runtime.last_soc = soc
            elif runtime.last_soc is not None:
                soc = runtime.last_soc
        inputs.append(
            ApplianceInput(
                # Stable id keying the runtime state: the config subentry id,
                # so a rename doesn't reset the appliance's runtime state.
                uid=uid,
                name=name,
                priority=app.get(CONF_PRIORITY, DEFAULT_PRIORITY),
                min_amps=min_amps,
                max_amps=max_amps,
                mode=_resolve_mode(coordinator, app),
                has_fixed_amps=bool(fixed_entity),
                fixed_amps_value=read_sensor_optional(hass, fixed_entity) if fixed_entity else None,
                fixed_amps_hold_seconds=app.get(
                    CONF_FIXED_AMPS_HYSTERESIS_SECONDS, DEFAULT_FIXED_AMPS_HYSTERESIS_SECONDS
                ),
                fixed_amps_step=step if step is not None else FIXED_AMPS_STEP,
                partial_min_excess=app.get(CONF_PARTIAL_MIN_EXCESS, DEFAULT_PARTIAL_MIN_EXCESS),
                hysteresis_seconds=app.get(CONF_HYSTERESIS_ON_OFF_SECONDS, DEFAULT_HYSTERESIS),
                averaging_window_seconds=app.get(CONF_AVERAGING_WINDOW, DEFAULT_AVERAGING),
                connected=connected,
                entity_on=_is_on(hass, app.get(CONF_ENTITY_ID), assumed_on),
                measured_amps=measured,
                has_power_sensor=has_power_sensor,
                soc=soc,
                force_charge_below_soc=_resolve_force_charge_below_soc(coordinator, app),
            )
        )
        raw_by_uid[uid] = app
    return inputs, raw_by_uid


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


async def apply_commands(hass, active, fixed_amps_commands, inputs, raw_by_uid) -> None:
    """Apply the core's decisions to Home Assistant.

    ``active`` and the commands are keyed by appliance uid (config subentry id),
    so duplicate display names can't misdirect a command. Writes the fixed_amps
    setpoints, then switches managed appliances on/off to match ``active``
    (skipping unmanaged / missing / unavailable entities).
    """
    for cmd in fixed_amps_commands:
        raw = raw_by_uid.get(cmd.uid)
        entity_id = raw.get(CONF_FIXED_AMPS) if raw else None
        if entity_id:
            await _write_fixed_amps(hass, entity_id, cmd.value, raw.get(CONF_NAME, cmd.uid))

    active_set = set(active)
    for app in inputs:
        if app.mode == MODE_UNMANAGED:
            continue
        raw = raw_by_uid.get(app.uid)
        entity_id = raw.get(CONF_ENTITY_ID) if raw else None
        if not entity_id:
            continue
        state = hass.states.get(entity_id)
        if state is None or state.state in UNAVAILABLE_STATES:
            _LOGGER.debug("apply states: %s unavailable, skipped", entity_id)
            continue

        should_be_on = app.uid in active_set
        domain = split_entity_id(entity_id)[0]
        currently_on = state.state == STATE_ON

        # One failing device must not abort the loop — the remaining
        # appliances (e.g. the rest of a critical shed) still get commanded.
        try:
            if should_be_on and not currently_on:
                _LOGGER.info("turning ON %s (%s)", entity_id, app.name)
                await hass.services.async_call(domain, "turn_on", {"entity_id": entity_id}, blocking=True)
            elif not should_be_on and currently_on:
                _LOGGER.info("turning OFF %s (%s)", entity_id, app.name)
                await hass.services.async_call(domain, "turn_off", {"entity_id": entity_id}, blocking=True)
        except Exception as err:
            _LOGGER.warning("Failed to switch %s (%s): %s", entity_id, app.name, err)
