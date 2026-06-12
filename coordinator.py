from __future__ import annotations

from datetime import datetime, timedelta
from collections import deque
import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from .const import (
    DOMAIN, CONF_APPLIANCES, CONF_VOLTAGE, CONF_PEAK_TARIFF_SENSOR,
    CONF_CAPACITY_TARIFF_SENSOR, CONF_MIN_SOC,CONF_BATTERY_SOC_SENSOR,
    CONF_MIN_SOC_OVERRIDE, CONF_IGNORE_BATTERY_PROTECTION,
    CONF_SOC_HYSTERESIS_PERCENT, CONF_CONNECTED_SENSOR,
    CONF_SOLAR_PRODUCTION,
    CONF_NAME, CONF_PRIORITY, CONF_MAX_AMPS, CONF_MIN_AMPS,
    CONF_PARTIAL_MIN_EXCESS, CONF_HYSTERESIS_SECONDS, CONF_MIN_SWITCH_INTERVAL,
    CONF_AVERAGING_WINDOW,
    DEFAULT_VOLTAGE, DEFAULT_MIN_SOC,
    DEFAULT_HYSTERESIS, DEFAULT_MIN_SWITCH, DEFAULT_AVERAGING,
    DEFAULT_SOC_HYSTERESIS, DEFAULT_IGNORE_PROTECTION,
    DEFAULT_MODE, CONF_RUNTIME_MODES,
)

_LOGGER = logging.getLogger(__name__)

_OFF_STATES = {"off", "false", "False", "0"}
_UNAVAILABLE_STATES = {STATE_UNAVAILABLE, STATE_UNKNOWN, "none", ""}

# Multipliers to normalise power values to watts.
_POWER_UNIT_MULTIPLIERS: dict[str, float] = {
    "w": 1.0,
    "kw": 1000.0,
    "mw": 1_000_000.0,
}

DEVICE_CLASS_POWER = "power"
DEVICE_CLASS = "device_class"
UNIT_OF_MEASUREMENT = "unit_of_measurement"

def _normalise_power(value: float, unit: str | None) -> float:
    """Convert a power reading to watts based on its unit_of_measurement."""
    if unit is None:
        return value
    return value * _POWER_UNIT_MULTIPLIERS.get(unit.lower().strip(), 1.0)


class SolarApplianceCoordinator(DataUpdateCoordinator):
    """Coordinator

    Periodically reads sensor data, runs the optimizer and planner,
    and applies control decisions.
    """
    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry, update_interval=timedelta(seconds=30))
        self.entry = entry
        self.appliance_states = {}
        self.runtime_modes = entry.options.get(CONF_RUNTIME_MODES, {})
        self._last_excess = 0.0
        self._last_soc = None

    @property
    def enabled(self) -> bool:
        """Return whether the controller is enabled."""
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        """Set whether the controller is enabled."""
        self._enabled = value

    def _read_sensor(
        self, entity_id: str | None, default: float = 0.0,
    ) -> float:
        """Read a numeric sensor value, returning default if unavailable.

        power value is normalised to watts using the
        sensor's ``unit_of_measurement`` attribute (kW → W, MW → W).
        """
        if not entity_id:
            return default
        state = self.hass.states.get(entity_id)
        _LOGGER.debug("Reading sensor %s: %s", entity_id, state)
        if state is None or state.state in _UNAVAILABLE_STATES:
            return default
        try:
            val = float(state.state)
        except (ValueError, TypeError):
            return default
        if state.attributes.get(DEVICE_CLASS) == DEVICE_CLASS_POWER:
            val = _normalise_power(val, state.attributes.get(UNIT_OF_MEASUREMENT))
        return val

    def _read_sensor_optional(
        self, entity_id: str | None,
    ) -> float | None:
        """Read a numeric sensor value, returning None if unavailable.

        power value is normalised to watts using the
        sensor's ``unit_of_measurement`` attribute (kW → W, MW → W).
        """
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in _UNAVAILABLE_STATES:
            return None
        try:
            val = float(state.state)
        except (ValueError, TypeError):
            return None

        if state.attributes.get(DEVICE_CLASS) == DEVICE_CLASS_POWER :
            val = _normalise_power(val, state.attributes.get(UNIT_OF_MEASUREMENT))
        return val

    def _read_binary(self, entity_id: str | None) -> bool | None:
        """Read a binary sensor, returns True/False/None."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return None
        return state.state == "on"

    async def _async_update_data(self):
        data = {**self.entry.data, **self.entry.options}
        appliances = data.get(CONF_APPLIANCES, [])
        _LOGGER.info("Updating solar appliance data" )
        if self._is_capacity_tariff_active(data):
            self._force_all_off()
            return {"excess_amps": 0, "soc": self._last_soc, "active_appliances": [], "appliances": appliances}
        
        _LOGGER.info("data %s", data)
        for app in appliances:
            _LOGGER.info("app %s", app )

        excess_amps = self._calculate_excess_amps(data)
        self._last_excess = excess_amps
        self._last_soc = self._get_current_soc(data)

        _LOGGER.info("Updating solar appliance data %f", excess_amps )

        self._update_histories(appliances, excess_amps)

        active = self._optimize_appliances(appliances, excess_amps, data)

        return {
            "excess_amps": round(excess_amps, 2),
            "soc": self._last_soc,
            "active_appliances": active,
            "appliances": appliances,
        }

    def _is_capacity_tariff_active(self, config):
        entity_id = config.get(CONF_CAPACITY_TARIFF_SENSOR)
        if not entity_id:
            return False
        state = self.hass.states.get(entity_id)
        return state and state.state == "on"

    def _force_all_off(self):
        now = datetime.now()
        for state in self.appliance_states.values():
            state["last_on"] = None
            state["last_change"] = now

    def _calculate_excess_amps(self, config):
        solar_w = self._read_sensor(config.get(CONF_SOLAR_PRODUCTION))
        voltage = self._read_sensor(config.get(CONF_VOLTAGE), default=DEFAULT_VOLTAGE)
        if (voltage <= 0):
            return 0
        return solar_w / voltage

    def _get_current_soc(self, config):
        entity = config.get(CONF_BATTERY_SOC_SENSOR)
        if not entity:
            return None
        state = self.hass.states.get(entity)
        try:
            return float(state.state) if state and state.state not in ("unknown", "unavailable") else None
        except (ValueError, TypeError):
            return None

    def _update_histories(self, appliances, excess_amps):
        now = datetime.now()
        for app in appliances:
            name = app[CONF_NAME]
            if name not in self.appliance_states:
                self.appliance_states[name] = {
                    "last_on": None,
                    "last_change": now,
                    "excess_history": deque(maxlen=120),
                    "soc_blocked": False,
                }
            self.appliance_states[name]["excess_history"].append(excess_amps)

    def _get_averaged_excess(self, app):
        name = app[CONF_NAME]
        state = self.appliance_states.get(name)
        if not state or not state["excess_history"]:
            return self._last_excess
        window = app.get(CONF_AVERAGING_WINDOW, DEFAULT_AVERAGING)
        samples = max(1, int(window / 30))
        recent = list(state["excess_history"])[-samples:]
        return sum(recent) / len(recent) if recent else self._last_excess

    def _is_connected(self, app, config):
        entity_id = app.get(CONF_CONNECTED_SENSOR)
        if not entity_id:
            return True
        state = self.hass.states.get(entity_id)
        return state and state.state == "on"

    def _optimize_appliances(self, appliances, excess_amps, config):
        sorted_apps = sorted(appliances, key=lambda a: a.get(CONF_PRIORITY, 999))
        active = []
        remaining = excess_amps
        now = datetime.now()

        for app in sorted_apps:
            if self._should_run_with_hysteresis(app, remaining, config, now):
                active.append(app[CONF_NAME])
                used = app.get(CONF_MIN_AMPS) or app.get(CONF_MAX_AMPS, 0)
                remaining -= used
                name = app.get(CONF_NAME)
                if name in self.appliance_states:
                    self.appliance_states[name]["last_on"] = now
        return active

    def _should_run_with_hysteresis(self, app, remaining_amps, config, now):
        if not self._is_connected(app, config):
            return False

        name = app[CONF_NAME]
        state = self.appliance_states.setdefault(name, {
            "last_on": None,
            "last_change": now,
            "excess_history": deque(maxlen=120),
            "soc_blocked": False,
        })

        avg_excess = self._get_averaged_excess(app)

        # SoC Protection
        if not app.get(CONF_IGNORE_BATTERY_PROTECTION, DEFAULT_IGNORE_PROTECTION) and self._last_soc is not None:
            min_soc = app.get(CONF_MIN_SOC_OVERRIDE) or config.get(CONF_MIN_SOC, DEFAULT_MIN_SOC)
            soc_hyst = app.get(CONF_SOC_HYSTERESIS_PERCENT, DEFAULT_SOC_HYSTERESIS)
            if state["soc_blocked"]:
                if self._last_soc >= min_soc + soc_hyst:
                    state["soc_blocked"] = False
            elif self._last_soc < min_soc:
                state["soc_blocked"] = True
            if state["soc_blocked"]:
                return False

        condition_met = self._basic_condition_met(app, avg_excess, remaining_amps, config)

        hyst_sec = app.get(CONF_HYSTERESIS_SECONDS, DEFAULT_HYSTERESIS)
        min_int = app.get(CONF_MIN_SWITCH_INTERVAL, DEFAULT_MIN_SWITCH)

        if (now - state["last_change"]).total_seconds() < min_int:
            return bool(state["last_on"])

        if condition_met:
            if state["last_on"] is None or (now - state["last_on"]).total_seconds() >= hyst_sec:
                state["last_change"] = now
                return True
            return bool(state["last_on"])
        else:
            if state["last_on"]:
                state["last_change"] = now
            return False

    def _basic_condition_met(self, app, avg_excess, remaining, config):
        appliance_name = app[CONF_NAME]
        mode = self.runtime_modes.get(appliance_name, DEFAULT_MODE)
        min_a = app.get(CONF_MIN_AMPS, 6)
        is_off_peak = self._get_off_peak_status(config)

        if is_off_peak:
            if mode == "off_peak":
                return True
            return remaining >= min_a

        # Peak hours
        if mode == "forced":
            return remaining >= min_a
        elif mode == "partial_solar":
            return avg_excess >= app.get(CONF_PARTIAL_MIN_EXCESS, 1000) / config.get(CONF_VOLTAGE, DEFAULT_VOLTAGE)
        elif mode == "off_peak":
            breakeven = self.calculate_breakeven(
                min_a,
                config.get("cost_peak", 0.40),
                config.get("cost_offpeak", 0.20),
                config.get("gain_export", 0.10)
            )
            return avg_excess >= breakeven

        return False

    def _get_peak_tarriff_status(self, config):
        entity_id = config.get(CONF_PEAK_TARIFF_SENSOR)
        if not entity_id:
            return True
        state = self.hass.states.get(entity_id)
        return state and state.state == "on"

    @staticmethod
    def calculate_breakeven(min_amps, cost_peak, cost_offpeak, gain_export):
        denom = cost_peak - gain_export
        if abs(denom) > 0.001:
            return min_amps * (cost_peak - cost_offpeak) / denom
        return 0