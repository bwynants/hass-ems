"""Constants for Energy Management System.

Home-Assistant-facing constants (domain, platforms, config-entry keys) live
here. The pure domain constants and numeric defaults live in
:mod:`ems_core.const` and are re-exported below so existing imports keep working.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform

# Re-export the pure domain constants / defaults from the core library.
from .ems_core.const import *  # noqa: F401,F403

DOMAIN = "energy_management_system"
MANUFACTURER = "Energy Management System"
SUBENTRY_TYPE_APPLIANCE = "appliance"

PLATFORMS = [Platform.SWITCH, Platform.SENSOR, Platform.SELECT, Platform.BINARY_SENSOR]


def get_appliances(entry: ConfigEntry) -> list[dict[str, Any]]:
    """Return appliance configs stored as config subentries.

    Include the subentry id in the returned dict so callers can persist
    changes back to the specific subentry.
    """
    return [
        {**subentry.data, "subentry_id": subentry.subentry_id}
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_TYPE_APPLIANCE
    ]

# Global configuration
CONF_SOLAR_PRODUCTION = "solar_production_sensor"
CONF_HOUSE_LOAD = "house_load_sensor"
CONF_GRID_EXPORT = "grid_export_sensor"
CONF_BATTERY_SOC_SENSOR = "battery_soc_sensor"
CONF_PEAK_TARIFF_SENSOR = "peak_tariff_sensor"
CONF_VOLTAGE = "voltage"
CONF_MIN_SOC = "min_soc"
CONF_BATTERY_PROTECTION_MODE = "battery_protection_mode"

# Electricity prices (€/kWh), used by the off_peak-mode breakeven decision.
CONF_COST_PEAK = "cost_peak"
CONF_COST_OFFPEAK = "cost_offpeak"
CONF_GAIN_EXPORT = "gain_export"

# Quarterly demand (capacity-tariff) feature
CONF_DEMAND_POWER_CURRENT_PEAK_SENSOR = "demand_power_current_peak_sensor"
CONF_DEMAND_POWER_CONSUMED_SENSOR = "demand_power_consumed_sensor"
CONF_DEMAND_MONTH_MAX_SENSOR = "demand_month_max_sensor"
CONF_MINIMAL_PEAK_WARNING = "minimal_peak_warning"
CONF_PEAK_HIGH_SAFETY = "peak_high_safety"
CONF_PEAK_CRITICAL_SAFETY = "peak_critical_safety"

CONF_RUNTIME_MODES = "runtime_modes"
CONF_RUNTIME_MODE = "runtime_mode"
CONF_AUTO_SWITCHING = "auto_switching"

# Appliance configuration
CONF_NAME = "name"
CONF_ENTITY_ID = "entity_id"
CONF_PRIORITY = "priority"
CONF_MIN_AMPS = "min_amps"
CONF_MAX_AMPS = "max_amps"
CONF_FIXED_AMPS = "fixed_amps"
CONF_FIXED_AMPS_HYSTERESIS_SECONDS = "fixed_amps_hysteresis_seconds"
CONF_POWER_SENSOR = "power_sensor"
CONF_PARTIAL_MIN_EXCESS = "partial_min_excess"
CONF_CONNECTED_SENSOR = "connected_sensor"
CONF_IS_BATTERY = "is_battery"

# Hysteresis
CONF_HYSTERESIS_ON_OFF_SECONDS = "hysteresis_on_off_seconds"
CONF_AVERAGING_WINDOW = "averaging_window_seconds"

# SoC
CONF_MIN_SOC_OVERRIDE = "min_soc_override"
CONF_IGNORE_BATTERY_PROTECTION = "ignore_battery_protection"
CONF_SOC_HYSTERESIS_PERCENT = "soc_hysteresis_percent"
