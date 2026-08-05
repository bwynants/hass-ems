"""Appliance optimizer for the EMS core — pure decision logic.

``optimize()`` and ``critical_shed()`` take a snapshot (appliance inputs, runtime
state, budgets, energy/battery context, and ``now``) and return the decisions
(active list, decided amps, fixed_amps commands). They mutate the passed
``states`` dict — the same stateful hysteresis/latch semantics as before — but
touch no Home Assistant APIs.
"""
from __future__ import annotations

import logging

from .const import (
    MODE_FORCED, MODE_PARTIAL_SOLAR, MODE_FULL_SOLAR, MODE_OFF_PEAK, MODE_UNMANAGED,
    BATTERY_MODE_RELAXED, DEFAULT_VOLTAGE, CONTROLLER_UPDATE_INTERVAL,
    FORCED_PEAK_HEADROOM_AMPS, SUNDOWN_SOLAR_W,
)
from .models import (
    ApplianceInput, ApplianceState, EnergyInput, BatteryStatus,
    SetFixedAmps, OptimizeResult, ShedResult,
)
from .energy import scaled_amps, breakeven
from . import fixed_amps

_LOGGER = logging.getLogger(__name__)


def record_excess(
    appliances: list[ApplianceInput],
    states: dict[str, ApplianceState],
    excess_amps: float,
) -> None:
    """Append the current excess to each appliance's averaging history.

    Called once per cycle before :func:`optimize` (and also on disabled cycles),
    so the rolling average is continuous regardless of whether the optimizer ran.
    """
    for app in appliances:
        state = states.setdefault(app.uid, ApplianceState())
        state.excess_history.append(excess_amps)


def _averaged_excess(app: ApplianceInput, state: ApplianceState, fallback: float) -> float:
    if not state.excess_history:
        return fallback
    window = app.averaging_window_seconds
    samples = max(1, int(window / CONTROLLER_UPDATE_INTERVAL))
    recent = list(state.excess_history)[-samples:]
    return sum(recent) / len(recent) if recent else fallback


def _basic_condition(
    app: ApplianceInput,
    state: ApplianceState,
    avg_excess: float,
    remaining: float,
    energy: EnergyInput,
) -> bool:
    name = app.name
    mode = app.mode

    if mode == MODE_FORCED:
        return True

    is_off_peak = energy.off_peak
    # off_peak mode runs freely during the CHEAPEST tariff period. Normally that
    # is the configured off-peak window. But with dynamic tariffs the peak price
    # can fall BELOW the off-peak price — then the "peak" window is actually the
    # cheap one, so run freely there too instead of gating on the solar
    # breakeven (which is meaningless, and would even go negative, once peak is
    # the cheaper rate). Off-peak-hours free-run is unchanged.
    peak_cheaper = energy.cost_peak < energy.cost_offpeak
    if mode == MODE_OFF_PEAK and (is_off_peak or peak_cheaper):
        _LOGGER.debug(
            "%s mode=off_peak run-free (is_off_peak=%s, peak_cheaper=%s: peak=%.3f offpeak=%.3f)",
            name, is_off_peak, peak_cheaper, energy.cost_peak, energy.cost_offpeak,
        )
        return True

    required_a = app.min_amps

    if mode == MODE_FULL_SOLAR:
        # Solar-only: never imports from the grid, and no partial/off-peak
        # fallback. Start and stop are ASYMMETRIC:
        #   * to START (currently off) require BOTH the current budget AND the
        #     sustained average to cover the appliance, so a single-cycle spike
        #     can't commit it to a run it can't hold;
        #   * to STAY ON (currently on) require only the sustained average to
        #     still cover it, so a momentary dip — with the rolling average
        #     still healthy — doesn't drop a long-running appliance the very
        #     next cycle. (The instantaneous budget alone is too twitchy once
        #     the on/off hysteresis window, timed from the last change, has
        #     expired.)
        # (Checked before the generic instantaneous early-return below so the
        # averaged requirement is not bypassed.)
        if state.is_on:
            covered = avg_excess >= required_a
        else:
            covered = remaining >= required_a and avg_excess >= required_a
        # "Transient spike": current budget covers but the sustained average
        # doesn't. Logged at INFO once on entering the condition (it's the
        # non-obvious reason the run condition fails), then at DEBUG on
        # repeats. The wording claims only the run condition — the on/off
        # hysteresis in _should_run may still hold a running appliance on.
        spike = not covered and remaining >= required_a
        level = logging.INFO if spike and not state.full_solar_spike else logging.DEBUG
        state.full_solar_spike = spike
        _LOGGER.log(
            level,
            "%s mode=full_solar %s (remaining=%.2f A, avg_excess=%.2f A, required=%.2f A)%s",
            name,
            "covered" if covered else "run condition not met",
            remaining, avg_excess, required_a,
            "; current covers but sustained avg too low (transient spike) — "
            "waiting for sustained solar" if spike else "",
        )
        return covered

    if remaining >= required_a:
        _LOGGER.debug("%s mode=%s, remaining=%s, required_a=%s", name, mode, remaining, required_a)
        return True

    _LOGGER.debug(
        "%s mode=%s, avg_excess=%s, remaining=%s, off_peak=%s",
        name, mode, avg_excess, remaining, is_off_peak,
    )

    # Peak hours
    if mode == MODE_PARTIAL_SOLAR:
        voltage = energy.voltage if energy.voltage > 0 else DEFAULT_VOLTAGE
        partial_min_amps = app.partial_min_excess / voltage
        _LOGGER.debug("%s mode=%s, avg_excess=%s, min_excess=%s", name, mode, avg_excess, partial_min_amps)
        return avg_excess >= partial_min_amps
    elif mode == MODE_OFF_PEAK:
        be = breakeven(required_a, energy.cost_peak, energy.cost_offpeak, energy.gain_export)
        _LOGGER.debug("%s mode=%s, avg_excess=%s, breakeven=%s", name, mode, avg_excess, be)
        return avg_excess >= be

    return False


def _should_run(
    app: ApplianceInput,
    state: ApplianceState,
    remaining_amps: float,
    cycle_excess: float,
    energy: EnergyInput,
    battery: BatteryStatus,
    now,
) -> bool:
    if not app.connected:
        _LOGGER.debug("%s not connected, skipping", app.name)
        return False

    name = app.name
    avg_excess = _averaged_excess(app, state, cycle_excess)
    _LOGGER.debug(
        "evaluating %s: avg_excess=%s, remaining=%s, is_on=%s, last_change=%s",
        name, avg_excess, remaining_amps, state.is_on, state.last_change,
    )

    # SoC Protection. In ``relaxed`` mode a low battery doesn't hard-off the
    # appliance; instead it may only run while solar surplus covers it.
    soc_block_solar_only = False
    if app.is_battery:
        # The battery is the thing being protected; never block it on its own
        # SoC or it could never charge back up. Clear any stale latch.
        if state.soc_blocked:
            state.soc_blocked = False
            _LOGGER.debug("%s soc_blocked cleared (appliance is a battery)", name)
    elif app.ignore_battery_protection:
        if state.soc_blocked:
            state.soc_blocked = False
            _LOGGER.debug("%s soc_blocked cleared (battery protection ignored)", name)
    elif battery.min_soc is not None:
        if battery.soc is None:
            # SoC sensor unavailable: fail safe by HOLDING the existing latch —
            # a dropout must neither lift an active block (draining the low
            # battery) nor set a new one on missing data.
            if state.soc_blocked:
                _LOGGER.debug("%s soc_blocked held (SoC sensor unavailable)", name)
        else:
            # Use the per-appliance override only when explicitly set (0 is
            # valid), otherwise the threshold configured on the battery
            # appliance(s).
            override = app.min_soc_override
            min_soc = override if override is not None else battery.min_soc
            soc_hyst = app.soc_hysteresis_percent
            # Clamp the clear threshold so it is always reachable (SoC maxes at 100%).
            clear_at = min(min_soc + soc_hyst, 100)
            if state.soc_blocked:
                if battery.soc >= clear_at:
                    state.soc_blocked = False
                    _LOGGER.debug("%s soc_blocked cleared, soc=%s >= %s", name, battery.soc, clear_at)
            elif battery.soc < min_soc:
                state.soc_blocked = True
                _LOGGER.debug("%s soc_blocked set, soc=%s < %s", name, battery.soc, min_soc)
        if state.soc_blocked:
            if battery.protection_mode == BATTERY_MODE_RELAXED:
                soc_block_solar_only = True
                _LOGGER.debug("%s soc-blocked (relaxed): solar-only until SoC recovers", name)
            else:
                _LOGGER.debug("%s blocked by SOC protection (strict)", name)
                return False

    if soc_block_solar_only:
        # Battery low (relaxed): only run while the solar budget covers the
        # appliance. full_solar keeps its sustained-average requirement too —
        # the bypass must never be WEAKER than the mode's own gate, or a low
        # battery would make a full_solar appliance easier to start.
        required_a = app.min_amps
        condition_met = remaining_amps >= required_a and (
            app.mode != MODE_FULL_SOLAR or avg_excess >= required_a
        )
        # _basic_condition (the spike flag's only writer) is bypassed here;
        # re-arm the flag so the one-shot INFO log still fires on the next
        # genuine transient spike after SoC recovers.
        state.full_solar_spike = False
    else:
        condition_met = _basic_condition(app, state, avg_excess, remaining_amps, energy)
    _LOGGER.debug("%s condition_met=%s", name, condition_met)

    # Already in the desired state: nothing to do, leave the timer untouched.
    if condition_met == state.is_on:
        return state.is_on

    # A transition is wanted. Hysteresis only damps changes away from an
    # already-settled state; the first decision (last_change is None) and a
    # change after the window has elapsed both transition immediately.
    hyst_sec = app.hysteresis_seconds
    last_change = state.last_change
    if last_change is not None:
        time_since_change = (now - last_change).total_seconds()
        if time_since_change < hyst_sec:
            _LOGGER.info(
                "%s within hysteresis (%ds); holding state=%s",
                name, int(time_since_change), state.is_on,
            )
            return state.is_on

    state.is_on = condition_met
    state.last_change = now
    _LOGGER.debug("%s transitioning to %s", name, "on" if condition_met else "off")
    return condition_met


def optimize(
    appliances: list[ApplianceInput],
    states: dict[str, ApplianceState],
    excess_amps: float,
    peak_amps: float | None,
    energy: EnergyInput,
    battery: BatteryStatus,
    now,
    hold_ramp: bool = False,
) -> OptimizeResult:
    """Allocate the surplus across appliances in priority order.

    Mutates ``states`` (hysteresis timers, SoC latch, fixed_amps tracking).
    Call :func:`record_excess` first each cycle to roll the excess into the
    averaging history. Returns the active appliance uids, the draw decided this
    cycle, and the fixed_amps write commands for the shell to apply.
    """
    sorted_apps = sorted(appliances, key=lambda a: a.priority)
    active: list[str] = []          # appliance uids
    active_names: list[str] = []    # display names, for the log lines only
    commands: list[SetFixedAmps] = []
    remaining = excess_amps
    decided_amps = 0.0
    peak_remaining = peak_amps

    _LOGGER.debug(
        "optimize: %d appliance(s) in priority order | excess=%.2f A | peak headroom=%s",
        len(sorted_apps), remaining,
        "off" if peak_remaining is None else f"{peak_remaining:.2f} A",
    )

    for app in sorted_apps:
        name = app.name
        min_a = app.min_amps
        mode = app.mode
        if mode == MODE_UNMANAGED:
            _LOGGER.debug("  - %s: unmanaged, left alone", name)
            continue
        # In `forced` mode, solar surplus and the grid peak-headroom are ADDITIVE
        # — solar is used on top of grid power (the grid part still bounded by the
        # peak/High limit). Other modes use solar only, capped by the headroom.
        # An `off_peak` appliance behaves like forced too, but only during
        # off-peak hours at sundown (no solar to follow): cheap night grid should
        # charge it at full rate up to the peak limit, not dribble at min_amps.
        off_peak_night = (
            mode == MODE_OFF_PEAK and energy.off_peak
            and energy.solar_w <= SUNDOWN_SOLAR_W
        )
        forced_additive = (mode == MODE_FORCED or off_peak_night) and peak_remaining is not None
        solar_avail = max(remaining, 0.0)
        if forced_additive:
            # Reserve FORCED_PEAK_HEADROOM_AMPS of the grid (peak) headroom so the
            # appliance doesn't scale right up to the quarter-hour demand limit —
            # filling it completely pushes the peak to the cap and gets the
            # appliance shed, causing on/off oscillation. Solar is used freely;
            # only the grid portion is held back.
            grid_headroom = max(peak_remaining - FORCED_PEAK_HEADROOM_AMPS, 0.0)
            forced_budget = remaining + grid_headroom
        else:
            forced_budget = None

        state = states.setdefault(app.uid, ApplianceState())

        # Hard peak cap: if the appliance can't reach its minimum from the budget
        # it may use, it can't run this cycle. The off is immediate (protective),
        # but the state machine is kept consistent: the transition is recorded so
        # re-activation is damped by the on/off hysteresis (no 10s relay flapping
        # when headroom hovers around min), and the setpoint tracking is cleared
        # so it comes back with a fresh write instead of the stale value.
        if peak_remaining is not None:
            reachable = forced_budget if forced_additive else peak_remaining
            if reachable < min_a:
                _LOGGER.debug(
                    "  - %s (mode=%s): OFF, peak headroom too low (%.1f A available < %d A min)",
                    name, mode, reachable, min_a,
                )
                if state.is_on:
                    state.is_on = False
                    state.last_change = now
                state.last_fixed_amps = None
                state.last_fixed_amps_change = None
                continue
        if _should_run(app, state, remaining, excess_amps, energy, battery, now):
            active.append(app.uid)
            active_names.append(name)

            if forced_additive:
                scaled = scaled_amps(min_a, app.max_amps, forced_budget)
            else:
                scaled = scaled_amps(min_a, app.max_amps, remaining)
                if peak_remaining is not None:
                    scaled = max(min(scaled, peak_remaining), min_a)

            # Grid reading frozen (stale source): never let a running appliance's
            # setpoint climb. The surplus add-back would otherwise feed a runaway
            # (add-back = setpoint → higher budget → higher setpoint → …) when the
            # grid can't confirm the extra draw — the same loop the power-sensor
            # hold guards against. Ramp-DOWN is still allowed.
            if hold_ramp and state.last_fixed_amps is not None:
                scaled = min(scaled, state.last_fixed_amps)

            # Command the setpoint if a fixed_amps entity is configured.
            commanded = None
            if app.has_fixed_amps:
                decision = fixed_amps.decide(
                    scaled, min_a, app.max_amps,
                    state.last_fixed_amps, state.last_fixed_amps_change,
                    now, app.fixed_amps_hold_seconds, app.fixed_amps_value,
                )
                if decision.write_value is not None:
                    state.last_fixed_amps = decision.write_value
                    state.last_fixed_amps_change = now
                    commands.append(SetFixedAmps(app.uid, decision.write_value))
                commanded = decision.effective

            # Deduct the draw from the budget: measured, else commanded, else
            # min. The measured value only counts while the appliance is
            # actually running — on the activation cycle an OFF appliance's
            # power sensor reads 0.0, which would deduct nothing and let the
            # same surplus be allocated twice.
            measured = app.measured_amps
            if measured is not None and app.entity_on:
                used = measured
            elif commanded is not None:
                used = commanded
            else:
                used = min_a
            _LOGGER.debug(
                "  - %s (mode=%s): ON, scaled=%.1f A, draw=%.2f A (%s) | budget before: excess=%.2f A peak=%s",
                name, mode, scaled, used,
                "measured" if measured is not None else ("setpoint" if commanded is not None else "min_amps"),
                remaining,
                "off" if peak_remaining is None else f"{peak_remaining:.2f} A",
            )
            decided_amps += used
            if forced_additive:
                solar_used = min(used, solar_avail)
                remaining -= solar_used
                peak_remaining -= used - solar_used
            else:
                remaining -= used
                if peak_remaining is not None:
                    # Peak headroom is GRID-import allowance (the demand math
                    # works on grid offtake): only the part of the draw beyond
                    # the available solar consumes it. Charging solar-covered
                    # draw against it would falsely starve downstream (forced)
                    # appliances of grid headroom that is actually untouched.
                    solar_used = min(used, solar_avail)
                    peak_remaining -= max(used - solar_used, 0.0)
        else:
            _LOGGER.debug("  - %s (mode=%s): OFF, run condition not met", name, mode)
            # Forget the setpoint tracking: a re-activation must write a fresh
            # setpoint immediately instead of silently holding the stale value
            # through the hold window (e.g. switching back on at 16 A against a
            # 6 A budget).
            state.last_fixed_amps = None
            state.last_fixed_amps_change = None

    _LOGGER.debug(
        "optimize done: active=%s | leftover excess=%.2f A | leftover peak=%s",
        active_names, remaining,
        "off" if peak_remaining is None else f"{peak_remaining:.2f} A",
    )
    return OptimizeResult(active=active, decided_amps=decided_amps, fixed_amps_commands=commands)


def critical_shed(
    appliances: list[ApplianceInput],
    states: dict[str, ApplianceState],
    now,
) -> ShedResult:
    """Shed load on critical demand, gracefully.

    Each running appliance above its minimum (with an adjustable fixed_amps
    setpoint) is throttled to ``min_amps`` and kept on; appliances already at the
    minimum, or without an adjustable setpoint, are switched off. Returns the
    kept appliance uids/amps and the fixed_amps commands (throttle-to-min writes).
    """
    kept: list[str] = []
    kept_amps = 0.0
    commands: list[SetFixedAmps] = []
    for app in appliances:
        name = app.name
        state = states.setdefault(app.uid, ApplianceState())
        if app.mode == MODE_UNMANAGED or not app.entity_on:
            continue
        min_a = app.min_amps
        current = app.fixed_amps_value if app.has_fixed_amps else None
        if current is not None and current > min_a:
            # Step down to min and keep it on this cycle.
            state.is_on = True
            state.last_change = now
            state.last_fixed_amps = float(min_a)
            state.last_fixed_amps_change = now
            commands.append(SetFixedAmps(app.uid, float(min_a)))
            kept.append(app.uid)
            kept_amps += min_a
            _LOGGER.warning(
                "demand CRITICAL: %s throttled %.1f A -> min %d A (will shed if still critical)",
                name, current, min_a,
            )
        else:
            # Already at min, or no adjustable setpoint: switch off.
            state.is_on = False
            state.last_change = now
            state.last_fixed_amps = None
            state.last_fixed_amps_change = None
            _LOGGER.warning(
                "demand CRITICAL: %s shed (off)%s",
                name, "" if app.has_fixed_amps else " (no adjustable setpoint)",
            )
    return ShedResult(kept=kept, kept_amps=kept_amps, fixed_amps_commands=commands)
