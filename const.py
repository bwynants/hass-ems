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

PLATFORMS = [
    Platform.SWITCH, Platform.SENSOR, Platform.SELECT, Platform.BINARY_SENSOR, Platform.NUMBER,
    Platform.EVENT,
]


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
CONF_PEAK_TARIFF_SENSOR = "peak_tariff_sensor"
CONF_VOLTAGE = "voltage"

# Electricity prices (€/kWh), used by the off_peak-mode breakeven decision.
CONF_COST_PEAK = "cost_peak"
CONF_COST_OFFPEAK = "cost_offpeak"
CONF_GAIN_PEAK = "gain_peak"
CONF_GAIN_OFFPEAK = "gain_offpeak"
# Legacy single export price (pre-0.5). Still read as the fallback for both
# gain_peak and gain_offpeak; the options flow retires it on the next save.
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

# When enabled, write the full per-cycle debug trace to ems_debug.log in the
# config dir. Off by default — opt-in, since it forces DEBUG logging + disk I/O.
CONF_DEBUG_LOG = "debug_log"
DEFAULT_DEBUG_LOG = False

# Appliance configuration
CONF_NAME = "name"
CONF_ENTITY_ID = "entity_id"
CONF_PRIORITY = "priority"
CONF_MIN_AMPS = "min_amps"
CONF_MAX_AMPS = "max_amps"
CONF_FIXED_AMPS = "fixed_amps"
CONF_FIXED_AMPS_HYSTERESIS_SECONDS = "fixed_amps_hysteresis_seconds"
CONF_POWER_SENSOR = "power_sensor"
CONF_PARTIAL_MIN_EXCESS_AMPS = "partial_min_excess_amps"
CONF_CONNECTED_SENSOR = "connected_sensor"
# EV charger: the car's state-of-charge sensor (%). Its presence is what makes
# an appliance an EV charger. Deliberately NOT the battery-era
# "battery_soc_sensor"/"min_soc" keys: old subentries still carry those in
# storage and must not be reinterpreted as this feature.
CONF_EV_SOC_SENSOR = "ev_soc_sensor"
# Per-appliance runtime setting (number entity), persisted in the subentry like
# CONF_RUNTIME_MODE: force charging while the car's SoC is below this (%).
CONF_FORCE_CHARGE_BELOW_SOC = "force_charge_below_soc"

# Hysteresis
CONF_HYSTERESIS_ON_OFF_SECONDS = "hysteresis_on_off_seconds"
CONF_AVERAGING_WINDOW = "averaging_window_seconds"
