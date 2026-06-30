"""Constants for Energy Management System."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform

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
        dict({**subentry.data, "subentry_id": getattr(subentry, "subentry_id", None) or getattr(subentry, "id", None)})
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

# Quarterly demand (capacity-tariff) feature
CONF_DEMAND_POWER_CURRENT_PEAK_SENSOR = "demand_power_current_peak_sensor"
CONF_DEMAND_POWER_CONSUMED_SENSOR = "demand_power_consumed_sensor"
CONF_DEMAND_MONTH_MAX_SENSOR = "demand_month_max_sensor"
CONF_MINIMAL_PEAK_WARNING = "minimal_peak_warning"
CONF_PEAK_HIGH_SAFETY = "peak_high_safety"
CONF_PEAK_CRITICAL_SAFETY = "peak_critical_safety"

CONF_APPLIANCES = "appliances"
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
CONF_ENABLED = "enabled"
CONF_IS_BATTERY = "is_battery"

# Hysteresis
CONF_HYSTERESIS_ON_OFF_SECONDS = "hysteresis_on_off_seconds"
CONF_AVERAGING_WINDOW = "averaging_window_seconds"

# SoC
CONF_MIN_SOC_OVERRIDE = "min_soc_override"
CONF_IGNORE_BATTERY_PROTECTION = "ignore_battery_protection"
CONF_SOC_HYSTERESIS_PERCENT = "soc_hysteresis_percent"

# Modes
MODES = ["forced", "partial_solar", "full_solar", "off_peak"]
DEFAULT_MODE = "partial_solar"

# Defaults
DEFAULT_VOLTAGE = 230
DEFAULT_MIN_SOC = 20
DEFAULT_BATTERY_MODE = "strict"
BATTERY_PROTECTION_MODES = ["strict", "relaxed"]
DEFAULT_HYSTERESIS = 120
DEFAULT_MIN_AMPS = 6
DEFAULT_PRIORITY = 100
DEFAULT_MAX_AMPS = 16
DEFAULT_FIXED_AMPS_HYSTERESIS_SECONDS = 120
DEFAULT_AVERAGING = 180
DEFAULT_SOC_HYSTERESIS = 5
DEFAULT_IGNORE_PROTECTION = False
DEFAULT_AUTO_SWITCHING = True
DEFAULT_IS_BATTERY = False

# Quarterly demand defaults (kW). Thresholds entered as plain numbers in config.
DEFAULT_MINIMAL_PEAK_WARNING = 4.0
DEFAULT_PEAK_HIGH_SAFETY = 0.4
DEFAULT_PEAK_CRITICAL_SAFETY = 0.2

# Quarterly demand constants (match peakpower.yaml).
DEMAND_QUARTER_SECONDS = 900
DEMAND_MAX_POWER_CAP_KW = 40.0
DEMAND_HYSTERESIS_KW = 0.2
DEMAND_HIGH_GATE_MINUTES = 6
DEMAND_CRITICAL_GATE_MINUTES = 8
DEMAND_UPDATE_INTERVAL_SECONDS = 10