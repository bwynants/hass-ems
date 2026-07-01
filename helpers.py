"""Stateless, shared helpers for the Energy Management System integration.

Pure functions and small utilities used by both the main coordinator and the
quarter-hour demand coordinator, so neither has to import private helpers from
the other.
"""
from __future__ import annotations

from collections import deque

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

# Sensor-reading constants.
UNAVAILABLE_STATES = {STATE_UNAVAILABLE, STATE_UNKNOWN, "none", ""}
DEVICE_CLASS = "device_class"
DEVICE_CLASS_POWER = "power"
UNIT_OF_MEASUREMENT = "unit_of_measurement"

# How many excess samples per appliance to keep for averaging.
APPLIANCE_HISTORY_MAXLEN = 120

# Multipliers to normalise power values to watts.
_POWER_UNIT_MULTIPLIERS: dict[str, float] = {
    "w": 1.0,
    "kw": 1000.0,
    "mw": 1_000_000.0,
}


def normalise_power(value: float, unit: str | None) -> float:
    """Convert a power reading to watts based on its unit_of_measurement."""
    if unit is None:
        return value
    return value * _POWER_UNIT_MULTIPLIERS.get(unit.lower().strip(), 1.0)


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


def read_binary(hass: HomeAssistant, entity_id: str | None) -> bool | None:
    """Read a binary sensor as True/False, or None when unavailable."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unavailable", "unknown"):
        return None
    return state.state == "on"


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


def calculate_scaled_amps(min_a: float, max_a: float, remaining_amps: float) -> float:
    """Scale amperage to the available excess, clamped between min and max."""
    if remaining_amps >= max_a:
        return float(max_a)
    if remaining_amps <= min_a:
        return float(min_a)
    return float(remaining_amps)


def calculate_breakeven(min_amps, cost_peak, cost_offpeak, gain_export):
    """Averaged excess (amps) above which running at peak beats waiting for off-peak."""
    denom = cost_peak - gain_export
    if abs(denom) > 0.001:
        return min_amps * (cost_peak - cost_offpeak) / denom
    return 0


def default_appliance_state() -> dict:
    """Return a fresh per-appliance runtime-state dict."""
    return {
        "is_on": False,
        "last_change": None,
        "last_fixed_amps": None,
        "last_fixed_amps_change": None,
        "excess_history": deque(maxlen=APPLIANCE_HISTORY_MAXLEN),
        "soc_blocked": False,
    }
