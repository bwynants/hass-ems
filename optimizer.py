"""Appliance optimizer for the Energy Management System.

``ApplianceOptimizerMixin`` holds the surplus-allocation logic: excess-amps
calculation, per-appliance run conditions (mode / hysteresis / SoC / peak
throttling), fixed_amps setpoint stepping, and applying on/off decisions.

It is mixed into ``SolarApplianceCoordinator`` and relies on these coordinator
attributes/methods: ``self.hass``, ``self.appliance_states``, ``self._last_excess``,
``self._last_soc``, ``self._battery_min_soc``, ``self._battery_protection_mode``,
``self._read_sensor``/``self._read_sensor_optional`` and
``self.get_appliance_persistent_setting``.
"""
from __future__ import annotations

import logging

from homeassistant.const import STATE_ON
from homeassistant.core import split_entity_id
from homeassistant.util import dt as dt_util

from .const import (
    CONF_VOLTAGE, CONF_MIN_SOC_OVERRIDE, CONF_SOC_HYSTERESIS_PERCENT,
    CONF_IS_BATTERY, CONF_IGNORE_BATTERY_PROTECTION, CONF_CONNECTED_SENSOR,
    BATTERY_MODE_RELAXED,
    MODE_FORCED, MODE_PARTIAL_SOLAR, MODE_FULL_SOLAR, MODE_OFF_PEAK, MODE_UNMANAGED,
    CONF_SOLAR_PRODUCTION, CONF_HOUSE_LOAD, CONF_GRID_EXPORT, CONF_ENTITY_ID,
    CONF_NAME, CONF_PRIORITY, CONF_MAX_AMPS, CONF_MIN_AMPS, CONF_FIXED_AMPS,
    CONF_FIXED_AMPS_HYSTERESIS_SECONDS, DEFAULT_FIXED_AMPS_HYSTERESIS_SECONDS,
    CONF_POWER_SENSOR, CONF_PARTIAL_MIN_EXCESS, CONF_HYSTERESIS_ON_OFF_SECONDS,
    CONF_AVERAGING_WINDOW, CONF_PEAK_TARIFF_SENSOR,
    DEFAULT_MIN_AMPS, DEFAULT_MAX_AMPS, DEFAULT_VOLTAGE,
    DEFAULT_HYSTERESIS, DEFAULT_AVERAGING, DEFAULT_SOC_HYSTERESIS,
    DEFAULT_IGNORE_PROTECTION, DEFAULT_MODE, CONF_RUNTIME_MODE, CONF_RUNTIME_MODES,
    CONTROLLER_UPDATE_INTERVAL, FIXED_AMPS_DEADBAND,
)
from .helpers import (
    UNAVAILABLE_STATES,
    calculate_scaled_amps,
    calculate_breakeven,
    default_appliance_state,
)

_LOGGER = logging.getLogger(__name__)


class ApplianceOptimizerMixin:
    """Surplus-allocation and appliance-control logic for the coordinator."""

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
            # Unmanaged appliances are part of the base load (like the house),
            # not something EMS can free/reallocate, so don't add their draw back.
            if self._is_unmanaged(app):
                continue
            if self._is_entity_on(app):
                total += self._get_actual_amps(app, config)
        return total

    def _update_histories(self, appliances, excess_amps):
        for app in appliances:
            name = app[CONF_NAME]
            if name not in self.appliance_states:
                self.appliance_states[name] = default_appliance_state()
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
            min_a = app.get(CONF_MIN_AMPS, DEFAULT_MIN_AMPS)
            mode = self._appliance_mode(app)
            if mode == MODE_UNMANAGED:
                _LOGGER.debug("  - %s: unmanaged, left alone", name)
                continue
            # In `forced` mode, solar surplus and the grid peak-headroom are
            # ADDITIVE — solar is used on top of grid power (the grid part still
            # bounded by the peak/High limit). Other modes use solar only, capped
            # by the headroom (the prediction throttle).
            forced_additive = mode == MODE_FORCED and peak_remaining is not None
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
        state = self.appliance_states.setdefault(name, default_appliance_state())

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
                if self._battery_protection_mode == BATTERY_MODE_RELAXED:
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
            required_a = app.get(CONF_MIN_AMPS, DEFAULT_MIN_AMPS)
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

    def _is_unmanaged(self, app) -> bool:
        """True when the appliance's mode is 'unmanaged' — EMS leaves it alone."""
        return self._appliance_mode(app) == MODE_UNMANAGED

    def _basic_condition_met(self, app, avg_excess, remaining, config):
        appliance_name = app[CONF_NAME]
        mode = self._appliance_mode(app)

        if mode == MODE_FORCED:
            return True

        is_off_peak = self._get_off_peak_status(config)
        if is_off_peak:
            if mode == MODE_OFF_PEAK:
                _LOGGER.debug(
                    "%s mode=%s, is_off_peak=%s",
                    appliance_name,
                    mode,
                    is_off_peak,
                )
                return True

        # Use fixed_amps entity if set (will be scaled by optimizer), otherwise use min_amps for condition check
        required_a = app.get(CONF_MIN_AMPS, DEFAULT_MIN_AMPS)
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
        if mode == MODE_PARTIAL_SOLAR:
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
        elif mode == MODE_FULL_SOLAR:
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
        elif mode == MODE_OFF_PEAK:
            breakeven = calculate_breakeven(
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
            # Unmanaged appliances are never switched by EMS — leave them as-is.
            if self._is_unmanaged(app):
                continue
            entity_id = app.get(CONF_ENTITY_ID)
            if not entity_id:
                continue
            state = self.hass.states.get(entity_id)
            if state is None or state.state in UNAVAILABLE_STATES:
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
        return app.get(CONF_MIN_AMPS, DEFAULT_MIN_AMPS)

    def _apply_fixed_amps(self, app: dict, name: str, scaled_amps: float, now) -> float:
        """Update the fixed_amps entity in whole-amp steps with a deadband.

        The setpoint is quantised to whole amps and only moves when the newly
        computed value is more than ``FIXED_AMPS_DEADBAND`` (0.5 A) away from the
        current setpoint. Changes are additionally rate-limited by a time
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
        min_a = app.get(CONF_MIN_AMPS, DEFAULT_MIN_AMPS)
        max_a = app.get(CONF_MAX_AMPS, DEFAULT_MAX_AMPS)
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
        return calculate_scaled_amps(
            app.get(CONF_MIN_AMPS, DEFAULT_MIN_AMPS),
            app.get(CONF_MAX_AMPS, DEFAULT_MAX_AMPS),
            remaining_amps,
        )

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
        return not self._get_peak_tariff_status(config)

    def _get_peak_tariff_status(self, config):
        entity_id = config.get(CONF_PEAK_TARIFF_SENSOR)
        if not entity_id:
            return True
        state = self.hass.states.get(entity_id)
        return state and state.state == "on"
