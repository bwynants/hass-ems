"""Home Assistant sensor-reading helpers for the Energy Management System.

These touch ``hass.states`` so they live in the integration (not the pure
``ems_core`` library). Power normalisation is delegated to ``ems_core.energy``.
"""
from __future__ import annotations

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from .ems_core.energy import normalise_power

# Sensor-reading constants.
UNAVAILABLE_STATES = {STATE_UNAVAILABLE, STATE_UNKNOWN, "none", ""}
DEVICE_CLASS = "device_class"
DEVICE_CLASS_POWER = "power"
UNIT_OF_MEASUREMENT = "unit_of_measurement"


def _read_float(hass: HomeAssistant, entity_id: str | None):
    """Return ``(value, state)`` for a numeric entity, or ``None`` if unusable."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in UNAVAILABLE_STATES:
        return None
    try:
        return float(state.state), state
    except (ValueError, TypeError):
        return None


def read_sensor(hass: HomeAssistant, entity_id: str | None, default: float = 0.0) -> float:
    """Read a numeric sensor (watts-normalised for power) or return ``default``."""
    result = _read_float(hass, entity_id)
    if result is None:
        return default
    val, state = result
    if state.attributes.get(DEVICE_CLASS) == DEVICE_CLASS_POWER:
        val = normalise_power(val, state.attributes.get(UNIT_OF_MEASUREMENT))
    return val


def read_sensor_optional(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Read a numeric sensor (watts-normalised for power) or return ``None``."""
    result = _read_float(hass, entity_id)
    if result is None:
        return None
    val, state = result
    if state.attributes.get(DEVICE_CLASS) == DEVICE_CLASS_POWER:
        val = normalise_power(val, state.attributes.get(UNIT_OF_MEASUREMENT))
    return val


def read_kw(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Read a sensor value in kW, or None.

    Power-class sources are unit-normalised to watts then divided by 1000;
    non-power sources are assumed to already be in kW.
    """
    result = _read_float(hass, entity_id)
    if result is None:
        return None
    val, state = result
    if state.attributes.get(DEVICE_CLASS) == DEVICE_CLASS_POWER:
        val = normalise_power(val, state.attributes.get(UNIT_OF_MEASUREMENT)) / 1000.0
    return val
