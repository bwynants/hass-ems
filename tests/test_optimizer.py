"""Tests for ems_core.optimizer — allocation, throttling, SoC, hysteresis, shed."""
import logging
from datetime import datetime, timedelta

from ems_core.optimizer import optimize, critical_shed, record_excess
from ems_core.models import (
    ApplianceInput, ApplianceState, EnergyInput, BatteryStatus, SetFixedAmps,
)
from ems_core.const import (
    MODE_FORCED, MODE_PARTIAL_SOLAR, MODE_FULL_SOLAR, MODE_OFF_PEAK, MODE_UNMANAGED,
    BATTERY_MODE_STRICT, BATTERY_MODE_RELAXED,
)

T0 = datetime(2026, 7, 1, 8, 0, 0)


def _app(**kw):
    base = dict(
        name="a", priority=100, min_amps=6, max_amps=16, mode=MODE_PARTIAL_SOLAR,
        has_fixed_amps=False, fixed_amps_value=None, fixed_amps_hold_seconds=120,
        partial_min_excess=1000, hysteresis_seconds=120, averaging_window_seconds=180,
        connected=True, entity_on=False, measured_amps=None, is_battery=False,
        ignore_battery_protection=False, min_soc_override=None, soc_hysteresis_percent=5,
    )
    base.update(kw)
    base.setdefault("uid", base["name"])
    return ApplianceInput(**base)


def _energy(**kw):
    base = dict(
        grid_w=None, solar_w=0.0, house_load_w=0.0, voltage=230, off_peak=False,
        cost_peak=0.40, cost_offpeak=0.20, gain_export=0.10,
    )
    base.update(kw)
    return EnergyInput(**base)


NO_BAT = BatteryStatus()


def test_forced_additive_budget_uses_solar_on_top_of_grid():
    # forced appliance, solar surplus 4A + peak headroom 16A => can scale high.
    app = _app(mode=MODE_FORCED, has_fixed_amps=True, fixed_amps_value=6, max_amps=16)
    states = {}
    res = optimize([app], states, excess_amps=4.0, peak_amps=16.0,
                   energy=_energy(), battery=NO_BAT, now=T0)
    assert res.active == ["a"]
    # forced_budget = remaining(4) + peak(16) = 20 -> scaled clamps to max 16 ->
    # first fixed_amps write rounds 16 to 16.
    assert res.fixed_amps_commands[0].value == 16.0


def test_non_forced_throttled_to_peak_headroom():
    # partial_solar with big solar but tiny peak headroom => throttled to headroom
    app = _app(mode=MODE_PARTIAL_SOLAR, has_fixed_amps=True, fixed_amps_value=6,
               min_amps=6, max_amps=16, partial_min_excess=0)
    states = {}
    res = optimize([app], states, excess_amps=16.0, peak_amps=8.0,
                   energy=_energy(), battery=NO_BAT, now=T0)
    assert res.active == ["a"]
    # scaled(16 clamped to max 16) then min(16, peak 8)=8; first write rounds
    # 8 down-with-margin to 7.5 (0.5 A step, 0.25 A deadband: 8 - 0.25 -> 7.5).
    assert res.fixed_amps_commands[0].value == 7.5


def test_peak_headroom_sheds_when_below_min():
    # headroom 3A < min 6A => appliance cannot run
    app = _app(mode=MODE_PARTIAL_SOLAR, min_amps=6, partial_min_excess=0)
    states = {}
    res = optimize([app], states, excess_amps=16.0, peak_amps=3.0,
                   energy=_energy(), battery=NO_BAT, now=T0)
    assert res.active == []


def test_priority_order_high_priority_claims_headroom_first():
    # No solar (excess 0) so the winner consumes GRID headroom; headroom only
    # covers one appliance at min (6A) — the lower priority number wins.
    a1 = _app(name="hi", priority=1, min_amps=6, mode=MODE_PARTIAL_SOLAR, partial_min_excess=0)
    a2 = _app(name="lo", priority=50, min_amps=6, mode=MODE_PARTIAL_SOLAR, partial_min_excess=0)
    states = {}
    res = optimize([a2, a1], states, excess_amps=0.0, peak_amps=6.0,
                   energy=_energy(), battery=NO_BAT, now=T0)
    assert res.active == ["hi"]


def test_full_solar_off_when_not_covered():
    app = _app(mode=MODE_FULL_SOLAR, min_amps=6, partial_min_excess=0)
    states = {}
    res = optimize([app], states, excess_amps=3.0, peak_amps=None,
                   energy=_energy(), battery=NO_BAT, now=T0)
    assert res.active == []


def test_full_solar_on_when_covered():
    app = _app(mode=MODE_FULL_SOLAR, min_amps=6)
    states = {}
    res = optimize([app], states, excess_amps=8.0, peak_amps=None,
                   energy=_energy(), battery=NO_BAT, now=T0)
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
                       energy=_energy(), battery=NO_BAT, now=T0)
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
                        energy=_energy(), battery=NO_BAT,
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
                   energy=_energy(), battery=NO_BAT, now=T0)
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
                   energy=_energy(), battery=NO_BAT, now=T0)
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
                   energy=_energy(), battery=NO_BAT, now=T0)
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
                   energy=_energy(), battery=NO_BAT, now=T0)
    assert res.active == []


def test_off_peak_runs_free_during_off_peak_hours():
    # Baseline unchanged: off_peak mode runs freely in the off-peak window even
    # while importing (excess negative).
    app = _app(mode=MODE_OFF_PEAK, min_amps=6)
    states = {}
    res = optimize([app], states, excess_amps=-10.0, peak_amps=None,
                   energy=_energy(off_peak=True), battery=NO_BAT, now=T0)
    assert res.active == ["a"]


def test_off_peak_gated_by_breakeven_when_peak_dearer():
    # Peak hours, peak dearer than off-peak (default 0.40 > 0.20), importing ->
    # the solar breakeven gate keeps it OFF.
    app = _app(mode=MODE_OFF_PEAK, min_amps=6)
    states = {}
    res = optimize([app], states, excess_amps=-10.0, peak_amps=None,
                   energy=_energy(off_peak=False), battery=NO_BAT, now=T0)
    assert res.active == []


def test_off_peak_runs_free_when_peak_cheaper_than_off_peak():
    # Dynamic tariff inversion: during PEAK hours but peak price BELOW off-peak
    # price -> peak is the cheap period, so run freely even while importing
    # (this is the case the old breakeven-only gate would drop under heavy import).
    app = _app(mode=MODE_OFF_PEAK, min_amps=6)
    states = {}
    res = optimize([app], states, excess_amps=-10.0, peak_amps=None,
                   energy=_energy(off_peak=False, cost_peak=0.15, cost_offpeak=0.20),
                   battery=NO_BAT, now=T0)
    assert res.active == ["a"]


def test_off_peak_free_run_still_respects_peak_headroom():
    # Running "freely" is only about eligibility — the quarter-hour demand cap
    # still sheds it when headroom is below min.
    app = _app(mode=MODE_OFF_PEAK, min_amps=6)
    states = {}
    res = optimize([app], states, excess_amps=0.0, peak_amps=3.0,
                   energy=_energy(off_peak=False, cost_peak=0.15, cost_offpeak=0.20),
                   battery=NO_BAT, now=T0)
    assert res.active == []


def test_soc_strict_blocks():
    app = _app(mode=MODE_FORCED, min_amps=6)
    bat = BatteryStatus(soc=10, min_soc=20, protection_mode=BATTERY_MODE_STRICT)
    states = {}
    res = optimize([app], states, excess_amps=10.0, peak_amps=None,
                   energy=_energy(), battery=bat, now=T0)
    assert res.active == []
    assert states["a"].soc_blocked is True


def test_soc_relaxed_runs_only_when_solar_covers():
    app = _app(mode=MODE_FORCED, min_amps=6)
    bat = BatteryStatus(soc=10, min_soc=20, protection_mode=BATTERY_MODE_RELAXED)
    # solar covers (10A >= 6A min) -> runs despite low SoC
    states = {}
    res = optimize([app], states, excess_amps=10.0, peak_amps=None,
                   energy=_energy(), battery=bat, now=T0)
    assert res.active == ["a"]
    # solar does NOT cover (3A < 6A) -> blocked
    states2 = {}
    res2 = optimize([app], states2, excess_amps=3.0, peak_amps=None,
                    energy=_energy(), battery=bat, now=T0)
    assert res2.active == []


def test_relaxed_soc_bypass_keeps_full_solar_average_gate():
    # A LOW battery must not make a full_solar appliance EASIER to start:
    # the relaxed bypass keeps the sustained-average requirement, so a
    # transient spike (current covers, average doesn't) still stays off.
    app = _app(mode=MODE_FULL_SOLAR, min_amps=6)
    bat = BatteryStatus(soc=10, min_soc=20, protection_mode=BATTERY_MODE_RELAXED)
    st = ApplianceState()
    st.excess_history.extend([2.0, 2.0, 2.0])
    states = {"a": st}
    res = optimize([app], states, excess_amps=10.0, peak_amps=None,
                   energy=_energy(), battery=bat, now=T0)
    assert res.active == []
    # Sustained + current coverage still runs under the relaxed bypass.
    st2 = ApplianceState()
    st2.excess_history.extend([8.0, 8.0, 8.0])
    states2 = {"a": st2}
    res2 = optimize([app], states2, excess_amps=8.0, peak_amps=None,
                    energy=_energy(), battery=bat, now=T0)
    assert res2.active == ["a"]


def test_setpoint_cleared_on_turn_off():
    # After a turn-off, re-activation must write a fresh setpoint immediately
    # instead of holding the stale high value through the hold window.
    app = _app(mode=MODE_FULL_SOLAR, min_amps=6, max_amps=16,
               has_fixed_amps=True, fixed_amps_value=16.0,
               hysteresis_seconds=0, fixed_amps_hold_seconds=120)
    states = {}
    r1 = optimize([app], states, excess_amps=20.0, peak_amps=None,
                  energy=_energy(), battery=NO_BAT, now=T0)
    assert r1.fixed_amps_commands == [SetFixedAmps("a", 16.0)]
    # Surplus gone -> off; setpoint tracking must be cleared.
    r2 = optimize([app], states, excess_amps=0.0, peak_amps=None,
                  energy=_energy(), battery=NO_BAT, now=T0 + timedelta(seconds=10))
    assert r2.active == []
    assert states["a"].last_fixed_amps is None
    # Modest surplus re-activates within the old hold window: fresh write at 6,
    # not a silent hold at the stale 16.
    r3 = optimize([app], states, excess_amps=6.5, peak_amps=None,
                  energy=_energy(), battery=NO_BAT, now=T0 + timedelta(seconds=20))
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
             energy=_energy(), battery=NO_BAT, now=T0)
    assert states["a"].is_on is True
    # Cycle 2: 30s later, condition now false, but within hysteresis -> stays on
    res = optimize([app], states, excess_amps=0.0, peak_amps=None,
                   energy=_energy(), battery=NO_BAT, now=T0 + timedelta(seconds=30))
    assert res.active == ["a"]
    # Cycle 3: 200s later, condition still false, past hysteresis -> turns off
    res2 = optimize([app], states, excess_amps=0.0, peak_amps=None,
                    energy=_energy(), battery=NO_BAT, now=T0 + timedelta(seconds=230))
    assert res2.active == []


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
                   energy=_energy(), battery=NO_BAT, now=T0)
    assert res.active == ["a1"]
    assert res.decided_amps == 6


def test_running_appliance_uses_measured():
    # While actually running, the measured draw takes precedence for budgeting.
    app = _app(mode=MODE_FULL_SOLAR, min_amps=6, measured_amps=9.0, entity_on=True)
    states = {}
    res = optimize([app], states, excess_amps=10.0, peak_amps=None,
                   energy=_energy(), battery=NO_BAT, now=T0)
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
                  energy=_energy(), battery=NO_BAT, now=T0)
    assert r1.active == ["a"]
    assert r1.fixed_amps_commands == [SetFixedAmps("a", 16.0)]
    # Headroom collapses below min -> hard off, state machine updated.
    r2 = optimize([app], states, excess_amps=0.0, peak_amps=3.0,
                  energy=_energy(), battery=NO_BAT, now=T0 + timedelta(seconds=10))
    assert r2.active == []
    assert states["a"].is_on is False
    assert states["a"].last_fixed_amps is None
    # Headroom recovers within the hysteresis window -> stays off (damped).
    r3 = optimize([app], states, excess_amps=0.0, peak_amps=8.0,
                  energy=_energy(), battery=NO_BAT, now=T0 + timedelta(seconds=20))
    assert r3.active == []
    # Past the window -> on again with a FRESH setpoint write (7.5 = 0.5-step of
    # the 8 A forced budget).
    r4 = optimize([app], states, excess_amps=0.0, peak_amps=8.0,
                  energy=_energy(), battery=NO_BAT, now=T0 + timedelta(seconds=90))
    assert r4.active == ["a"]
    assert r4.fixed_amps_commands == [SetFixedAmps("a", 7.5)]


def test_soc_block_held_when_sensor_unavailable():
    # A latched SoC block must survive the SoC sensor dropping out (fail safe).
    app = _app(mode=MODE_FORCED, min_amps=6)
    low = BatteryStatus(soc=10, min_soc=20, protection_mode=BATTERY_MODE_STRICT)
    states = {}
    res = optimize([app], states, excess_amps=10.0, peak_amps=None,
                   energy=_energy(), battery=low, now=T0)
    assert res.active == [] and states["a"].soc_blocked is True
    # Sensor unavailable: soc=None. The latch is held, appliance stays blocked.
    lost = BatteryStatus(soc=None, min_soc=20, protection_mode=BATTERY_MODE_STRICT)
    res2 = optimize([app], states, excess_amps=10.0, peak_amps=None,
                    energy=_energy(), battery=lost, now=T0 + timedelta(seconds=10))
    assert res2.active == []
    assert states["a"].soc_blocked is True


def test_min_soc_override_zero_disables_block():
    # min_soc_override=0 is a documented escape hatch: 'override if override
    # is not None' — a falsy-0 rewrite would silently re-enable blocking.
    app = _app(mode=MODE_FORCED, min_amps=6, min_soc_override=0)
    low = BatteryStatus(soc=10, min_soc=20, protection_mode=BATTERY_MODE_STRICT)
    states = {}
    res = optimize([app], states, excess_amps=10.0, peak_amps=None,
                   energy=_energy(), battery=low, now=T0)
    assert res.active == ["a"]
    assert states["a"].soc_blocked is False


def test_solar_covered_draw_keeps_peak_headroom():
    # A non-forced appliance running fully on solar must not consume grid
    # peak headroom — a downstream forced appliance still gets the grid budget.
    solar_app = _app(name="s", uid="s", priority=1, mode=MODE_FULL_SOLAR, min_amps=6)
    forced_app = _app(name="f", uid="f", priority=50, mode=MODE_FORCED, min_amps=6)
    states = {}
    # 10 A solar, 6 A grid headroom: solar app uses 6 A of solar (headroom
    # untouched), forced app then runs on remaining 4 A solar + 6 A grid.
    res = optimize([solar_app, forced_app], states, excess_amps=10.0, peak_amps=6.0,
                   energy=_energy(), battery=NO_BAT, now=T0)
    assert res.active == ["s", "f"]


def test_forced_additive_split_seen_by_second_appliance():
    # The forced solar/grid split must leave the correct budgets for the NEXT
    # appliance: forced draws 16 (10 solar + 6 grid of the 10 headroom), so a
    # second forced appliance sees 0 solar + 4 grid < min 6 -> capped off.
    f1 = _app(name="f1", uid="f1", priority=1, mode=MODE_FORCED, min_amps=6,
              max_amps=16, has_fixed_amps=True, fixed_amps_value=16.0)
    f2 = _app(name="f2", uid="f2", priority=50, mode=MODE_FORCED, min_amps=6)
    states = {}
    res = optimize([f1, f2], states, excess_amps=10.0, peak_amps=10.0,
                   energy=_energy(), battery=NO_BAT, now=T0)
    assert res.active == ["f1"]
    # With more headroom the second one fits again (0 solar + 10 grid >= 6).
    states2 = {}
    res2 = optimize([f1, f2], states2, excess_amps=10.0, peak_amps=16.0,
                    energy=_energy(), battery=NO_BAT, now=T0)
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
                   energy=_energy(), battery=NO_BAT, now=T0)
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
                   energy=_energy(), battery=NO_BAT, now=T0)
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
