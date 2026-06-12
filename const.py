"""Constants for Solar Appliance Manager."""

from homeassistant.const import Platform

DOMAIN = "solar_appliance_manager"
MANUFACTURER = "Solar Appliance Manager"

PLATFORMS = [Platform.SWITCH, Platform.SENSOR, Platform.SELECT]

# Global configuration
CONF_SOLAR_PRODUCTION = "solar_production_sensor"
CONF_HOUSE_LOAD = "house_load_sensor"
CONF_GRID_EXPORT = "grid_export_sensor"
CONF_BATTERY_SOC_SENSOR = "battery_soc_sensor"
CONF_PEAK_TARIFF_SENSOR = "peak_tariff_sensor"
CONF_CAPACITY_TARIFF_SENSOR = "capacity_tariff_sensor"
CONF_VOLTAGE = "voltage"
CONF_MIN_SOC = "min_soc"
CONF_BATTERY_PROTECTION_MODE = "battery_protection_mode"

CONF_APPLIANCES = "appliances"
CONF_RUNTIME_MODES = "runtime_modes"

# Appliance configuration
CONF_NAME = "name"
CONF_ENTITY_ID = "entity_id"
CONF_PRIORITY = "priority"
CONF_MIN_AMPS = "min_amps"
CONF_MAX_AMPS = "max_amps"
CONF_PARTIAL_MIN_EXCESS = "partial_min_excess"
CONF_CONNECTED_SENSOR = "connected_sensor"

# Hysteresis
CONF_HYSTERESIS_SECONDS = "hysteresis_seconds"
CONF_MIN_SWITCH_INTERVAL = "min_switch_interval"
CONF_AVERAGING_WINDOW = "averaging_window_seconds"

# SoC
CONF_MIN_SOC_OVERRIDE = "min_soc_override"
CONF_IGNORE_BATTERY_PROTECTION = "ignore_battery_protection"
CONF_SOC_HYSTERESIS_PERCENT = "soc_hysteresis_percent"

# Modes
MODES = ["forced", "partial_solar", "off_peak"]
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
DEFAULT_MIN_SWITCH = 300
DEFAULT_AVERAGING = 180
DEFAULT_SOC_HYSTERESIS = 5
DEFAULT_IGNORE_PROTECTION = False