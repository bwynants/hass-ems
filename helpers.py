"""Home Assistant sensor-reading helpers for the Energy Management System.

These touch ``hass.states`` so they live in the integration (not the pure
``ems_core`` library). Power normalisation is delegated to ``ems_core.energy``.
"""
from __future__ import annotations

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
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


def _read_normalised(hass: HomeAssistant, entity_id: str | None) -> tuple[float, bool] | None:
    """Return ``(value, is_power)`` for a numeric entity, or ``None``.

    Power-class values are unit-normalised to watts; the single place the
    device-class/unit normalisation happens.
    """
    result = _read_float(hass, entity_id)
    if result is None:
        return None
    val, state = result
    if state.attributes.get(DEVICE_CLASS) == DEVICE_CLASS_POWER:
        return normalise_power(val, state.attributes.get(UNIT_OF_MEASUREMENT)), True
    return val, False


def read_sensor_optional(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Read a numeric sensor (watts-normalised for power) or return ``None``."""
    result = _read_normalised(hass, entity_id)
    return None if result is None else result[0]


def read_sensor(hass: HomeAssistant, entity_id: str | None, default: float = 0.0) -> float:
    """Read a numeric sensor (watts-normalised for power) or return ``default``."""
    val = read_sensor_optional(hass, entity_id)
    return default if val is None else val


def read_value(hass: HomeAssistant, source, default: float) -> float:
    """Read a config value that is an entity id, or fall back to ``default``.

    Accepts a plain number too (legacy entries stored the value directly);
    an unset source or an unavailable entity yields ``default``.
    """
    if source is None or source == "":
        return default
    if isinstance(source, (int, float)):
        return float(source)
    val = read_sensor_optional(hass, source)
    return default if val is None else val


def sensor_stale(hass: HomeAssistant, entity_id: str | None, max_age_seconds: float, now) -> bool:
    """True when the entity exists but hasn't reported within ``max_age_seconds``.

    Detects a *frozen* source — an integration that is still present but no
    longer updating (``last_reported`` stops advancing). A missing/unset entity
    is NOT reported stale here (that case is handled by the caller's own
    fallback, e.g. grid → solar−house); only a present-but-frozen one is.
    """
    if not entity_id:
        return False
    state = hass.states.get(entity_id)
    if state is None:
        return False
    return (now - state.last_reported).total_seconds() > max_age_seconds


def read_number_bounds(
    hass: HomeAssistant, entity_id: str | None
) -> tuple[float, float] | None:
    """Return ``(min, max)`` from a number/input_number entity's attributes.

    Both ``number`` and ``input_number`` expose ``min``/``max`` attributes.
    Returns ``None`` when the entity is unset, unavailable, or doesn't expose
    both bounds — so the caller can fall back to configured defaults.
    """
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in UNAVAILABLE_STATES:
        return None
    lo = state.attributes.get("min")
    hi = state.attributes.get("max")
    if lo is None or hi is None:
        return None
    try:
        return float(lo), float(hi)
    except (ValueError, TypeError):
        return None


def read_binary(
    hass: HomeAssistant,
    entity_id: str | None,
    *,
    missing: bool,
    unavailable: bool,
) -> bool:
    """Read an on/off entity with explicit fallback semantics.

    ``missing`` is returned when no entity is configured; ``unavailable`` when
    the entity exists but is currently unavailable/unknown. Callers choose the
    safe direction per use — e.g. an unavailable connected-sensor should not be
    treated as "disconnected" (which would drop a running appliance with no
    hysteresis), and an unavailable tariff sensor should assume peak.
    """
    if not entity_id:
        return missing
    state = hass.states.get(entity_id)
    if state is None or state.state in UNAVAILABLE_STATES:
        return unavailable
    return state.state == STATE_ON


def read_kw(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Read a sensor value in kW, or None.

    Power-class sources are unit-normalised to watts then divided by 1000;
    non-power sources are assumed to already be in kW.
    """
    result = _read_normalised(hass, entity_id)
    if result is None:
        return None
    val, is_power = result
    return val / 1000.0 if is_power else val
