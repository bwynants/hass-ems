"""Battery SoC-protection reads for the Energy Management System.

Pure helpers that derive the protection SoC, threshold and mode from the
appliances flagged as batteries. They are stateless reads; the coordinator
stores the results (``self._last_soc`` etc.) for use by the optimizer.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant

from .const import (
    CONF_IS_BATTERY,
    CONF_BATTERY_SOC_SENSOR,
    CONF_MIN_SOC,
    DEFAULT_MIN_SOC,
    CONF_BATTERY_PROTECTION_MODE,
    DEFAULT_BATTERY_MODE,
    BATTERY_MODE_STRICT,
    BATTERY_MODE_RELAXED,
)
from .helpers import read_sensor_optional


def current_soc(hass: HomeAssistant, appliances):
    """Return the battery SoC used for protection, or None.

    Read from the SoC sensor of every appliance flagged as a battery; when
    more than one battery is configured the lowest reading is used (the most
    conservative value for protecting the others). Returns None when no
    battery is configured or none reports a usable value.
    """
    socs = []
    for app in appliances:
        if not app.get(CONF_IS_BATTERY, False):
            continue
        val = read_sensor_optional(hass, app.get(CONF_BATTERY_SOC_SENSOR))
        if val is not None:
            socs.append(val)
    return min(socs) if socs else None


def battery_min_soc(appliances):
    """Return the SoC protection threshold from the battery appliances.

    Each battery carries its own ``min_soc``; with multiple batteries the
    highest threshold wins (the most conservative). Returns None when no
    battery is configured, which disables SoC protection entirely.
    """
    thresholds = [
        app.get(CONF_MIN_SOC, DEFAULT_MIN_SOC)
        for app in appliances
        if app.get(CONF_IS_BATTERY, False)
    ]
    return max(thresholds) if thresholds else None


def battery_protection_mode(appliances):
    """Return the active SoC protection mode, or None.

    ``strict`` hard-blocks appliances below the threshold; ``relaxed`` lets
    them keep running but only while solar surplus covers them. With several
    batteries the strictest mode wins. Returns None when no battery exists.
    """
    modes = [
        app.get(CONF_BATTERY_PROTECTION_MODE, DEFAULT_BATTERY_MODE)
        for app in appliances
        if app.get(CONF_IS_BATTERY, False)
    ]
    if not modes:
        return None
    return BATTERY_MODE_STRICT if any(m == BATTERY_MODE_STRICT for m in modes) else BATTERY_MODE_RELAXED
