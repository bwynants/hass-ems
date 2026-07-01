"""Battery SoC-protection reducers for the EMS core — pure functions."""
from __future__ import annotations

from .const import DEFAULT_MIN_SOC, DEFAULT_BATTERY_MODE, BATTERY_MODE_STRICT, BATTERY_MODE_RELAXED
from .models import ApplianceInput, BatteryStatus


def battery_status(appliances: list[ApplianceInput]) -> BatteryStatus:
    """Derive the SoC / threshold / protection-mode used to gate other appliances.

    Reduces over every appliance flagged as a battery:
    - ``soc`` = lowest reported SoC (most conservative for protecting others),
    - ``min_soc`` = highest configured threshold (most conservative),
    - ``protection_mode`` = strict if any battery is strict, else relaxed.
    All fields are None when no battery is configured.
    """
    batteries = [a for a in appliances if a.is_battery]

    socs = [a.battery_soc for a in batteries if a.battery_soc is not None]
    thresholds = [
        a.battery_min_soc if a.battery_min_soc is not None else DEFAULT_MIN_SOC
        for a in batteries
    ]
    modes = [a.battery_protection_mode or DEFAULT_BATTERY_MODE for a in batteries]

    soc = min(socs) if socs else None
    min_soc = max(thresholds) if thresholds else None
    if not modes:
        protection_mode = None
    else:
        protection_mode = (
            BATTERY_MODE_STRICT
            if any(m == BATTERY_MODE_STRICT for m in modes)
            else BATTERY_MODE_RELAXED
        )
    return BatteryStatus(soc=soc, min_soc=min_soc, protection_mode=protection_mode)
