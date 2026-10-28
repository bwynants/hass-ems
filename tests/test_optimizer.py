"""Tests for ems_core.optimizer — allocation, throttling, hysteresis, shed."""
import logging
from datetime import datetime, timedelta

from ems_core.optimizer import (
    optimize, critical_shed, record_excess, force_charge_active, effective_mode,
    not_consuming,
)
from ems_core.const import (
    NOT_CONSUMING_GRACE_SECONDS, SOC_FULL_PERCENT, PEAK_GATE_SHED_CYCLES,
)
from ems_core.models import (
    ApplianceInput, ApplianceState, EnergyInput, SetFixedAmps, OptimizeResult,
)
from ems_core.const import (
    MODE_FORCED, MODE_PARTIAL_SOLAR, MODE_FULL_SOLAR, MODE_OFF_PEAK, MODE_UNMANAGED,
)

T0 = datetime(2026, 7, 1, 8, 0, 0)


def _app(**kw):
    base = dict(
        name="a", priority=100, min_amps=6, max_amps=16, mode=MODE_PARTIAL_SOLAR,
        has_fixed_amps=False, fixed_amps_value=None, fixed_amps_hold_seconds=120,
        partial_min_excess_amps=4.0, hysteresis_seconds=120, averaging_window_seconds=180,
        connected=True, entity_on=False, measured_amps=None,
    )
    base.update(kw)
    base.setdefault("uid", base["name"])
    return ApplianceInput(**base)


def _energy(**kw):
    base = dict(
        grid_w=None, solar_w=0.0, house_load_w=0.0, voltage=230, off_peak=False,
        cost_peak=0.40, cost_offpeak=0.20, gain_peak=0.10, gain_offpeak=0.10,
    )
    base.update(kw)
    return EnergyInput(**base)


def test_forced_additive_budget_uses_solar_on_top_of_grid():
    # forced appliance, solar surplus 4A + peak headroom 16A => can scale high.
    app = _app(mode=MODE_FORCED, has_fixed_amps=True, fixed_amps_value=6, max_amps=16)
    states = {}
    res = optimize([app], states, excess_amps=4.0, peak_amps=16.0,
                   energy=_energy(), now=T0)
    assert res.active == ["a"]
    # forced_budget = remaining(4) + peak(16) = 20 -> scaled clamps to max 16 ->
    # first fixed_amps write rounds 16 to 16.
    assert res.fixed_amps_commands[0].value == 16.0


def test_solar_funded_draw_not_capped_by_peak_headroom():
    # partial_solar with a big surplus and a tiny peak headroom: the demand
    # ceiling is a GRID-import allowance, so surplus-funded draw must scale past
    # it (the meter reads export — nothing is being imported to limit).
    app = _app(mode=MODE_PARTIAL_SOLAR, has_fixed_amps=True, fixed_amps_value=6,
               min_amps=6, max_amps=32, partial_min_excess_amps=0)
    states = {}
    res = optimize([app], states, excess_amps=16.0, peak_amps=8.0,
                   energy=_energy(), now=T0)
    assert res.active == ["a"]
    # scaled = 16 (surplus, under max 32) -> well above the 8 A headroom; first
    # write quantises down-with-margin (0.5 A step, 0.25 A deadband) to 15.5.
    assert res.fixed_amps_commands[0].value == 15.5


def test_solar_mode_runs_when_grid_headroom_below_min():
    # Headroom 3 A < min 6 A, but 16 A of surplus covers the appliance outright.
    # Pre-fix this was switched off on the headroom alone.
    app = _app(mode=MODE_PARTIAL_SOLAR, min_amps=6, partial_min_excess_amps=0)
    states = {}
    res = optimize([app], states, excess_amps=16.0, peak_amps=3.0,
                   energy=_energy(), now=T0)
    assert res.active == ["a"]


def test_peak_headroom_sheds_when_below_min():
    # No surplus, so the whole draw would come off the grid: headroom 3 A <
    # min 6 A => the appliance cannot run.
    app = _app(mode=MODE_PARTIAL_SOLAR, min_amps=6, partial_min_excess_amps=0)
    states = {}
    res = optimize([app], states, excess_amps=0.0, peak_amps=3.0,
                   energy=_energy(), now=T0)
    assert res.active == []


def test_min_amps_floor_grid_topup_bounded_by_headroom():
    # Surplus (0.2 A) below min_amps: `scaled_amps` floors the setpoint at min,
    # so the appliance really does import the 5.8 A difference. The gate bounds
    # exactly that top-up — solar 0.2 + headroom 5.9 = 6.1 >= min 6, so it runs.
    app = _app(mode=MODE_PARTIAL_SOLAR, min_amps=6, partial_min_excess_amps=0)
    res = optimize([app], {}, excess_amps=0.2, peak_amps=5.9,
                   energy=_energy(), now=T0)
    assert res.active == ["a"]
    # Same surplus, less headroom: 0.2 + 5.0 = 5.2 < 6 -> the grid can't fund
    # the floor, so it stays off.
    res = optimize([app], {}, excess_amps=0.2, peak_amps=5.0,
                   energy=_energy(), now=T0)
    assert res.active == []


def test_priority_order_high_priority_claims_headroom_first():
    # No solar (excess 0) so the winner consumes GRID headroom; headroom only
    # covers one appliance at min (6A) — the lower priority number wins.
    a1 = _app(name="hi", priority=1, min_amps=6, mode=MODE_PARTIAL_SOLAR, partial_min_excess_amps=0)
    a2 = _app(name="lo", priority=50, min_amps=6, mode=MODE_PARTIAL_SOLAR, partial_min_excess_amps=0)
    states = {}
    res = optimize([a2, a1], states, excess_amps=0.0, peak_amps=6.0,
                   energy=_energy(), now=T0)
    assert res.active == ["hi"]


def test_full_solar_off_when_not_covered():
    app = _app(mode=MODE_FULL_SOLAR, min_amps=6, partial_min_excess_amps=0)
    states = {}
    res = optimize([app], states, excess_amps=3.0, peak_amps=None,
                   energy=_energy(), now=T0)
    assert res.active == []


def test_full_solar_on_when_covered():
    app = _app(mode=MODE_FULL_SOLAR, min_amps=6)
    states = {}
    res = optimize([app], states, excess_amps=8.0, peak_amps=None,
                   energy=_energy(), now=T0)
    assert res.active == ["a"]


def test_full_solar_off_on_transient_spike(caplog):
    # Current budget covers (10 >= 6) but the sustained average does not (2 < 6)
    # -> full_solar must NOT commit (would import once the spike passes).
    app = _app(mode=MODE_FULL_SOLAR, min_amps=6, averaging_window_seconds=180)
    st = ApplianceState()
    st.excess_history.extend([2.0, 2.0, 2.0])
    states = {"a": st}
    with caplog.at_level(logging.INFO, logger="ems_core.optimizer"):
        res = optimize([app], states, excess_amps=10.0, peak_amps=None,
                       energy=_energy(), now=T0)
    assert res.active == []

    def spike_infos():
        return [
            r for r in caplog.records
            if r.levelno == logging.INFO and "transient spike" in r.getMessage()
        ]

    # The transient-spike reason is surfaced at INFO once, on entry...
    assert len(spike_infos()) == 1
    # ...and repeats of the same condition don't log INFO again (edge-triggered).
    with caplog.at_level(logging.INFO, logger="ems_core.optimizer"):
        res2 = optimize([app], states, excess_amps=10.0, peak_amps=None,
                        energy=_energy(),
                        now=T0 + timedelta(seconds=10))
    assert res2.active == []
    assert len(spike_infos()) == 1


def test_full_solar_on_when_sustained_and_current():
    # Both the sustained average and the current budget cover -> runs.
    app = _app(mode=MODE_FULL_SOLAR, min_amps=6, averaging_window_seconds=180)
    st = ApplianceState()
    st.excess_history.extend([8.0, 8.0, 8.0])
    states = {"a": st}
    res = optimize([app], states, excess_amps=8.0, peak_amps=None,
                   energy=_energy(), now=T0)
    assert res.active == ["a"]


def test_full_solar_running_survives_short_drop_on_average():
    # A long-running full_solar appliance (hysteresis window already expired)
    # rides through a single-cycle solar drop because the sustained average
    # still covers it — the instantaneous budget alone would drop it too soon.
    app = _app(mode=MODE_FULL_SOLAR, min_amps=6, hysteresis_seconds=120,
               averaging_window_seconds=180)
    st = ApplianceState()
    st.is_on = True
    st.last_change = T0 - timedelta(seconds=3600)  # running for an hour
    st.excess_history.extend([10.0] * 18)          # healthy sustained solar
    states = {"a": st}
    # One cycle at 0 A: avg = (17*10 + 0)/18 ~= 9.4 A >= 6 -> stays on.
    res = optimize([app], states, excess_amps=0.0, peak_amps=None,
                   energy=_energy(), now=T0)
    assert res.active == ["a"]


def test_full_solar_running_turns_off_when_average_falls():
    # The asymmetric hold is not a latch: a *sustained* drop that pulls the
    # rolling average below min still turns a running appliance off (past the
    # hysteresis window).
    app = _app(mode=MODE_FULL_SOLAR, min_amps=6, hysteresis_seconds=120,
               averaging_window_seconds=180)
    st = ApplianceState()
    st.is_on = True
    st.last_change = T0 - timedelta(seconds=3600)
    st.excess_history.extend([2.0] * 18)  # average well below min
    states = {"a": st}
    res = optimize([app], states, excess_amps=2.0, peak_amps=None,
                   energy=_energy(), now=T0)
    assert res.active == []


def test_full_solar_start_still_requires_current_and_average():
    # Starting (currently off) is unchanged: the sustained average alone is not
    # enough — the current budget must cover too, or a spike would commit it.
    app = _app(mode=MODE_FULL_SOLAR, min_amps=6, averaging_window_seconds=180)
    st = ApplianceState()
    st.is_on = False
    st.excess_history.extend([10.0] * 18)  # avg healthy...
    states = {"a": st}
    # ...but the current budget is below min -> must NOT start.
    res = optimize([app], states, excess_amps=3.0, peak_amps=None,
                   energy=_energy(), now=T0)
    assert res.active == []


def test_off_peak_runs_free_during_off_peak_hours():
    # Baseline unchanged: off_peak mode runs freely in the off-peak window even
    # while importing (excess negative).
    app = _app(mode=MODE_OFF_PEAK, min_amps=6)
    states = {}
    res = optimize([app], states, excess_amps=-10.0, peak_amps=None,
                   energy=_energy(off_peak=True), now=T0)
    assert res.active == ["a"]


def test_off_peak_gated_by_breakeven_when_peak_dearer():
    # Peak hours, peak dearer than off-peak (default 0.40 > 0.20), importing ->
    # the solar breakeven gate keeps it OFF.
    app = _app(mode=MODE_OFF_PEAK, min_amps=6)
    states = {}
    res = optimize([app], states, excess_amps=-10.0, peak_amps=None,
                   energy=_energy(off_peak=False), now=T0)
    assert res.active == []


def test_off_peak_breakeven_uses_peak_period_export_gain():
    # Peak hours: the solar the appliance would self-consume is otherwise
    # exported at the PEAK export price, so only gain_peak enters the gate.
    # 6 A min + 0.5 A start margin = 6.5 A; breakeven = 6.5*(0.40-0.20)/(0.40-0.10)
    # = 4.33 A. An off-peak export price near the peak import price would
    # saturate the gate to +inf if it were (wrongly) used — it must not be.
    app = _app(mode=MODE_OFF_PEAK, min_amps=6)
    res = optimize([app], {}, excess_amps=5.0, peak_amps=None,
                   energy=_energy(off_peak=False, gain_peak=0.10, gain_offpeak=0.39), now=T0)
    assert res.active == ["a"]
    # Conversely a dear peak export price gates it even though gain_offpeak is low.
    res = optimize([app], {}, excess_amps=5.0, peak_amps=None,
                   energy=_energy(off_peak=False, gain_peak=0.39, gain_offpeak=0.10), now=T0)
    assert res.active == []


def test_energy_input_gain_now_follows_tariff_period():
    e = _energy(off_peak=False, gain_peak=0.06, gain_offpeak=0.04)
    assert e.gain_now == 0.06
    e = _energy(off_peak=True, gain_peak=0.06, gain_offpeak=0.04)
    assert e.gain_now == 0.04


def test_off_peak_runs_free_when_peak_cheaper_than_off_peak():
    # Dynamic tariff inversion: during PEAK hours but peak price BELOW off-peak
    # price -> peak is the cheap period, so run freely even while importing
    # (this is the case the old breakeven-only gate would drop under heavy import).
    app = _app(mode=MODE_OFF_PEAK, min_amps=6)
    states = {}
    res = optimize([app], states, excess_amps=-10.0, peak_amps=None,
                   energy=_energy(off_peak=False, cost_peak=0.15, cost_offpeak=0.20),
                   now=T0)
    assert res.active == ["a"]


def test_off_peak_free_run_still_respects_peak_headroom():
    # Running "freely" is only about eligibility — the quarter-hour demand cap
    # still sheds it when headroom is below min.
    app = _app(mode=MODE_OFF_PEAK, min_amps=6)
    states = {}
    res = optimize([app], states, excess_amps=0.0, peak_amps=3.0,
                   energy=_energy(off_peak=False, cost_peak=0.15, cost_offpeak=0.20),
                   now=T0)
    assert res.active == []


def test_importing_above_ceiling_blocks_start_in_solar_mode():
    # 2026-10-18 09:31: Saturday (off-peak), hob on, house importing 3.9 kW
    # (-17 A) against a 3.6 kW High limit (15.65 A). off_peak "runs free" on
    # eligibility, but the gate must net the import out: -17 + 15.65 < 6 A min.
    # Pre-fix the import was clamped away and the charger started.
    app = _app(mode=MODE_OFF_PEAK, min_amps=6, max_amps=20)
    states = {}
    res = optimize([app], states, excess_amps=-17.0, peak_amps=15.65,
                   energy=_energy(off_peak=True, solar_w=380.0), now=T0)
    assert res.active == []
    assert not states["a"].is_on


def test_importing_below_ceiling_still_allows_grid_topup():
    # Modest import (-2 A) with 15.65 A ceiling: -2 + 15.65 = 13.65 >= 6, so the
    # headroom funds the min_amps floor and an eligible (off-peak, run-free)
    # appliance runs at min rather than being blocked by the import alone.
    app = _app(mode=MODE_OFF_PEAK, min_amps=6, max_amps=20,
               has_fixed_amps=True, fixed_amps_value=6.0)
    res = optimize([app], {}, excess_amps=-2.0, peak_amps=15.65,
                   energy=_energy(off_peak=True, solar_w=380.0), now=T0)
    assert res.active == ["a"]
    assert res.fixed_amps_commands[0].value == 6.0


def test_running_solar_mode_shed_after_debounce_when_import_exceeds_ceiling():
    # A running off_peak charger: its own 6 A is added back, so excess reads the
    # base-load import only (-17 A). Budget -17 + 15.65 < 6 -> throttled for
    # PEAK_GATE_SHED_CYCLES - 1 cycles, then shed on the last one.
    app = _app(mode=MODE_OFF_PEAK, min_amps=6, max_amps=20, entity_on=True,
               measured_amps=6.0)
    st = ApplianceState()
    st.is_on = True
    st.last_change = T0 - timedelta(seconds=3600)
    states = {"a": st}
    for i in range(PEAK_GATE_SHED_CYCLES - 1):
        res = optimize([app], states, excess_amps=-17.0, peak_amps=15.65,
                       energy=_energy(off_peak=True, solar_w=380.0),
                       now=T0 + timedelta(seconds=10 * i))
        assert res.active == ["a"], f"cycle {i} should still be throttled, not shed"
    res = optimize([app], states, excess_amps=-17.0, peak_amps=15.65,
                   energy=_energy(off_peak=True, solar_w=380.0),
                   now=T0 + timedelta(seconds=10 * (PEAK_GATE_SHED_CYCLES - 1)))
    assert res.active == []
    assert not states["a"].is_on


def test_off_peak_night_scales_like_forced():
    # off_peak mode, off-peak hours, sundown (no solar): scales up on cheap grid
    # like forced (additive budget, up to max within the peak limit), not min.
    app = _app(mode=MODE_OFF_PEAK, min_amps=6, max_amps=16,
               has_fixed_amps=True, fixed_amps_value=6.0)
    states = {}
    res = optimize([app], states, excess_amps=0.0, peak_amps=20.0,
                   energy=_energy(off_peak=True, solar_w=0.0), now=T0)
    assert res.active == ["a"]
    # forced_budget = 0 + (20 - 2 reserve) = 18 -> scaled to max 16.
    assert res.fixed_amps_commands[0].value == 16.0


def test_off_peak_day_does_not_grid_scale():
    # Off-peak but with solar (daytime): NOT sundown -> solar-following, so with
    # no surplus it runs at min, not scaled up on grid.
    app = _app(mode=MODE_OFF_PEAK, min_amps=6, max_amps=16,
               has_fixed_amps=True, fixed_amps_value=6.0)
    states = {}
    res = optimize([app], states, excess_amps=0.0, peak_amps=20.0,
                   energy=_energy(off_peak=True, solar_w=3000.0), now=T0)
    assert res.active == ["a"]
    assert res.fixed_amps_commands[0].value == 6.0


def test_off_peak_peak_hours_no_grid_scale_even_at_sundown():
    # Sundown but PEAK hours (peak cheaper so it still runs): the forced-like
    # grid scaling is gated on off-peak hours -> runs at min, not scaled up.
    app = _app(mode=MODE_OFF_PEAK, min_amps=6, max_amps=16,
               has_fixed_amps=True, fixed_amps_value=6.0)
    states = {}
    res = optimize([app], states, excess_amps=0.0, peak_amps=20.0,
                   energy=_energy(off_peak=False, solar_w=0.0,
                                  cost_peak=0.15, cost_offpeak=0.20),
                   now=T0)
    assert res.active == ["a"]
    assert res.fixed_amps_commands[0].value == 6.0


def test_hold_ramp_prevents_setpoint_climb():
    # hold_ramp (grid frozen): a running appliance's setpoint must not climb,
    # even with a big budget — this is the guard against the surplus-add-back
    # runaway when the grid can't confirm the extra draw.
    def run(hold):
        app = _app(mode=MODE_FORCED, min_amps=6, max_amps=16,
                   has_fixed_amps=True, fixed_amps_value=10.0, fixed_amps_hold_seconds=0)
        st = ApplianceState()
        st.is_on = True
        st.last_change = T0 - timedelta(seconds=3600)
        st.last_fixed_amps = 10.0
        st.last_fixed_amps_change = T0 - timedelta(seconds=3600)
        return optimize([app], {"a": st}, excess_amps=0.0, peak_amps=20.0,
                        energy=_energy(), now=T0, hold_ramp=hold)
    # Without the guard the big budget ramps the setpoint above 10 A.
    up = run(False)
    assert up.fixed_amps_commands and up.fixed_amps_commands[0].value > 10.0
    # With the guard it's clamped to the held 10 A — no command above it.
    held = run(True)
    assert all(c.value <= 10.0 for c in held.fixed_amps_commands)


def test_hold_ramp_still_allows_ramp_down():
    # The guard caps the top, not the bottom: a shrinking budget still throttles.
    app = _app(mode=MODE_FORCED, min_amps=6, max_amps=16,
               has_fixed_amps=True, fixed_amps_value=12.0, fixed_amps_hold_seconds=0)
    st = ApplianceState()
    st.is_on = True
    st.last_change = T0 - timedelta(seconds=3600)
    st.last_fixed_amps = 12.0
    st.last_fixed_amps_change = T0 - timedelta(seconds=3600)
    res = optimize([app], {"a": st}, excess_amps=0.0, peak_amps=8.0,
                   energy=_energy(), now=T0, hold_ramp=True)
    assert res.active == ["a"]
    assert res.fixed_amps_commands[0].value < 12.0


def test_setpoint_cleared_on_turn_off():
    # After a turn-off, re-activation must write a fresh setpoint immediately
    # instead of holding the stale high value through the hold window.
    app = _app(mode=MODE_FULL_SOLAR, min_amps=6, max_amps=16,
               has_fixed_amps=True, fixed_amps_value=16.0,
               hysteresis_seconds=0, fixed_amps_hold_seconds=120)
    states = {}
    r1 = optimize([app], states, excess_amps=20.0, peak_amps=None,
                  energy=_energy(), now=T0)
    assert r1.fixed_amps_commands == [SetFixedAmps("a", 16.0)]
    # Surplus gone -> off; setpoint tracking must be cleared.
    r2 = optimize([app], states, excess_amps=0.0, peak_amps=None,
                  energy=_energy(), now=T0 + timedelta(seconds=10))
    assert r2.active == []
    assert states["a"].last_fixed_amps is None
    # Modest surplus re-activates within the old hold window: fresh write at 6,
    # not a silent hold at the stale 16.
    r3 = optimize([app], states, excess_amps=6.5, peak_amps=None,
                  energy=_energy(), now=T0 + timedelta(seconds=20))
    assert r3.active == ["a"]
    assert r3.fixed_amps_commands == [SetFixedAmps("a", 6.0)]


def test_on_off_hysteresis_holds_recent_state():
    # full_solar: a running appliance stays on while its rolling average covers
    # it, so here the excess drops straight to 0 and the average follows down —
    # the switch-off is gated only by the on/off hysteresis window.
    app = _app(mode=MODE_FULL_SOLAR, min_amps=6, hysteresis_seconds=120)
    # Cycle 1: turns on at T0
    states = {}
    optimize([app], states, excess_amps=10.0, peak_amps=None,
             energy=_energy(), now=T0)
    assert states["a"].is_on is True
    # Cycle 2: 30s later, condition now false, but within hysteresis -> stays on
    res = optimize([app], states, excess_amps=0.0, peak_amps=None,
                   energy=_energy(), now=T0 + timedelta(seconds=30))
    assert res.active == ["a"]
    # Cycle 3: 200s later, condition still false, past hysteresis -> turns off
    res2 = optimize([app], states, excess_amps=0.0, peak_amps=None,
                    energy=_energy(), now=T0 + timedelta(seconds=230))
    assert res2.active == []


def test_start_needs_min_amps_plus_on_off_margin():
    # On/off deadband: an OFF appliance starts only once min_amps + 0.5 A is
    # available (partial threshold out of reach, so only the min-amps gate decides).
    app = _app(mode=MODE_PARTIAL_SOLAR, min_amps=6, partial_min_excess_amps=100_000)
    states = {}
    res = optimize([app], states, excess_amps=6.3, peak_amps=None,
                   energy=_energy(), now=T0)
    assert res.active == []
    res = optimize([app], states, excess_amps=6.5, peak_amps=None,
                   energy=_energy(), now=T0 + timedelta(seconds=10))
    assert res.active == ["a"]


def test_running_appliance_stays_on_down_to_min_amps():
    # ...but once ON it holds down to min_amps itself, even after the time
    # hysteresis window has expired — the 0.5 A gap is the hysteresis.
    app = _app(mode=MODE_PARTIAL_SOLAR, min_amps=6, partial_min_excess_amps=100_000,
               hysteresis_seconds=0)
    states = {}
    optimize([app], states, excess_amps=8.0, peak_amps=None, energy=_energy(), now=T0)
    assert states["a"].is_on is True
    res = optimize([app], states, excess_amps=6.2, peak_amps=None,
                   energy=_energy(), now=T0 + timedelta(seconds=10))
    assert res.active == ["a"]
    res = optimize([app], states, excess_amps=5.9, peak_amps=None,
                   energy=_energy(), now=T0 + timedelta(seconds=20))
    assert res.active == []


def test_on_off_margin_is_independent_of_the_setpoint_step():
    # A 0.1 A charger tracks its setpoint in 0.1 A steps, but the start margin
    # is still the fixed 0.5 A, not half its step.
    app = _app(mode=MODE_PARTIAL_SOLAR, min_amps=6, partial_min_excess_amps=100_000,
               has_fixed_amps=True, fixed_amps_value=6, fixed_amps_step=0.1)
    states = {}
    res = optimize([app], states, excess_amps=6.4, peak_amps=None,
                   energy=_energy(), now=T0)
    assert res.active == []
    res = optimize([app], states, excess_amps=6.55, peak_amps=None,
                   energy=_energy(), now=T0 + timedelta(seconds=10))
    assert res.active == ["a"]
    assert res.fixed_amps_commands == [SetFixedAmps("a", 6.5)]


def test_activation_ignores_zero_measured():
    # An OFF appliance's power sensor reads 0.0 — the activation cycle must
    # budget the commanded/min draw, not the meaningless measured 0, or the
    # same surplus gets allocated twice.
    a1 = _app(name="a1", uid="a1", mode=MODE_FULL_SOLAR, min_amps=6,
              measured_amps=0.0, entity_on=False)
    a2 = _app(name="a2", uid="a2", mode=MODE_FULL_SOLAR, min_amps=6,
              measured_amps=0.0, entity_on=False, priority=200)
    states = {}
    res = optimize([a1, a2], states, excess_amps=8.0, peak_amps=None,
                   energy=_energy(), now=T0)
    assert res.active == ["a1"]
    assert res.decided_amps == 6


def test_running_appliance_uses_measured():
    # While actually running, the measured draw takes precedence for budgeting.
    app = _app(mode=MODE_FULL_SOLAR, min_amps=6, measured_amps=9.0, entity_on=True)
    states = {}
    res = optimize([app], states, excess_amps=10.0, peak_amps=None,
                   energy=_energy(), now=T0)
    assert res.active == ["a"]
    assert res.decided_amps == 9.0


def test_peak_cap_off_goes_through_state_machine():
    # A peak-cap hard-off must record the transition (so re-activation is
    # hysteresis-damped, no 10s relay flapping) and clear the setpoint
    # tracking (so it comes back with a fresh write).
    app = _app(mode=MODE_FORCED, min_amps=6, max_amps=16,
               has_fixed_amps=True, fixed_amps_value=16.0, hysteresis_seconds=60)
    states = {}
    r1 = optimize([app], states, excess_amps=0.0, peak_amps=20.0,
                  energy=_energy(), now=T0)
    assert r1.active == ["a"]
    assert r1.fixed_amps_commands == [SetFixedAmps("a", 16.0)]
    # Headroom collapses below min. The running appliance is NOT shed on the
    # first short cycles (debounce); the setpoint write to min is held here by
    # the 120 s fixed_amps window, see the throttle test below.
    low = dict(excess_amps=0.0, peak_amps=3.0, energy=_energy())
    for i in range(1, PEAK_GATE_SHED_CYCLES):
        r = optimize([app], states, now=T0 + timedelta(seconds=10 * i), **low)
        assert r.active == ["a"], f"grace cycle {i} must keep it on"
        assert states["a"].is_on is True
        assert states["a"].peak_gate_low_cycles == i
    # The PEAK_GATE_SHED_CYCLES-th consecutive short cycle -> hard off, state
    # machine updated.
    t_off = T0 + timedelta(seconds=10 * PEAK_GATE_SHED_CYCLES)
    r2 = optimize([app], states, now=t_off, **low)
    assert r2.active == []
    assert states["a"].is_on is False
    assert states["a"].last_fixed_amps is None
    assert states["a"].peak_gate_low_cycles == 0
    # Headroom recovers within the hysteresis window -> stays off (damped).
    r3 = optimize([app], states, excess_amps=0.0, peak_amps=8.0,
                  energy=_energy(), now=t_off + timedelta(seconds=10))
    assert r3.active == []
    # Past the window -> on again with a FRESH setpoint write. Budget = 8 A peak
    # minus the 1 A forced reserve = 7 A -> written 6.5 A (not the stale 16).
    r4 = optimize([app], states, excess_amps=0.0, peak_amps=8.0,
                  energy=_energy(), now=t_off + timedelta(seconds=80))
    assert r4.active == ["a"]
    assert r4.fixed_amps_commands == [SetFixedAmps("a", 6.5)]


def test_peak_gate_grace_resets_when_budget_recovers():
    # The real-world case: a charger starts, the grid meter shows the full
    # import at once but the charger's power sensor (slower poll) still reports
    # the ramp, so the add-back is short and the budget reads below min for ONE
    # cycle. The running appliance must survive that cycle, and the counter
    # must reset once the budget covers min again — short cycles only shed
    # when consecutive.
    app = _app(mode=MODE_FORCED, min_amps=6, max_amps=16,
               has_fixed_amps=True, fixed_amps_value=12.0, hysteresis_seconds=60,
               fixed_amps_hold_seconds=0)
    states = {"a": ApplianceState(is_on=True, last_change=T0 - timedelta(hours=1),
                                  last_fixed_amps=12.0, last_fixed_amps_change=T0)}
    energy = _energy()
    # Stale add-back: budget -9 + (15.5 - 1) = 5.5 A < 6 A min. Kept on, but
    # throttled to min meanwhile (a real overload is curbed at once).
    r1 = optimize([app], states, excess_amps=-9.0, peak_amps=15.5,
                  energy=energy, now=T0)
    assert r1.active == ["a"]
    assert r1.fixed_amps_commands == [SetFixedAmps("a", 6.0)]
    assert states["a"].peak_gate_low_cycles == 1
    # Fresh add-back: budget -2.7 + 14.5 = 11.8 A -> fine, counter cleared and
    # the setpoint scales back up.
    r2 = optimize([app], states, excess_amps=-2.7, peak_amps=15.5,
                  energy=energy, now=T0 + timedelta(seconds=10))
    assert r2.active == ["a"]
    assert states["a"].peak_gate_low_cycles == 0
    assert r2.fixed_amps_commands and r2.fixed_amps_commands[0].value >= 11.5
    # Another isolated short cycle later starts counting from 1 again.
    r3 = optimize([app], states, excess_amps=-9.0, peak_amps=15.5,
                  energy=energy, now=T0 + timedelta(seconds=20))
    assert r3.active == ["a"]
    assert states["a"].peak_gate_low_cycles == 1


def test_peak_gate_blocks_start_without_grace():
    # Debounce applies to a RUNNING appliance only: one that is off must not be
    # started on a budget that can't reach min, not even for a cycle.
    app = _app(mode=MODE_FORCED, min_amps=6)
    states = {"a": ApplianceState(is_on=False, last_change=T0 - timedelta(hours=1))}
    res = optimize([app], states, excess_amps=0.0, peak_amps=3.0,
                   energy=_energy(), now=T0)
    assert res.active == []
    assert states["a"].peak_gate_low_cycles == 0


def test_solar_covered_draw_keeps_peak_headroom():
    # A non-forced appliance running fully on solar must not consume grid
    # peak headroom — a downstream forced appliance still gets the grid budget.
    solar_app = _app(name="s", uid="s", priority=1, mode=MODE_FULL_SOLAR, min_amps=6)
    forced_app = _app(name="f", uid="f", priority=50, mode=MODE_FORCED, min_amps=6)
    states = {}
    # 10 A solar, 6 A grid headroom: solar app uses 6 A of solar (headroom
    # untouched), forced app then runs on remaining 4 A solar + 6 A grid.
    res = optimize([solar_app, forced_app], states, excess_amps=10.0, peak_amps=6.0,
                   energy=_energy(), now=T0)
    assert res.active == ["s", "f"]


def test_forced_reserves_peak_headroom():
    # Forced leaves 1 A of grid headroom unused so it doesn't scale up to the
    # demand cap (which would shed it). With 10 A peak headroom and no solar it
    # targets 9 A (10 - 1), written as 8.5 A — not the 9.5 A it would take if it
    # consumed the whole headroom. ~1 A of peak headroom is left over.
    app = _app(mode=MODE_FORCED, min_amps=6, max_amps=16,
               has_fixed_amps=True, fixed_amps_value=6.0)
    states = {}
    res = optimize([app], states, excess_amps=0.0, peak_amps=10.0,
                   energy=_energy(), now=T0)
    assert res.active == ["a"]
    assert res.fixed_amps_commands[0].value == 8.5


def test_forced_additive_split_seen_by_second_appliance():
    # The forced solar/grid split must leave the correct budgets for the NEXT
    # appliance: forced draws 16 (10 solar + 6 grid of the 10 headroom), so a
    # second forced appliance sees 0 solar + 4 grid < min 6 -> capped off.
    f1 = _app(name="f1", uid="f1", priority=1, mode=MODE_FORCED, min_amps=6,
              max_amps=16, has_fixed_amps=True, fixed_amps_value=16.0)
    f2 = _app(name="f2", uid="f2", priority=50, mode=MODE_FORCED, min_amps=6)
    states = {}
    res = optimize([f1, f2], states, excess_amps=10.0, peak_amps=10.0,
                   energy=_energy(), now=T0)
    assert res.active == ["f1"]
    # With more headroom the second one fits again (0 solar + 10 grid >= 6).
    states2 = {}
    res2 = optimize([f1, f2], states2, excess_amps=10.0, peak_amps=16.0,
                    energy=_energy(), now=T0)
    assert res2.active == ["f1", "f2"]


def test_duplicate_names_commands_keyed_by_uid():
    # Two appliances with the SAME display name: the active list and the
    # fixed_amps commands must carry the uid of the one that actually won the
    # budget, so the shell can't misdirect a command.
    a1 = _app(uid="sub1", name="dup", priority=1, mode=MODE_FULL_SOLAR, min_amps=6,
              has_fixed_amps=True, fixed_amps_value=6.0)
    a2 = _app(uid="sub2", name="dup", priority=50, mode=MODE_FULL_SOLAR, min_amps=6)
    states = {}
    # 8 A covers one appliance's minimum but not both.
    res = optimize([a1, a2], states, excess_amps=8.0, peak_amps=None,
                   energy=_energy(), now=T0)
    assert res.active == ["sub1"]
    assert all(cmd.uid == "sub1" for cmd in res.fixed_amps_commands)


def test_state_keyed_by_uid_not_name():
    # Two appliances sharing a display name must not share runtime state.
    a1 = _app(uid="sub1", name="dup")
    a2 = _app(uid="sub2", name="dup")
    states = {}
    record_excess([a1, a2], states, 8.0)
    assert set(states) == {"sub1", "sub2"}
    # A rename (same uid, new name) keeps the accumulated state.
    renamed = _app(uid="sub1", name="renamed")
    record_excess([renamed], states, 9.0)
    assert set(states) == {"sub1", "sub2"}
    assert list(states["sub1"].excess_history) == [8.0, 9.0]


def test_unmanaged_left_alone():
    app = _app(mode=MODE_UNMANAGED, entity_on=True)
    states = {}
    res = optimize([app], states, excess_amps=10.0, peak_amps=None,
                   energy=_energy(), now=T0)
    assert res.active == []
    assert res.fixed_amps_commands == []


def test_critical_shed_throttles_then_off():
    # Above min with adjustable setpoint -> throttled to min & kept
    throttleable = _app(name="t", entity_on=True, has_fixed_amps=True,
                        fixed_amps_value=12, min_amps=6)
    # Already at min -> shed off
    atmin = _app(name="m", entity_on=True, has_fixed_amps=True,
                 fixed_amps_value=6, min_amps=6)
    # No adjustable setpoint -> off
    plain = _app(name="p", entity_on=True, has_fixed_amps=False, min_amps=6)
    states = {}
    res = critical_shed([throttleable, atmin, plain], states, now=T0)
    assert res.kept == ["t"]
    assert res.kept_amps == 6
    assert res.fixed_amps_commands == [SetFixedAmps("t", 6.0)]
    assert states["m"].is_on is False and states["p"].is_on is False


# ---- Startup state adoption -------------------------------------------------
# The shell seeds is_on / last_change / last_fixed_amps from the appliances'
# real state before the first cycle after a restart or reload. These cover what
# that seeding has to buy: a restart must not power-cycle a running appliance,
# while a genuine off decision must still get through.


def test_seeded_running_appliance_survives_a_bad_first_budget():
    app = _app(mode=MODE_PARTIAL_SOLAR, entity_on=True, hysteresis_seconds=120,
               partial_min_excess_amps=4.0)
    states = {"a": ApplianceState(is_on=True, last_change=T0)}
    res = optimize([app], states, excess_amps=0.0, peak_amps=None,
                   energy=_energy(), now=T0 + timedelta(seconds=10))
    assert res.active == ["a"]
    assert states["a"].is_on is True


def test_seeded_running_appliance_still_switches_off_after_the_window():
    app = _app(mode=MODE_PARTIAL_SOLAR, entity_on=True, hysteresis_seconds=120,
               partial_min_excess_amps=4.0)
    states = {"a": ApplianceState(is_on=True, last_change=T0)}
    res = optimize([app], states, excess_amps=0.0, peak_amps=None,
                   energy=_energy(), now=T0 + timedelta(seconds=121))
    assert res.active == []
    assert states["a"].is_on is False


def test_unseeded_running_appliance_is_dropped_immediately():
    # The regression this guards: with no runtime state the core starts from
    # is_on=False and last_change=None, so one blank budget switches a running
    # appliance off with nothing to damp it — and the recovery back on is
    # equally undamped. Hence the shell must seed before its first cycle.
    app = _app(mode=MODE_PARTIAL_SOLAR, entity_on=True, hysteresis_seconds=120,
               partial_min_excess_amps=4.0)
    states = {}
    res = optimize([app], states, excess_amps=0.0, peak_amps=None,
                   energy=_energy(), now=T0)
    assert res.active == []
    assert states["a"].last_change is None  # nothing arms the hysteresis window


def test_seeded_setpoint_is_not_rewritten_on_the_first_cycle():
    app = _app(mode=MODE_FORCED, entity_on=True, has_fixed_amps=True,
               fixed_amps_value=10.0, measured_amps=10.0, min_amps=6, max_amps=16)
    states = {"a": ApplianceState(is_on=True, last_change=T0,
                                  last_fixed_amps=10.0, last_fixed_amps_change=T0)}
    res = optimize([app], states, excess_amps=10.0, peak_amps=None,
                   energy=_energy(), now=T0 + timedelta(seconds=10))
    assert res.active == ["a"]
    assert res.fixed_amps_commands == []


def test_setpoint_uses_the_appliances_own_step():
    # A 0.1 A "ampere to use" entity: 7.85 A of budget is run at 7.8 (quantised
    # down by the 0.05 A half-step deadband), keeping the 0.3 A the default step
    # would have thrown away.
    app = _app(mode=MODE_FORCED, has_fixed_amps=True, fixed_amps_value=6,
               fixed_amps_step=0.1)
    res = optimize([app], {}, excess_amps=7.85, peak_amps=None,
                   energy=_energy(), now=T0)
    assert res.fixed_amps_commands == [SetFixedAmps("a", 7.8)]


def test_setpoint_falls_back_to_the_default_step():
    # Same budget, no step from the entity -> the 0.5 A default applies.
    app = _app(mode=MODE_FORCED, has_fixed_amps=True, fixed_amps_value=6)
    res = optimize([app], {}, excess_amps=7.85, peak_amps=None,
                   energy=_energy(), now=T0)
    assert res.fixed_amps_commands == [SetFixedAmps("a", 7.5)]


# ---------------------------------------------------------------------------
# Car-SoC force-charge override (EV chargers)
# ---------------------------------------------------------------------------

def test_force_charge_pure_rule():
    ev = dict(mode=MODE_PARTIAL_SOLAR, soc=30, force_charge_below_soc=50)
    assert force_charge_active(_app(**ev)) is True
    assert effective_mode(_app(**ev)) == MODE_FORCED
    # Not plugged in -> the car's SoC is irrelevant.
    assert force_charge_active(_app(**{**ev, "connected": False})) is False
    # No SoC reading (sensor unavailable / no car) -> fail safe, no forcing.
    assert force_charge_active(_app(**{**ev, "soc": None})) is False
    # No threshold (appliance has no SoC sensor configured) -> feature off.
    assert force_charge_active(_app(**{**ev, "force_charge_below_soc": None})) is False
    # Release exactly at the threshold; strictly below forces.
    assert force_charge_active(_app(**{**ev, "soc": 50})) is False
    assert force_charge_active(_app(**{**ev, "soc": 49.9})) is True
    # Threshold 0 (the default) can never force.
    assert force_charge_active(_app(**{**ev, "soc": 0, "force_charge_below_soc": 0})) is False
    # Unmanaged is never overridden.
    unmanaged = _app(**{**ev, "mode": MODE_UNMANAGED})
    assert force_charge_active(unmanaged) is False
    assert effective_mode(unmanaged) == MODE_UNMANAGED


def test_force_charge_partial_solar_runs_on_grid():
    # Importing (surplus -3 A): partial_solar would stay off, but the car is
    # plugged in below its threshold -> driven as forced, charges from the grid.
    app = _app(mode=MODE_PARTIAL_SOLAR, min_amps=6, soc=30, force_charge_below_soc=50)
    states = {}
    res = optimize([app], states, excess_amps=-3.0, peak_amps=20.0,
                   energy=_energy(), now=T0)
    assert res.active == ["a"]
    assert res.forced == ["a"]
    # Control: same appliance with a charged car -> the mode's own gate applies.
    full = _app(mode=MODE_PARTIAL_SOLAR, min_amps=6, soc=90, force_charge_below_soc=50)
    res2 = optimize([full], {}, excess_amps=-3.0, peak_amps=20.0,
                    energy=_energy(), now=T0)
    assert res2.active == []
    assert res2.forced == []


def test_force_charge_full_solar_ignores_average_gate():
    # full_solar with a starved rolling average would never start; the override
    # must reach the run condition too (not only the budget path).
    app = _app(mode=MODE_FULL_SOLAR, min_amps=6, soc=30, force_charge_below_soc=50)
    st = ApplianceState()
    st.excess_history.extend([0.0] * 18)
    states = {"a": st}
    res = optimize([app], states, excess_amps=0.0, peak_amps=20.0,
                   energy=_energy(), now=T0)
    assert res.active == ["a"]
    assert res.forced == ["a"]


def test_force_charge_uses_additive_budget():
    # Mirror of test_forced_additive_budget_uses_solar_on_top_of_grid, but the
    # appliance is partial_solar and only forced by the car SoC: the budget path
    # must use the effective mode, so solar 4 A + grid headroom (16 - 1) A -> 16.
    app = _app(mode=MODE_PARTIAL_SOLAR, has_fixed_amps=True, fixed_amps_value=6,
               max_amps=16, soc=30, force_charge_below_soc=50)
    res = optimize([app], {}, excess_amps=4.0, peak_amps=16.0,
                   energy=_energy(), now=T0)
    assert res.active == ["a"]
    assert res.fixed_amps_commands[0].value == 16.0
    # Without the override the same surplus scales on solar only: 4 A < 6 A min
    # -> floors at min_amps.
    plain = _app(mode=MODE_PARTIAL_SOLAR, has_fixed_amps=True, fixed_amps_value=6,
                 max_amps=16, partial_min_excess_amps=0)
    res2 = optimize([plain], {}, excess_amps=4.0, peak_amps=16.0,
                    energy=_energy(), now=T0)
    assert res2.fixed_amps_commands[0].value == 6.0


def test_force_charge_still_respects_peak_gate():
    # Forcing never buys a way past the quarter-hour cap: with 3 A of grid
    # headroom the 6 A minimum is unreachable -> off, but still reported forced
    # ("EMS wants to force-charge; the peak limit is what blocks it").
    app = _app(mode=MODE_PARTIAL_SOLAR, min_amps=6, soc=30, force_charge_below_soc=50)
    states = {}
    res = optimize([app], states, excess_amps=0.0, peak_amps=3.0,
                   energy=_energy(), now=T0)
    assert res.active == []
    assert res.forced == ["a"]
    assert states["a"].is_on is False


def test_force_charge_not_when_unplugged():
    app = _app(mode=MODE_PARTIAL_SOLAR, connected=False, soc=10, force_charge_below_soc=50)
    res = optimize([app], {}, excess_amps=-3.0, peak_amps=20.0,
                   energy=_energy(), now=T0)
    assert res.active == []
    assert res.forced == []


def test_force_charge_not_when_soc_unknown():
    app = _app(mode=MODE_PARTIAL_SOLAR, soc=None, force_charge_below_soc=50)
    res = optimize([app], {}, excess_amps=-3.0, peak_amps=20.0,
                   energy=_energy(), now=T0)
    assert res.active == []
    assert res.forced == []


def test_force_charge_not_when_threshold_unset():
    app = _app(mode=MODE_PARTIAL_SOLAR, soc=10, force_charge_below_soc=None)
    res = optimize([app], {}, excess_amps=-3.0, peak_amps=20.0,
                   energy=_energy(), now=T0)
    assert res.active == []
    assert res.forced == []


def test_force_charge_releases_at_threshold():
    states = {}
    low = _app(mode=MODE_PARTIAL_SOLAR, min_amps=6, hysteresis_seconds=0,
               soc=49, force_charge_below_soc=50)
    r1 = optimize([low], states, excess_amps=-3.0, peak_amps=20.0,
                  energy=_energy(), now=T0)
    assert r1.active == ["a"] and r1.forced == ["a"]
    # Next cycle the car reached the threshold: override released and, still
    # importing, the partial_solar gate switches it off.
    done = _app(mode=MODE_PARTIAL_SOLAR, min_amps=6, hysteresis_seconds=0,
                soc=50, force_charge_below_soc=50)
    r2 = optimize([done], states, excess_amps=-3.0, peak_amps=20.0,
                  energy=_energy(), now=T0 + timedelta(seconds=10))
    assert r2.forced == []
    assert r2.active == []


def test_force_charge_never_overrides_unmanaged():
    app = _app(mode=MODE_UNMANAGED, entity_on=True, has_fixed_amps=True,
               fixed_amps_value=6, soc=5, force_charge_below_soc=50)
    res = optimize([app], {}, excess_amps=-3.0, peak_amps=20.0,
                   energy=_energy(), now=T0)
    assert res.active == []
    assert res.forced == []
    assert res.fixed_amps_commands == []


def test_force_charge_without_demand_feature_runs_at_min():
    # No quarterly-demand sensors (peak_amps None): forced is not additive, so a
    # negative surplus floors the setpoint at min_amps — same as `forced` today.
    app = _app(mode=MODE_PARTIAL_SOLAR, min_amps=6, has_fixed_amps=True,
               fixed_amps_value=6, soc=30, force_charge_below_soc=50)
    res = optimize([app], {}, excess_amps=-3.0, peak_amps=None,
                   energy=_energy(), now=T0)
    assert res.active == ["a"]
    assert res.forced == ["a"]
    assert res.fixed_amps_commands == [SetFixedAmps("a", 6.0)]


def test_forced_list_defaults_empty():
    assert OptimizeResult(active=[], decided_amps=0.0, fixed_amps_commands=[]).forced == []
    app = _app(mode=MODE_FORCED)
    res = optimize([app], {}, excess_amps=10.0, peak_amps=None,
                   energy=_energy(), now=T0)
    assert res.active == ["a"]
    assert res.forced == []


def test_force_charge_logs_transitions_once(caplog):
    states = {}
    low = _app(mode=MODE_PARTIAL_SOLAR, soc=30, force_charge_below_soc=50)
    high = _app(mode=MODE_PARTIAL_SOLAR, soc=60, force_charge_below_soc=50)
    with caplog.at_level(logging.INFO, logger="ems_core.optimizer"):
        optimize([low], states, excess_amps=-3.0, peak_amps=20.0, energy=_energy(), now=T0)
        optimize([low], states, excess_amps=-3.0, peak_amps=20.0, energy=_energy(),
                 now=T0 + timedelta(seconds=10))
        optimize([high], states, excess_amps=-3.0, peak_amps=20.0, energy=_energy(),
                 now=T0 + timedelta(seconds=20))
        optimize([high], states, excess_amps=-3.0, peak_amps=20.0, energy=_energy(),
                 now=T0 + timedelta(seconds=30))
    forcing = [r for r in caplog.records if "forcing grid charge" in r.getMessage()]
    released = [r for r in caplog.records if "force charge released" in r.getMessage()]
    assert len(forcing) == 1
    assert len(released) == 1


# ---------------------------------------------------------------------------
# "Switched on but not consuming" problem flag
# ---------------------------------------------------------------------------

GRACE = timedelta(seconds=NOT_CONSUMING_GRACE_SECONDS)


def _idle(**kw):
    # A forced appliance with a power sensor reading zero: EMS wants it on.
    base = dict(mode=MODE_FORCED, has_power_sensor=True, measured_amps=0.0, entity_on=True)
    base.update(kw)
    return _app(**base)


def _cycles(app_by_time, states):
    last = None
    for offset, app in app_by_time:
        last = optimize([app], states, excess_amps=10.0, peak_amps=None,
                        energy=_energy(), now=T0 + timedelta(seconds=offset))
    return last


def test_not_consuming_flags_after_grace_only():
    states = {}
    r = _cycles([(0, _idle())], states)
    assert r.active == ["a"] and r.not_consuming == []          # timer just started
    r = _cycles([(NOT_CONSUMING_GRACE_SECONDS - 1, _idle())], states)
    assert r.not_consuming == []                                # still within grace
    r = _cycles([(NOT_CONSUMING_GRACE_SECONDS, _idle())], states)
    assert r.not_consuming == ["a"]                             # grace elapsed


def test_not_consuming_resets_when_draw_returns():
    states = {}
    _cycles([(0, _idle())], states)
    _cycles([(100, _idle(measured_amps=6.2))], states)          # drawing -> reset
    assert states["a"].not_consuming_since is None
    r = _cycles([(NOT_CONSUMING_GRACE_SECONDS + 100, _idle())], states)
    assert r.not_consuming == []                                # timer restarted at +grace+100


def test_not_consuming_exempt_when_car_nearly_full():
    states = {}
    full = _idle(soc=SOC_FULL_PERCENT, force_charge_below_soc=0)
    _cycles([(0, full)], states)
    r = _cycles([(NOT_CONSUMING_GRACE_SECONDS * 2, full)], states)
    assert r.not_consuming == []
    # Just under the "full" mark the taper exemption does not apply.
    low = _idle(soc=SOC_FULL_PERCENT - 1, force_charge_below_soc=0)
    states2 = {}
    _cycles([(0, low)], states2)
    r2 = _cycles([(NOT_CONSUMING_GRACE_SECONDS, low)], states2)
    assert r2.not_consuming == ["a"]


def test_not_consuming_requires_power_sensor_and_reading():
    # No power sensor: unknowable, never flagged.
    states = {}
    _cycles([(0, _idle(has_power_sensor=False, measured_amps=None))], states)
    r = _cycles([(NOT_CONSUMING_GRACE_SECONDS, _idle(has_power_sensor=False, measured_amps=None))], states)
    assert r.not_consuming == []
    # Power sensor dropout (None) is not a fault either.
    states2 = {}
    _cycles([(0, _idle(measured_amps=None))], states2)
    r2 = _cycles([(NOT_CONSUMING_GRACE_SECONDS, _idle(measured_amps=None))], states2)
    assert r2.not_consuming == []


def test_not_consuming_only_when_ems_switched_it_on():
    # partial_solar with no surplus: EMS keeps it OFF, so zero draw is expected.
    states = {}
    off = _idle(mode=MODE_PARTIAL_SOLAR)
    for offset in (0, NOT_CONSUMING_GRACE_SECONDS):
        r = optimize([off], states, excess_amps=-3.0, peak_amps=None,
                     energy=_energy(), now=T0 + timedelta(seconds=offset))
    assert r.active == [] and r.not_consuming == []
    assert states["a"].not_consuming_since is None
    # Unmanaged: left alone, never flagged, timer cleared.
    states2 = {"a": ApplianceState(not_consuming_since=T0 - GRACE)}
    r2 = optimize([_idle(mode=MODE_UNMANAGED)], states2, excess_amps=10.0, peak_amps=None,
                  energy=_energy(), now=T0)
    assert r2.not_consuming == []
    assert states2["a"].not_consuming_since is None


def test_not_consuming_pure_helper():
    st = ApplianceState()
    assert not_consuming(_idle(), st, active=True, now=T0) is False
    assert st.not_consuming_since == T0
    assert not_consuming(_idle(), st, active=True, now=T0 + GRACE) is True
    assert not_consuming(_idle(), st, active=False, now=T0 + GRACE) is False
    assert st.not_consuming_since is None


def test_partial_threshold_has_on_off_deadband():
    # partial_solar threshold gate carries the same 0.5 A deadband as the
    # min-amps gate: start only once the averaged excess clears the threshold
    # by ON_OFF_HYSTERESIS_AMPS, stay on down to the threshold itself.
    # Threshold 3 A. One-sample averaging and no time hysteresis so only the
    # amps gate decides; excess stays under min_amps so the instantaneous
    # min-amps gate never short-circuits the decision.
    app = _app(mode=MODE_PARTIAL_SOLAR, min_amps=6, partial_min_excess_amps=3.0,
               hysteresis_seconds=0, averaging_window_seconds=10)
    states = {}
    res = optimize([app], states, excess_amps=3.4, peak_amps=None,
                   energy=_energy(), now=T0)
    assert res.active == []
    res = optimize([app], states, excess_amps=3.5, peak_amps=None,
                   energy=_energy(), now=T0 + timedelta(seconds=10))
    assert res.active == ["a"]
    res = optimize([app], states, excess_amps=3.1, peak_amps=None,
                   energy=_energy(), now=T0 + timedelta(seconds=20))
    assert res.active == ["a"]
    res = optimize([app], states, excess_amps=2.9, peak_amps=None,
                   energy=_energy(), now=T0 + timedelta(seconds=30))
    assert res.active == []
