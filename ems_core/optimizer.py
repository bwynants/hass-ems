"""Appliance optimizer for the EMS core — pure decision logic.

``optimize()`` and ``critical_shed()`` take a snapshot (appliance inputs, runtime
state, budgets, energy context, and ``now``) and return the decisions
(active list, decided amps, fixed_amps commands). They mutate the passed
``states`` dict — the same stateful hysteresis semantics as before — but
touch no Home Assistant APIs.
"""
from __future__ import annotations

from datetime import timedelta
import logging

from .const import (
    MODE_FORCED, MODE_PARTIAL_SOLAR, MODE_FULL_SOLAR, MODE_OFF_PEAK, MODE_UNMANAGED,
    CONTROLLER_UPDATE_INTERVAL, DEMAND_QUARTER_SECONDS,
    SUNDOWN_SOLAR_W, ON_OFF_HYSTERESIS_AMPS,
    PEAK_GATE_SHED_CYCLES, FIXED_AMPS_MAX_STEP_UP, PEAK_CUT_HOLD_SECONDS,
    NOT_CONSUMING_AMPS, NOT_CONSUMING_GRACE_SECONDS, SOC_FULL_PERCENT,
)
from .models import (
    ApplianceInput, ApplianceState, EnergyInput,
    SetFixedAmps, OptimizeResult, ShedResult,
)
from .energy import scaled_amps, breakeven
from . import fixed_amps

_LOGGER = logging.getLogger(__name__)

_PEAK_CUT_HOLD = timedelta(seconds=PEAK_CUT_HOLD_SECONDS)


def quarter_id(now) -> int:
    """The quarter hour ``now`` falls in (``timestamp // 900``), the key of
    the per-quarter shed lockout; the same id the demand side uses."""
    return int(now.timestamp() // DEMAND_QUARTER_SECONDS)


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


def force_charge_active(app: ApplianceInput) -> bool:
    """EV rule: the car is plugged in AND its SoC is below the appliance's threshold.

    Fail-safe: no threshold (no SoC sensor configured) or no SoC reading (sensor
    unavailable/unknown, e.g. no car plugged in) ⇒ False. Unmanaged appliances
    are never overridden — that mode means EMS keeps its hands off entirely.
    No hysteresis on purpose: SoC only rises while the car is plugged in, so the
    release at ``soc >= threshold`` is clean.
    """
    if app.mode == MODE_UNMANAGED or not app.connected:
        return False
    if app.soc is None or app.force_charge_below_soc is None:
        return False
    return app.soc < app.force_charge_below_soc


def effective_mode(app: ApplianceInput) -> str:
    """The mode the optimizer drives this appliance with THIS cycle.

    A plugged-in car below its force-charge threshold is driven as ``forced``
    (charge from the grid now, additive solar+grid budget) regardless of the
    mode the user selected; it reverts once the threshold is reached.
    """
    return MODE_FORCED if force_charge_active(app) else app.mode


def not_consuming(app: ApplianceInput, state: ApplianceState, active: bool, now) -> bool:
    """Track "EMS switched it on, but it isn't drawing anything".

    Only judged for appliances with a power sensor that are active this cycle;
    a start-up delay is covered by the grace period, a power-sensor dropout
    (``measured_amps`` None, or a reading the shell is holding from before the
    dropout — ``measured_live`` False) is never a fault, nor is a controlled
    entity that is unavailable (EMS cannot have switched it on), and a car at
    or above SOC_FULL_PERCENT tapering off is normal. Anything else resets the
    timer. Mutates ``state.not_consuming_since``.

    The held-reading and unavailable-entity cases are what a charger that
    drops off the network looks like: the shell keeps its last 0 A reading,
    off-peak starts, EMS "activates" it — and without these guards the flag
    came on 5 minutes later and announced a charging failure for a link
    outage (2026-10-29).
    """
    if (
        not active
        or not app.has_power_sensor
        or app.measured_amps is None
        or not app.measured_live
        or not app.entity_available
        or app.measured_amps >= NOT_CONSUMING_AMPS
        or (app.soc is not None and app.soc >= SOC_FULL_PERCENT)
    ):
        state.not_consuming_since = None
        return False
    if state.not_consuming_since is None:
        state.not_consuming_since = now
        return False
    return (now - state.not_consuming_since).total_seconds() >= NOT_CONSUMING_GRACE_SECONDS


def _basic_condition(
    app: ApplianceInput,
    state: ApplianceState,
    avg_excess: float,
    remaining: float,
    energy: EnergyInput,
    mode: str,
) -> bool:
    name = app.name

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

    # The amps the appliance must be able to draw. Starting carries a fixed
    # ON_OFF_HYSTERESIS_AMPS margin on top of min_amps; staying on needs only
    # min_amps itself. That 0.5 A gap is the on/off deadband — it is NOT the
    # setpoint step (which follows the "ampere to use" entity), so a finely
    # stepped charger still can't be flipped on and off by sub-amp noise.
    required_a = app.min_amps if state.is_on else app.min_amps + ON_OFF_HYSTERESIS_AMPS

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
        partial_min_amps = app.partial_min_excess_amps
        # Same on/off deadband as the min-amps gate above: an OFF appliance
        # starts only once the averaged excess clears the threshold by
        # ON_OFF_HYSTERESIS_AMPS; once ON it holds down to the threshold
        # itself. Without it an average hovering at the threshold flips the
        # appliance every time the time hysteresis expires. A zero threshold
        # means "no threshold" (run on any non-negative average, grid top-up
        # bounded by the peak gate) and keeps its plain sign check.
        on_margin = ON_OFF_HYSTERESIS_AMPS if partial_min_amps > 0 else 0.0
        partial_required = partial_min_amps if state.is_on else partial_min_amps + on_margin
        _LOGGER.debug(
            "%s mode=%s, avg_excess=%s, min_excess=%s, required=%s",
            name, mode, avg_excess, partial_min_amps, partial_required,
        )
        return avg_excess >= partial_required
    elif mode == MODE_OFF_PEAK:
        be = breakeven(required_a, energy.cost_peak, energy.cost_offpeak, energy.gain_now)
        _LOGGER.debug(
            "%s mode=%s, avg_excess=%s, breakeven=%s (peak=%.3f offpeak=%.3f gain_now=%.3f)",
            name, mode, avg_excess, be, energy.cost_peak, energy.cost_offpeak, energy.gain_now,
        )
        return avg_excess >= be

    return False


def _should_run(
    app: ApplianceInput,
    state: ApplianceState,
    remaining_amps: float,
    cycle_excess: float,
    energy: EnergyInput,
    now,
    mode: str,
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

    condition_met = _basic_condition(app, state, avg_excess, remaining_amps, energy, mode)
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
    now,
    hold_ramp: bool = False,
    proj_amps: float | None = None,
) -> OptimizeResult:
    """Allocate the surplus across appliances in priority order.

    Mutates ``states`` (hysteresis timers, fixed_amps tracking).
    Call :func:`record_excess` first each cycle to roll the excess into the
    averaging history. Returns the active appliance uids, the draw decided this
    cycle, and the fixed_amps write commands for the shell to apply.

    ``peak_amps`` is the instantaneous grid ceiling (the High limit);
    ``proj_amps`` the projected one for the rest of the quarter (the demand
    side's ``projected_ceiling``, None at the quarter boundary). The lower of
    the two bounds the grid draw, so setpoints are lowered — and as a last
    resort the appliance shed — before the High limit itself is reached. With
    the demand feature on, an appliance shed for peak stays off for the rest
    of its quarter.
    """
    sorted_apps = sorted(appliances, key=lambda a: a.priority)
    active: list[str] = []          # appliance uids
    active_names: list[str] = []    # display names, for the log lines only
    forced: list[str] = []          # uids overridden to `forced` by the car-SoC rule
    peak_limited: list[str] = []    # uids the peak logic acted on this cycle
    commands: list[SetFixedAmps] = []
    remaining = excess_amps
    decided_amps = 0.0
    peak_remaining = peak_amps

    # Two ceilings, the effective one being the lower. The instantaneous one
    # is the High limit itself; the projected one aims the quarter at the
    # shed limit (High − the shed margin), which is the only safety: in a
    # steady quarter it binds from the first minute and keeps the estimate
    # under High, so no extra reserve is held back on the instantaneous
    # ceiling (the 1 A forced reserve of 0.7.0 is gone). The projection can
    # only tighten: late in a quarter that started quietly it allows far more
    # than the High limit, and the minimum keeps the instantaneous bound as
    # the cap — the average so far is below High then, so drawing at High for
    # the rest still ends below it. Both are drawn down by the grid draw
    # allocated below.
    proj_remaining = proj_amps if peak_remaining is not None else None
    if proj_remaining is not None:
        _LOGGER.debug(
            "peak ceiling: instantaneous %.2f A, projected %.2f A for the rest of the quarter",
            peak_remaining, proj_remaining,
        )

    def _ceiling() -> float:
        """Grid headroom still available: the tighter of the two ceilings."""
        if proj_remaining is None:
            return peak_remaining
        return min(peak_remaining, proj_remaining)

    # The quarter the shed lockout is keyed on; only meaningful with the
    # demand feature (an instantaneous ceiling) on.
    this_quarter = quarter_id(now) if peak_remaining is not None else None

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
        state = states.setdefault(app.uid, ApplianceState())

        # Car-SoC override: resolved ONCE here and threaded down into the run
        # condition, so the budget path below and _should_run agree — a forced
        # car must draw the additive solar+grid budget, not solar only. Recorded
        # before the hard peak gate on purpose: "forced" then reads as "EMS wants
        # to force-charge", even when the quarter-hour cap keeps the charger off.
        soc_forced = force_charge_active(app)
        if soc_forced:
            mode = MODE_FORCED
            forced.append(app.uid)
        if soc_forced != state.force_charging:
            state.force_charging = soc_forced
            if soc_forced:
                _LOGGER.info(
                    "%s: car SoC %s%% < %s%% while plugged in — forcing grid charge",
                    name, app.soc, app.force_charge_below_soc,
                )
            else:
                _LOGGER.info(
                    "%s: car SoC %s%% >= %s%% — force charge released, back to mode=%s",
                    name, app.soc, app.force_charge_below_soc, app.mode,
                )
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
            # Solar is used freely; the grid portion is bounded by the
            # tighter ceiling (see _ceiling) — the projected one keeps the
            # quarter under the High limit by the shed margin.
            grid_headroom = max(_ceiling(), 0.0)
            forced_budget = remaining + grid_headroom
        else:
            forced_budget = None

        # Shed for peak earlier this quarter: stay off until the next one. A
        # restart would meet the same tight projection and be shed again a
        # few cycles later — that is the on/off sawing this exists to stop.
        if this_quarter is not None and state.peak_shed_quarter is not None:
            if state.peak_shed_quarter != this_quarter:
                state.peak_shed_quarter = None
            elif not state.is_on:
                _LOGGER.debug(
                    "  - %s: OFF, shed for peak this quarter; locked out until the next quarter",
                    name,
                )
                continue

        # Hard peak cap: if the appliance can't reach its minimum from the budget
        # it may use, it can't run. An OFF appliance is blocked at once. A
        # RUNNING one is shed only after PEAK_GATE_SHED_CYCLES consecutive short
        # cycles (debounce): its own draw comes back into the budget from a
        # power sensor that lags the grid meter, so the first cycle after a
        # start sees the full import but only part of the add-back and the
        # budget reads short for one cycle. Falling through in the grace cycles
        # throttles it to min_amps (the budget is below min, so `scaled_amps`
        # floors there) — a real overload is curbed immediately, only the
        # switch-off waits. When the shed does happen the state machine is kept
        # consistent: the transition is recorded so re-activation is damped by
        # the on/off hysteresis (no 10s relay flapping when headroom hovers
        # around min), and the setpoint tracking is cleared so it comes back
        # with a fresh write instead of the stale value.
        #
        # The budget a solar mode may use is the SIGNED surplus plus the grid
        # headroom, not the headroom alone: the demand ceiling is a grid-import
        # allowance (the same reasoning the deduction below applies), so
        # surplus-funded draw must not be gated on it. Without the solar term a
        # sunny appliance was switched off whenever the quarter-hour headroom
        # fell under min_amps, despite importing nothing. The headroom still
        # matters here because a solar mode DOES import when the surplus is
        # below min_amps — `scaled_amps` floors the setpoint at min_a — and that
        # top-up is what this gate bounds.
        #
        # SIGNED, not clamped at 0: a negative `remaining` is what the house is
        # already importing (the add-back has restored the appliance's own
        # draw, so it is base load only). The ceiling is the High limit as a
        # fixed grid ceiling — see the coordinator's `_get_peak_amps` — and
        # that is only a ceiling if the current import counts against it.
        # Clamping it away let an `off_peak` charger start into a 3.9 kW base
        # load on a 3.6 kW High limit (2026-10-18 09:31): the gate read the
        # full 15.6 A of ceiling, the critical backstop fired 7 minutes later,
        # and the quarter set a new monthly peak. The forced path has always
        # netted the import out (`forced_budget = remaining + headroom`); this
        # makes the two paths agree.
        if peak_remaining is not None:
            reachable = forced_budget if forced_additive else remaining + _ceiling()
            if reachable >= min_a:
                state.peak_gate_low_cycles = 0
            elif state.is_on and state.peak_gate_low_cycles + 1 < PEAK_GATE_SHED_CYCLES:
                state.peak_gate_low_cycles += 1
                peak_limited.append(app.uid)
                _LOGGER.info(
                    "%s: budget too low (%.1f A solar+grid available < %d A min) "
                    "for %d/%d cycles; throttling to min, not shedding yet",
                    name, reachable, min_a, state.peak_gate_low_cycles, PEAK_GATE_SHED_CYCLES,
                )
            else:
                _LOGGER.debug(
                    "  - %s (mode=%s): OFF, budget too low (%.1f A solar+grid available < %d A min)",
                    name, mode, reachable, min_a,
                )
                if state.is_on:
                    state.is_on = False
                    state.last_change = now
                    peak_limited.append(app.uid)
                    state.peak_shed_quarter = this_quarter
                    _LOGGER.info(
                        "%s: shed for the quarter-hour peak (%.1f A available < %d A min)%s",
                        name, reachable, min_a,
                        "; locked out until the next quarter" if this_quarter is not None else "",
                    )
                state.forget_setpoint()
                continue
        else:
            state.peak_gate_low_cycles = 0
        if _should_run(app, state, remaining, excess_amps, energy, now, mode):
            active.append(app.uid)
            active_names.append(name)

            if forced_additive:
                scaled = scaled_amps(min_a, app.max_amps, forced_budget)
            else:
                scaled = scaled_amps(min_a, app.max_amps, remaining)
                if peak_remaining is not None:
                    # Same signed surplus-plus-grid bound as the gate above — NOT
                    # the grid headroom alone, which would throttle a setpoint
                    # the sun is paying for (a 20 A surplus chopped to a 15 A
                    # demand ceiling while the meter reads zero import).
                    #
                    # With today's `scaled_amps` this is an invariant rather than
                    # an active throttle (`scaled` is already at most
                    # `max(remaining, min_a)`, and the gate guarantees
                    # `remaining + peak_remaining >= min_a` here), so it is kept
                    # as a guard on the budget plumbing, not as the mechanism
                    # that enforces the demand limit — the gate above and
                    # `critical_shed` do that.
                    scaled = max(min(scaled, remaining + _ceiling()), min_a)

            # Grid reading frozen (stale source): never let a running appliance's
            # setpoint climb. The surplus add-back would otherwise feed a runaway
            # (add-back = setpoint → higher budget → higher setpoint → …) when the
            # grid can't confirm the extra draw — the same loop the power-sensor
            # hold guards against. Ramp-DOWN is still allowed. The same clamp
            # serves the post-cut hold: after a grid-ceiling cut (below) the
            # load that caused it is usually still there, so the setpoint is
            # not chased back up for PEAK_CUT_HOLD_SECONDS.
            in_cut_hold = state.peak_cut_at is not None and now < state.peak_cut_at + _PEAK_CUT_HOLD
            if (hold_ramp or in_cut_hold) and state.last_fixed_amps is not None:
                scaled = min(scaled, state.last_fixed_amps)

            # Command the setpoint if a fixed_amps entity is configured.
            commanded = None
            if app.has_fixed_amps:
                decision = fixed_amps.decide(
                    scaled, min_a, app.max_amps,
                    state.last_fixed_amps, state.last_fixed_amps_change,
                    now, app.fixed_amps_hold_seconds, app.fixed_amps_value,
                    # The appliance's own step, with the deadband kept at half a
                    # step: the same relationship the defaults have (0.25 of
                    # 0.5), so a finer-grained appliance actually tracks finely
                    # instead of sitting in a band wider than its own step.
                    deadband=app.fixed_amps_step / 2,
                    step=app.fixed_amps_step,
                    # Setpoint anti-flap with the demand feature on: ramp up
                    # in bounded steps (a one-cycle budget glitch then moves
                    # the setpoint by at most this much).
                    max_step_up=FIXED_AMPS_MAX_STEP_UP if peak_remaining is not None else None,
                )
                # A grid-ceiling cut: the setpoint came from the grid budget
                # — neither floored at min_amps nor capped at max_amps — and
                # went down, so a base-load step or the quarter tightening
                # pushed it. That opens the post-cut hold above. A grace-cycle
                # throttle is floored at min and so never counts (the stale
                # add-back after a start must recover at once); a solar-mode
                # setpoint follows the sun, not the ceiling, and never counts.
                ceiling_bound = forced_additive and min_a < forced_budget < app.max_amps
                wrote_down = (
                    decision.write_value is not None
                    and state.last_fixed_amps is not None
                    and decision.write_value < state.last_fixed_amps
                )
                if ceiling_bound and wrote_down:
                    state.peak_cut_at = now
                    peak_limited.append(app.uid)
                    _LOGGER.info(
                        "%s: setpoint cut %.1f -> %.1f A for the grid ceiling; "
                        "no ramp-up for %ds",
                        name, state.last_fixed_amps, decision.write_value,
                        PEAK_CUT_HOLD_SECONDS,
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
            # A forced appliance's draw is solar first, grid for the rest; a
            # solar mode's draw comes off the surplus in full (its min-floor
            # top-up is the grid part). Peak headroom is GRID-import allowance
            # (the demand math works on grid offtake): only the part of the
            # draw beyond the available solar consumes it. Charging
            # solar-covered draw against it would falsely starve downstream
            # (forced) appliances of grid headroom that is actually untouched.
            solar_used = min(used, solar_avail)
            grid_used = max(used - solar_used, 0.0)
            remaining -= solar_used if forced_additive else used
            if peak_remaining is not None:
                peak_remaining -= grid_used
                if proj_remaining is not None:
                    proj_remaining -= grid_used
        else:
            _LOGGER.debug("  - %s (mode=%s): OFF, run condition not met", name, mode)
            state.forget_setpoint()

    # One pass over ALL appliances (unmanaged and peak-gated ones included) so an
    # appliance that stops being active always resets its not-consuming timer.
    active_set = set(active)
    flagged = [
        app.uid for app in appliances
        if not_consuming(app, states.setdefault(app.uid, ApplianceState()),
                         app.uid in active_set, now)
    ]
    if flagged:
        _LOGGER.debug("switched on but not consuming: %s", flagged)

    _LOGGER.debug(
        "optimize done: active=%s | leftover excess=%.2f A | leftover peak=%s",
        active_names, remaining,
        "off" if peak_remaining is None else f"{peak_remaining:.2f} A",
    )
    return OptimizeResult(
        active=active, decided_amps=decided_amps, fixed_amps_commands=commands,
        forced=forced, not_consuming=flagged, peak_limited=peak_limited,
    )


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

    An appliance switched off is locked out of restarting for the rest of
    the current quarter (see ``ApplianceState.peak_shed_quarter``); a
    throttled one gets the post-cut ramp-up hold.
    """
    this_quarter = quarter_id(now)
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
            state.peak_cut_at = now
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
            state.forget_setpoint()
            state.peak_shed_quarter = this_quarter
            _LOGGER.warning(
                "demand CRITICAL: %s shed (off)%s",
                name, "" if app.has_fixed_amps else " (no adjustable setpoint)",
            )
    return ShedResult(kept=kept, kept_amps=kept_amps, fixed_amps_commands=commands)
