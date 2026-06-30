from __future__ import annotations

from datetime import timedelta
from collections import deque
import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, STATE_ON
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.core import split_entity_id, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util
from .const import (
    DOMAIN, CONF_VOLTAGE, CONF_PEAK_TARIFF_SENSOR,
    CONF_MIN_SOC,CONF_BATTERY_SOC_SENSOR,
    CONF_BATTERY_PROTECTION_MODE, DEFAULT_BATTERY_MODE,
    CONF_MIN_SOC_OVERRIDE, CONF_IGNORE_BATTERY_PROTECTION,
    CONF_SOC_HYSTERESIS_PERCENT, CONF_CONNECTED_SENSOR,
    CONF_ENABLED, CONF_IS_BATTERY,
    CONF_SOLAR_PRODUCTION, CONF_HOUSE_LOAD, CONF_GRID_EXPORT, CONF_ENTITY_ID,
    CONF_NAME, CONF_PRIORITY, CONF_MAX_AMPS, CONF_MIN_AMPS, CONF_FIXED_AMPS,
    CONF_FIXED_AMPS_HYSTERESIS_SECONDS, DEFAULT_FIXED_AMPS_HYSTERESIS_SECONDS,
    CONF_POWER_SENSOR,
    CONF_PARTIAL_MIN_EXCESS, CONF_HYSTERESIS_ON_OFF_SECONDS,
    CONF_AVERAGING_WINDOW,
    DEFAULT_VOLTAGE, DEFAULT_MIN_SOC,
    DEFAULT_HYSTERESIS, DEFAULT_AVERAGING,
    DEFAULT_SOC_HYSTERESIS, DEFAULT_IGNORE_PROTECTION,
    DEFAULT_MODE, CONF_RUNTIME_MODE, CONF_RUNTIME_MODES, CONF_AUTO_SWITCHING,
    DEFAULT_AUTO_SWITCHING, get_appliances,
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

CONTROLLER_UPDATE_INTERVAL = 10
# The fixed_amps setpoint is quantised to whole amps, rounded DOWN with this
# margin: run at N amps only once N + margin is available (e.g. 6.8 -> 6, and it
# steps up to 7 only at 7.5). The margin also acts as a deadband against flicker.
FIXED_AMPS_DEADBAND = 0.5
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
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry, update_interval=timedelta(seconds=CONTROLLER_UPDATE_INTERVAL))
        self.entry = entry
        self.appliance_states = {}
        self._enabled = entry.options.get(CONF_AUTO_SWITCHING, DEFAULT_AUTO_SWITCHING)
        self._last_excess = 0.0
        self._last_soc = None
        self._battery_min_soc = None
        self._battery_protection_mode = None
        # Set by __init__.py after construction; the fast quarter-hour demand
        # coordinator, read here for peak-aware control.
        self.demand_coordinator = None
        # When True, the next config-entry update listener call skips the full
        # reload (used for live per-appliance / auto-switching setting changes).
        self.suppress_reload = False

    @property
    def enabled(self) -> bool:
        """Return whether the controller is enabled."""
        return self._enabled

    async def async_set_auto_switching(self, value: bool) -> None:
        """Set whether automatic switching is enabled and persist the option.

        Use this method for user-driven changes so the coordinator's enabled
        flag is only changed via an explicit API (prevents incidental toggles).
        """
        self._enabled = value
        options = dict(self.entry.options)
        options[CONF_AUTO_SWITCHING] = value
        # Applied live below; don't let the update listener reload the entry.
        self.suppress_reload = True
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        await self.async_refresh()
        _LOGGER.info("Auto switching set to %s", value)

    def setup_trigger_listeners(self) -> None:
        """Refresh immediately when an off-peak / tariff input changes state.

        Without this, a tariff transition would only take effect on the next
        scheduled poll. The listener unsubscribe is tied to the config entry
        so it is cleaned up on unload/reload.
        """
        config = {**self.entry.data, **self.entry.options}
        entities = [
            e
            for e in (
                config.get(CONF_PEAK_TARIFF_SENSOR),
            )
            if e
        ]
        if not entities:
            return

        @callback
        def _on_trigger_change(event) -> None:
            _LOGGER.debug(
                "Trigger entity %s changed (%s -> %s); requesting refresh",
                event.data.get("entity_id"),
                getattr(event.data.get("old_state"), "state", None),
                getattr(event.data.get("new_state"), "state", None),
            )
            self.hass.async_create_task(self.async_request_refresh())

        self.entry.async_on_unload(
            async_track_state_change_event(self.hass, entities, _on_trigger_change)
        )
        _LOGGER.debug("Watching tariff trigger entities for changes: %s", entities)

    def _get_subentry(self, appliance_id: str | None):
        if not appliance_id:
            return None
        return getattr(self.entry, "subentries", {}).get(appliance_id)

    def get_appliance_persistent_setting(
        self,
        appliance_id: str | None,
        key: str,
        fallback_key: str | None = None,
        default=None,
    ):
        """Return a persistent appliance setting from subentry or fallback options."""
        if not appliance_id:
            return default
        subentry = self._get_subentry(appliance_id)
        if subentry and key in subentry.data:
            return subentry.data.get(key, default)
        if fallback_key:
            return self.entry.options.get(fallback_key, {}).get(appliance_id, default)
        return default

    async def async_set_appliance_persistent_setting(
        self,
        appliance_id: str | None,
        key: str,
        value,
        title: str | None = None,
    ) -> bool:
        """Persist a per-appliance setting to the config subentry."""
        if not appliance_id:
            return False
        subentry = self._get_subentry(appliance_id)
        if not subentry or not hasattr(self.hass.config_entries, "async_update_subentry"):
            return False
        try:
            new_data = dict(subentry.data)
            new_data[key] = value
            title = title or getattr(subentry, "title", None) or self.entry.data.get(CONF_NAME)
            # Applied live via async_refresh below; a per-appliance setting change
            # (mode/enabled) doesn't need a full reload, which would reset state
            # and briefly disturb control. Tell the update listener to skip it.
            self.suppress_reload = True
            self.hass.config_entries.async_update_subentry(
                self.entry,
                subentry,
                data=new_data,
                title=title,
            )
            await self.async_refresh()
            return True
        except Exception as err:
            self.suppress_reload = False
            _LOGGER.exception("Failed to persist %s for appliance %s: %s", key, appliance_id, err)
            return False

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
        appliances = get_appliances(self.entry)
        _LOGGER.debug(
            "==== EMS cycle start: %d appliance(s), auto_switching=%s ====",
            len(appliances), self.enabled,
        )

        # SoC and the protection threshold are derived from the appliances that
        # are flagged as batteries (each carries its own SoC sensor and min_soc).
        self._last_soc = self._get_current_soc(appliances)
        self._battery_min_soc = self._get_battery_min_soc(appliances)
        self._battery_protection_mode = self._get_battery_protection_mode(appliances)
        if self._last_soc is not None or self._battery_min_soc is not None:
            _LOGGER.debug(
                "battery: soc=%s, min_soc=%s, protection=%s",
                self._last_soc, self._battery_min_soc, self._battery_protection_mode,
            )

        # Amps the managed appliances are currently drawing (exposed as a sensor
        # and added back to the grid-export budget further down).
        managed_amps = self._get_managed_consumption_amps(appliances, data)
        _LOGGER.debug("managed appliances currently drawing %.2f A", managed_amps)

        # Quarter-hour demand snapshot (drives peak-aware control), if configured.
        demand = self._demand_data()
        if demand:
            _LOGGER.debug(
                "demand: estimate=%s kW high_limit=%s crit_limit=%s high=%s critical=%s max_to_peak=%s kW",
                demand.get("estimate"), demand.get("high_limit"), demand.get("critical_limit"),
                demand.get("high"), demand.get("critical"), demand.get("max_power_to_peak"),
            )

        # Quarter-hour demand is critical: force everything off to protect the
        # monthly peak (emergency backstop if graceful throttling can't keep up).
        if self._is_demand_critical(data):
            if self.enabled:
                _LOGGER.warning(
                    "demand CRITICAL -> throttling managed appliances to min, then shedding"
                )
                kept, kept_amps = self._critical_shed(appliances)
                await self._apply_appliance_states(appliances, kept)
            else:
                kept, kept_amps = [], 0.0
                _LOGGER.warning(
                    "demand CRITICAL (auto-switching off; not intervening)"
                )
            _LOGGER.debug(
                "==== EMS cycle end: critical (kept at min: %s) ====", kept or "none"
            )
            return {
                "excess_amps": 0,
                "soc": self._last_soc,
                "active_appliances": kept,
                "appliances": appliances,
                "auto_switching": self.enabled,
                "demand_critical": True,
                "managed_amps": round(kept_amps, 2),
            }

        # Total surplus available for managed loads = grid surplus + the draw
        # already taken by managed appliances (added back to work from gross).
        grid_surplus = self._calculate_excess_amps(data)
        excess_amps = grid_surplus + managed_amps
        self._last_excess = excess_amps
        _LOGGER.debug(
            "budget: excess=%.2f A (grid surplus %.2f A + managed add-back %.2f A)",
            excess_amps, grid_surplus, managed_amps,
        )

        self._update_histories(appliances, excess_amps)

        if not self.enabled:
            _LOGGER.debug(
                "auto-switching OFF -> optimizer skipped; spare=%.2f A", excess_amps
            )
            _LOGGER.debug("==== EMS cycle end: auto-switching off ====")
            return {
                "excess_amps": round(excess_amps, 2),
                "soc": self._last_soc,
                "active_appliances": [],
                "appliances": appliances,
                "auto_switching": False,
                "managed_amps": round(managed_amps, 2),
            }

        peak_amps = self._get_peak_amps(data)
        _LOGGER.debug(
            "peak headroom: %s",
            "off (no demand feature)" if peak_amps is None else f"{peak_amps:.2f} A",
        )
        active, decided_amps = self._optimize_appliances(appliances, excess_amps, data, peak_amps)
        await self._apply_appliance_states(appliances, active)

        _LOGGER.debug(
            "result: active=%s | decided draw=%.2f A | spare(excess)=%.2f A",
            active, decided_amps, excess_amps,
        )
        _LOGGER.debug("==== EMS cycle end ====")
        return {
            "excess_amps": round(excess_amps, 2),
            "soc": self._last_soc,
            "active_appliances": active,
            "appliances": appliances,
            "auto_switching": True,
            # Report the draw the optimizer just decided (fresh setpoints), so
            # Managed Consumption updates this cycle instead of lagging one.
            "managed_amps": round(decided_amps, 2),
        }

    def _demand_data(self):
        """Return the demand coordinator's data when the feature is active."""
        dc = self.demand_coordinator
        if dc is None or not getattr(dc, "feature_active", False):
            return None
        return dc.data

    def _is_demand_critical(self, config) -> bool:
        """True when the quarter-hour demand is critical.

        Peak-aware control is always on; it simply has no effect until the
        quarter-hour demand sensors are configured (``_demand_data`` is None).
        """
        demand = self._demand_data()
        return bool(demand and demand.get("critical"))

    def _get_peak_amps(self, config):
        """Peak headroom in amps (from Max Power To Peak Limit), or None.

        None disables the peak cap in the optimizer (demand feature not
        configured, or no value yet). Peak-aware control is always on.
        """
        demand = self._demand_data()
        if not demand:
            return None
        kw = demand.get("max_power_to_peak")
        if kw is None:
            return None
        voltage = self._read_sensor(config.get(CONF_VOLTAGE), default=DEFAULT_VOLTAGE)
        if voltage <= 0:
            return None
        return kw * 1000.0 / voltage

    def _critical_shed(self, appliances):
        """Shed load on critical demand, gracefully.

        Each running appliance that is above its minimum (and has an adjustable
        ``fixed_amps`` setpoint) is first throttled to ``min_amps`` and kept on;
        appliances already at the minimum, or with no adjustable setpoint, are
        switched off. So on the first critical cycle adjustable loads drop to min,
        and only if demand is still critical next cycle are they cut completely.

        Returns ``(kept_names, kept_amps)`` for the appliances kept on at min.
        """
        now = dt_util.utcnow()
        kept: list[str] = []
        kept_amps = 0.0
        for app in appliances:
            name = app.get(CONF_NAME)
            state = self.appliance_states.setdefault(name, {
                "is_on": False,
                "last_change": None,
                "last_fixed_amps": None,
                "last_fixed_amps_change": None,
                "excess_history": deque(maxlen=120),
                "soc_blocked": False,
            })
            if not app.get(CONF_ENABLED, True) or not self._is_entity_on(app):
                continue
            min_a = app.get(CONF_MIN_AMPS, 6)
            fixed_entity = app.get(CONF_FIXED_AMPS)
            current = self._read_sensor_optional(fixed_entity) if fixed_entity else None
            if current is not None and current > min_a:
                # Step down to min and keep it on this cycle.
                state["is_on"] = True
                state["last_change"] = now
                state["last_fixed_amps"] = float(min_a)
                state["last_fixed_amps_change"] = now
                self.hass.async_create_task(
                    self._update_fixed_amps_entity(app, float(min_a))
                )
                kept.append(name)
                kept_amps += min_a
                _LOGGER.warning(
                    "demand CRITICAL: %s throttled %.1f A -> min %d A (will shed if still critical)",
                    name, current, min_a,
                )
            else:
                # Already at min, or no adjustable setpoint: switch off.
                state["is_on"] = False
                state["last_change"] = now
                state["last_fixed_amps"] = None
                state["last_fixed_amps_change"] = None
                _LOGGER.warning(
                    "demand CRITICAL: %s shed (off)%s",
                    name, "" if fixed_entity else " (no adjustable setpoint)",
                )
        return kept, kept_amps

    def _calculate_excess_amps(self, config):
        voltage = self._read_sensor(config.get(CONF_VOLTAGE), default=DEFAULT_VOLTAGE)
        if (voltage <= 0):
            return 0

        # Prefer the measured net grid power (import positive, export negative).
        # Return it SIGNED: export → positive surplus, import → negative. It must
        # stay signed so the managed-consumption add-back (excess += managed_amps)
        # reconstructs the true gross surplus (solar - house). Clamping a net
        # import to 0 would let a managed load's own draw be re-counted as surplus
        # and sustain itself even with no solar. Fall back to the
        # solar - house_load estimate when no grid-power sensor is set.
        grid_w = self._read_sensor_optional(config.get(CONF_GRID_EXPORT))
        if grid_w is not None:
            return -grid_w / voltage

        solar_w = self._read_sensor(config.get(CONF_SOLAR_PRODUCTION))
        house_load_w = self._read_sensor(config.get(CONF_HOUSE_LOAD))
        return (solar_w - house_load_w) / voltage

    def _is_entity_on(self, app) -> bool:
        """Return True if the appliance's controlled entity is currently on."""
        entity_id = app.get(CONF_ENTITY_ID)
        if not entity_id:
            return False
        state = self.hass.states.get(entity_id)
        return state is not None and state.state == STATE_ON

    def _get_managed_consumption_amps(self, appliances, config) -> float:
        """Sum the amps managed appliances are currently drawing.

        This is added back to the net grid-export budget so allocation is based
        on the gross surplus. Only appliances whose controlled entity is on are
        counted; each contributes its effective draw (measured power sensor,
        else fixed_amps value, else min_amps) via ``_get_actual_amps``.
        """
        total = 0.0
        for app in appliances:
            # Disabled appliances are forced off this cycle; don't count their
            # draw, as the capacity they free isn't ours to allocate yet.
            if not app.get(CONF_ENABLED, True):
                continue
            if self._is_entity_on(app):
                total += self._get_actual_amps(app, config)
        return total

    def _get_current_soc(self, appliances):
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
            val = self._read_sensor_optional(app.get(CONF_BATTERY_SOC_SENSOR))
            if val is not None:
                socs.append(val)
        return min(socs) if socs else None

    def _get_battery_min_soc(self, appliances):
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

    def _get_battery_protection_mode(self, appliances):
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
        return "strict" if any(m == "strict" for m in modes) else "relaxed"

    def _update_histories(self, appliances, excess_amps):
        for app in appliances:
            name = app[CONF_NAME]
            if name not in self.appliance_states:
                self.appliance_states[name] = {
                    "is_on": False,
                    "last_change": None,
                    "last_fixed_amps": None,
                    "last_fixed_amps_change": None,
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
        samples = max(1, int(window / CONTROLLER_UPDATE_INTERVAL))
        recent = list(state["excess_history"])[-samples:]
        return sum(recent) / len(recent) if recent else self._last_excess

    def _is_connected(self, app, config):
        entity_id = app.get(CONF_CONNECTED_SENSOR)
        if not entity_id:
            return True
        state = self.hass.states.get(entity_id)
        return state and state.state == "on"

    def _optimize_appliances(self, appliances, excess_amps, config, peak_amps=None):
        sorted_apps = sorted(appliances, key=lambda a: a.get(CONF_PRIORITY, 999))
        active = []
        remaining = excess_amps
        # Total draw the optimizer decides this cycle (the freshly commanded
        # setpoints / measured draws), so Managed Consumption reflects the new
        # values immediately instead of lagging a cycle.
        decided_amps = 0.0
        # Second, independent budget: the quarter-hour peak headroom (amps).
        # None means peak control is inactive. Higher-priority appliances claim
        # it first, so lower-priority ones are shed when the headroom runs out.
        peak_remaining = peak_amps
        now = dt_util.utcnow()

        _LOGGER.debug(
            "optimize: %d appliance(s) in priority order | excess=%.2f A | peak headroom=%s",
            len(sorted_apps), remaining,
            "off" if peak_remaining is None else f"{peak_remaining:.2f} A",
        )

        for app in sorted_apps:
            name = app.get(CONF_NAME)
            if not app.get(CONF_ENABLED, True):
                _LOGGER.debug("  - %s: disabled, skipped", name)
                continue
            min_a = app.get(CONF_MIN_AMPS, 6)
            mode = self._appliance_mode(app)
            # In `forced` mode, solar surplus and the grid peak-headroom are
            # ADDITIVE — solar is used on top of grid power (the grid part still
            # bounded by the peak/High limit). Other modes use solar only, capped
            # by the headroom (the prediction throttle).
            forced_additive = mode == "forced" and peak_remaining is not None
            solar_avail = max(remaining, 0.0)
            # Total amps a forced appliance may draw: peak headroom PLUS the
            # signed solar surplus. When solar > house, excess is positive and
            # adds on top of grid; when the house is importing, excess is
            # negative and is subtracted, so the non-managed load eating the
            # headroom throttles the appliance down (net grid stays under High).
            forced_budget = remaining + peak_remaining if forced_additive else None

            # Hard peak cap: if the appliance can't reach its minimum from the
            # budget it may use, it can't run this cycle (excluding it turns it
            # off downstream). Forced may draw solar + grid; others, grid only.
            if peak_remaining is not None:
                reachable = forced_budget if forced_additive else peak_remaining
                if reachable < min_a:
                    _LOGGER.debug(
                        "  - %s (mode=%s): OFF, peak headroom too low (%.1f A available < %d A min)",
                        name, mode, reachable, min_a,
                    )
                    continue
            if self._should_run_with_hysteresis(app, remaining, config, now):
                active.append(name)

                # Calculate scaled amperage. Forced scales against solar + grid
                # headroom; other modes scale against solar then are throttled to
                # the headroom (prediction against the High limit) so demand never
                # reaches the peak — never below the appliance minimum.
                if forced_additive:
                    scaled_amps = self._calculate_scaled_amps(app, forced_budget)
                else:
                    scaled_amps = self._calculate_scaled_amps(app, remaining)
                    if peak_remaining is not None:
                        scaled_amps = max(min(scaled_amps, peak_remaining), min_a)

                # Command the setpoint if a fixed_amps entity is configured
                # (rate-limited so it doesn't switch values too often).
                commanded = None
                if app.get(CONF_FIXED_AMPS):
                    commanded = self._apply_fixed_amps(app, name, scaled_amps, now)

                # Deduct the appliance's draw from the budget. Prefer the
                # measured power sensor (real consumption); otherwise use the
                # freshly commanded fixed_amps setpoint, then min_amps.
                measured = self._get_measured_amps(app, config)
                if measured is not None:
                    used = measured
                elif commanded is not None:
                    used = commanded
                else:
                    used = min_a
                _LOGGER.debug(
                    "  - %s (mode=%s): ON, scaled=%.1f A, draw=%.2f A (%s) | budget before: excess=%.2f A peak=%s",
                    name, mode, scaled_amps, used,
                    "measured" if measured is not None else ("setpoint" if commanded is not None else "min_amps"),
                    remaining,
                    "off" if peak_remaining is None else f"{peak_remaining:.2f} A",
                )
                decided_amps += used
                if forced_additive:
                    # Solar-covered part comes off the solar budget; the rest is
                    # grid import and comes off the peak headroom.
                    solar_used = min(used, solar_avail)
                    remaining -= solar_used
                    peak_remaining -= used - solar_used
                else:
                    remaining -= used
                    if peak_remaining is not None:
                        peak_remaining -= used
            else:
                _LOGGER.debug("  - %s (mode=%s): OFF, run condition not met", name, mode)
        _LOGGER.debug(
            "optimize done: active=%s | leftover excess=%.2f A | leftover peak=%s",
            active, remaining,
            "off" if peak_remaining is None else f"{peak_remaining:.2f} A",
        )
        return active, decided_amps

    def _should_run_with_hysteresis(self, app, remaining_amps, config, now):
        if not self._is_connected(app, config):
            _LOGGER.debug("%s not connected, skipping", app.get(CONF_NAME))
            return False

        name = app[CONF_NAME]
        state = self.appliance_states.setdefault(name, {
            "is_on": False,
            "last_change": None,
            "last_fixed_amps": None,
            "last_fixed_amps_change": None,
            "excess_history": deque(maxlen=120),
            "soc_blocked": False,
        })

        avg_excess = self._get_averaged_excess(app)
        _LOGGER.debug(
            "evaluating %s: avg_excess=%s, remaining=%s, is_on=%s, last_change=%s",
            name,
            avg_excess,
            remaining_amps,
            state["is_on"],
            state["last_change"],
        )

        # SoC Protection. In ``relaxed`` mode a low battery doesn't hard-off the
        # appliance; instead it may only run while solar surplus covers it.
        soc_block_solar_only = False
        if app.get(CONF_IS_BATTERY, False):
            # The battery is the thing being protected; never block it on its
            # own SoC or it could never charge back up. Clear any stale latch.
            if state["soc_blocked"]:
                state["soc_blocked"] = False
                _LOGGER.debug("%s soc_blocked cleared (appliance is a battery)", name)
        elif app.get(CONF_IGNORE_BATTERY_PROTECTION, DEFAULT_IGNORE_PROTECTION):
            # Protection disabled: clear any latched block so it can be turned off.
            if state["soc_blocked"]:
                state["soc_blocked"] = False
                _LOGGER.debug("%s soc_blocked cleared (battery protection ignored)", name)
        elif self._last_soc is not None and self._battery_min_soc is not None:
            # Use the per-appliance override only when explicitly set (0 is valid),
            # otherwise the threshold configured on the battery appliance(s).
            override = app.get(CONF_MIN_SOC_OVERRIDE)
            min_soc = override if override is not None else self._battery_min_soc
            soc_hyst = app.get(CONF_SOC_HYSTERESIS_PERCENT, DEFAULT_SOC_HYSTERESIS)
            # Clamp the clear threshold so it is always reachable (SoC maxes at 100%).
            clear_at = min(min_soc + soc_hyst, 100)
            if state["soc_blocked"]:
                if self._last_soc >= clear_at:
                    state["soc_blocked"] = False
                    _LOGGER.debug("%s soc_blocked cleared, soc=%s >= %s", name, self._last_soc, clear_at)
            elif self._last_soc < min_soc:
                state["soc_blocked"] = True
                _LOGGER.debug("%s soc_blocked set, soc=%s < %s", name, self._last_soc, min_soc)
            if state["soc_blocked"]:
                if self._battery_protection_mode == "relaxed":
                    # Don't kill it; let it wait for solar (gated below).
                    soc_block_solar_only = True
                    _LOGGER.debug(
                        "%s soc-blocked (relaxed): solar-only until SoC recovers", name
                    )
                else:
                    _LOGGER.debug("%s blocked by SOC protection (strict)", name)
                    return False

        if soc_block_solar_only:
            # Battery is low: ignore mode fallbacks (forced/off-peak/partial) and
            # only run when the remaining solar budget covers the appliance.
            required_a = app.get(CONF_MIN_AMPS, 6)
            condition_met = remaining_amps >= required_a
        else:
            condition_met = self._basic_condition_met(app, avg_excess, remaining_amps, config)
        _LOGGER.debug("%s condition_met=%s", name, condition_met)

        # Already in the desired state: nothing to do, leave the timer untouched.
        if condition_met == state["is_on"]:
            return state["is_on"]

        # A transition is wanted. Hysteresis only damps changes away from an
        # already-settled state; the first decision (last_change is None) and a
        # change after the window has elapsed both transition immediately.
        hyst_sec = app.get(CONF_HYSTERESIS_ON_OFF_SECONDS, DEFAULT_HYSTERESIS)
        last_change = state["last_change"]
        if last_change is not None:
            time_since_change = (now - last_change).total_seconds()
            if time_since_change < hyst_sec:
                _LOGGER.debug(
                    "%s within hysteresis (%ds); holding state=%s",
                    name, int(time_since_change), state["is_on"],
                )
                return state["is_on"]

        state["is_on"] = condition_met
        state["last_change"] = now
        _LOGGER.debug("%s transitioning to %s", name, "on" if condition_met else "off")
        return condition_met

    def _appliance_mode(self, app) -> str:
        """Return the appliance's runtime mode (from persistent storage)."""
        appliance_id = app.get("subentry_id") or app.get("id")
        if not appliance_id:
            _LOGGER.warning("Appliance %s has no subentry_id; using default mode", app.get(CONF_NAME))
            return DEFAULT_MODE
        return self.get_appliance_persistent_setting(
            appliance_id,
            CONF_RUNTIME_MODE,
            CONF_RUNTIME_MODES,
            DEFAULT_MODE,
        )

    def _basic_condition_met(self, app, avg_excess, remaining, config):
        appliance_name = app[CONF_NAME]
        mode = self._appliance_mode(app)

        if mode == "forced":
            return True

        is_off_peak = self._get_off_peak_status(config)
        if is_off_peak:
            if mode == "off_peak":
                _LOGGER.debug(
                    "%s mode=%s, is_off_peak=%s",
                    appliance_name,
                    mode,
                    is_off_peak,
                )
                return True

        # Use fixed_amps entity if set (will be scaled by optimizer), otherwise use min_amps for condition check
        required_a = app.get(CONF_MIN_AMPS, 6)
        if remaining >= required_a: 
            _LOGGER.debug(
                "%s mode=%s, remaining=%s, required_a=%s",
                appliance_name,
                mode,
                remaining,
                required_a,
            )
            return True
        
        _LOGGER.debug(
            "%s mode=%s, avg_excess=%s, remaining=%s, off_peak=%s",
            appliance_name,
            mode,
            avg_excess,
            remaining,
            is_off_peak,
        )

        # Peak hours
        if mode == "partial_solar":
            # Resolve voltage via the sensor reader — CONF_VOLTAGE may be an
            # entity id (e.g. input_number.voltage), not a raw number.
            voltage = self._read_sensor(config.get(CONF_VOLTAGE), default=DEFAULT_VOLTAGE)
            if voltage <= 0:
                voltage = DEFAULT_VOLTAGE
            partial_min_amps = app.get(CONF_PARTIAL_MIN_EXCESS, 1000) / voltage
            _LOGGER.debug(
                "%s mode=%s, avg_excess=%s, min_excess=%s",
                appliance_name,
                mode,
                avg_excess,
                partial_min_amps,
            )
            return avg_excess >= partial_min_amps
        elif mode == "full_solar":
            # Solar-only: run exclusively when the remaining solar budget fully
            # covers the appliance (the remaining >= required_a check above).
            # No partial-coverage or tariff fallback, so it never imports.
            _LOGGER.debug(
                "%s mode=full_solar not fully covered (remaining=%s < required_a=%s); off",
                appliance_name,
                remaining,
                required_a,
            )
            return False
        elif mode == "off_peak":
            breakeven = self.calculate_breakeven(
                required_a,
                config.get("cost_peak", 0.40),
                config.get("cost_offpeak", 0.20),
                config.get("gain_export", 0.10)
            )
            _LOGGER.debug(
                "%s mode=%s, avg_excess=%s, breakeven=%s",
                appliance_name,
                mode,
                avg_excess,
                breakeven,
            )
            return avg_excess >= breakeven

        return False

    async def _apply_appliance_states(
        self, appliances: list[dict], active: list[str]
    ) -> None:
        """Turn managed appliances on or off to match optimizer decisions."""
        active_set = set(active)
        _LOGGER.debug("apply states: active=%s", active_set)
        for app in appliances:
            entity_id = app.get(CONF_ENTITY_ID)
            if not entity_id:
                continue
            state = self.hass.states.get(entity_id)
            if state is None or state.state in _UNAVAILABLE_STATES:
                _LOGGER.debug("apply states: %s unavailable, skipped", entity_id)
                continue

            should_be_on = app[CONF_NAME] in active_set
            domain = split_entity_id(entity_id)[0]
            currently_on = state.state == STATE_ON

            if should_be_on and not currently_on:
                _LOGGER.info("turning ON %s (%s)", entity_id, app[CONF_NAME])
                await self.hass.services.async_call(
                    domain,
                    "turn_on",
                    {"entity_id": entity_id},
                    blocking=True,
                )
            elif not should_be_on and currently_on:
                _LOGGER.info("turning OFF %s (%s)", entity_id, app[CONF_NAME])
                await self.hass.services.async_call(
                    domain,
                    "turn_off",
                    {"entity_id": entity_id},
                    blocking=True,
                )

    def _get_measured_amps(self, app: dict, config) -> float | None:
        """Return the appliance's *measured* draw in amps, or None.

        Reads the optional per-appliance power sensor (watts, unit-normalised)
        and converts to amps using the configured voltage. Returns None when no
        sensor is configured or its value is unavailable.
        """
        entity_id = app.get(CONF_POWER_SENSOR)
        if not entity_id:
            return None
        watts = self._read_sensor_optional(entity_id)
        if watts is None:
            return None
        voltage = self._read_sensor(config.get(CONF_VOLTAGE), default=DEFAULT_VOLTAGE)
        if voltage <= 0:
            return None
        return watts / voltage

    def _get_actual_amps(self, app: dict, config) -> float:
        """Return the appliance's effective draw in amps (never None).

        Prefers the measured power sensor; when there is no sensor (or it is
        unavailable) it falls back to the configured fixed_amps number's value,
        and finally to the appliance's min_amps.
        """
        measured = self._get_measured_amps(app, config)
        if measured is not None:
            return measured
        fixed_entity = app.get(CONF_FIXED_AMPS)
        if fixed_entity:
            val = self._read_sensor_optional(fixed_entity)
            if val is not None:
                return val
        return app.get(CONF_MIN_AMPS, 6)

    def _apply_fixed_amps(self, app: dict, name: str, scaled_amps: float, now) -> float:
        """Update the fixed_amps entity in whole-amp steps with a deadband.

        The setpoint is quantised to ``FIXED_AMPS_STEP`` (1 A) and only moves when
        the newly computed value is more than ``FIXED_AMPS_DEADBAND`` (0.5 A) away
        from the current setpoint. Changes are additionally rate-limited by a time
        hysteresis (``fixed_amps_hysteresis_seconds``). Returns the effective
        amperage (the value the appliance is currently held at) for budgeting.
        """
        interval = app.get(
            CONF_FIXED_AMPS_HYSTERESIS_SECONDS, DEFAULT_FIXED_AMPS_HYSTERESIS_SECONDS
        )
        state = self.appliance_states.get(name)
        last_val = state.get("last_fixed_amps") if state else None
        last_change = state.get("last_fixed_amps_change") if state else None

        # Whole-amp setpoint with a 0.5 A margin/hysteresis. Run at N only once
        # N + 0.5 is available; once at N, hold it while the value stays within
        # [N - 0.5, N + 1.5), step UP when it reaches (N + 1) + 0.5 (e.g. 6 -> 7
        # at 7.5) and step DOWN when it falls below N - 0.5 (e.g. 6 -> 5 at 5.5),
        # landing on the adjacent amp without skipping. Full max is allowed once
        # the budget reaches it.
        min_a = app.get(CONF_MIN_AMPS, 6)
        max_a = app.get(CONF_MAX_AMPS, 16)
        db = FIXED_AMPS_DEADBAND
        if scaled_amps >= max_a:
            target = float(max_a)
        elif last_val is not None and (last_val - db) <= scaled_amps < (last_val + 1 + db):
            target = last_val                          # within hysteresis band → hold
        elif last_val is None or scaled_amps >= last_val + 1 + db:
            target = float(int(scaled_amps - db))      # step up (or first set)
        else:
            target = float(int(scaled_amps + db))      # step down to the adjacent amp
        target = max(float(min_a), min(float(max_a), target))

        def _write(value: float) -> float:
            if state is not None:
                state["last_fixed_amps"] = value
                state["last_fixed_amps_change"] = now
            self.hass.async_create_task(
                self._update_fixed_amps_entity(app, value)
            )
            return value

        # First write: nothing to gate.
        if last_val is None:
            return _write(target)
        if target == last_val:
            # Target unchanged (or within the deadband). Skip only if the entity
            # already holds it; if it has drifted (e.g. set externally), re-assert
            # our setpoint instead of silently sticking at a stale value.
            current = self._read_sensor_optional(app.get(CONF_FIXED_AMPS))
            if current is None or current == target:
                return last_val
            _LOGGER.debug(
                "%s fixed_amps drifted (entity=%.2f, want %.2f); re-asserting",
                name, current, target,
            )
            return _write(target)

        # The value wants to change; only allow it once the window has elapsed.
        if last_change is None or (now - last_change).total_seconds() >= interval:
            return _write(target)

        _LOGGER.debug(
            "%s fixed_amps change %.2f->%.2f within %ss hold; keeping setpoint",
            name, last_val, target, interval,
        )
        return last_val

    def _calculate_scaled_amps(self, app: dict, remaining_amps: float) -> float:
        """Calculate scaled amperage based on available excess, between min and max."""
        min_a = app.get(CONF_MIN_AMPS, 6)
        max_a = app.get(CONF_MAX_AMPS, 16)
        
        # Scale between min and max based on remaining amps
        # If remaining >= max, use max; if remaining <= min, use min; otherwise scale proportionally
        if remaining_amps >= max_a:
            return float(max_a)
        elif remaining_amps <= min_a:
            return float(min_a)
        else:
            # Linear scale between min and max
            return float(remaining_amps)

    async def _update_fixed_amps_entity(self, app: dict, calculated_amps: float) -> None:
        """Update the fixed_amps entity with the calculated value."""
        fixed_amps_entity = app.get(CONF_FIXED_AMPS)
        if not fixed_amps_entity:
            return
        
        try:
            await self.hass.services.async_call(
                "input_number",
                "set_value",
                {
                    "entity_id": fixed_amps_entity,
                    "value": calculated_amps,
                },
                blocking=True,
            )
            _LOGGER.debug(
                "Updated %s fixed_amps entity to %.1f amps",
                app.get(CONF_NAME),
                calculated_amps,
            )
        except Exception as err:
            _LOGGER.warning(
                "Failed to update fixed_amps entity %s: %s",
                fixed_amps_entity,
                err,
            )

    def _get_off_peak_status(self, config):
        return not self._get_peak_tarriff_status(config)

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