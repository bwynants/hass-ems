"""Tests for ems_core.demand_alerts — once-per-quarter peak announcements."""
from ems_core.demand_alerts import (
    ALERT_CLEARED,
    ALERT_CRITICAL,
    ALERT_HIGH,
    ALERT_NEW_MONTH_PEAK,
    DemandAlertState,
    is_new_month_peak,
    resolve_quarter,
    step_alerts,
)


def _step(state, **kw):
    """step_alerts with the bare once-per-quarter policy unless overridden:
    no sustain, no announce minute (the production defaults are the subject
    of the sustain/gate tests at the bottom)."""
    kw.setdefault("high_sustain", 1)
    kw.setdefault("critical_sustain", 1)
    kw.setdefault("high_alert_minute", 0)
    kw.setdefault("critical_alert_minute", 0)
    return step_alerts(state, **kw)


def _run(state, quarter_id, flags, **kw):
    """Feed a sequence of (high, critical) cycles; return state + all events."""
    events = []
    for high, critical in flags:
        state, ev = _step(state, quarter_id=quarter_id, high=high, critical=critical, **kw)
        events.extend(ev)
    return state, events


def test_high_announced_once_per_quarter():
    state, events = _run(DemandAlertState(), 100, [
        (False, False), (True, False), (True, False), (False, False), (True, False),
    ])
    assert events == [ALERT_HIGH]
    assert state.high_announced and not state.critical_announced


def test_critical_after_high_gets_its_own_message():
    state, events = _run(DemandAlertState(), 100, [
        (True, False), (True, True), (True, True), (True, False), (True, True),
    ])
    assert events == [ALERT_HIGH, ALERT_CRITICAL]


def test_critical_first_suppresses_later_high():
    # Critical latches without High having announced first (both flip on in the
    # same cycle, or a restart mid-quarter): only the critical message, and a
    # later High-only phase stays silent.
    state, events = _run(DemandAlertState(), 100, [
        (True, True), (True, False), (True, False),
    ])
    assert events == [ALERT_CRITICAL]
    assert state.high_announced and state.critical_announced


def test_flapping_is_silent():
    _, events = _run(DemandAlertState(), 100, [
        (True, False), (False, False), (True, False), (False, False),
        (True, True), (True, False), (True, True),
    ])
    assert events == [ALERT_HIGH, ALERT_CRITICAL]


def test_quiet_quarter_emits_nothing():
    state, events = _run(DemandAlertState(), 100, [(False, False)] * 10)
    assert events == []
    assert state == DemandAlertState(quarter_id=100)


def test_quarter_rollover_resets_flags_and_owes_verdict():
    state, _ = _run(DemandAlertState(), 100, [(True, False)])
    state, events = _step(state, quarter_id=101, high=False, critical=False)
    assert events == []
    assert state.quarter_id == 101
    assert not state.high_announced and not state.critical_announced
    assert state.pending_verdict is True
    # And the new quarter can warn again.
    state, events = _step(state, quarter_id=101, high=True, critical=False)
    assert events == [ALERT_HIGH]


def test_quarter_rollover_without_warning_owes_nothing():
    state, _ = _run(DemandAlertState(), 100, [(False, False)])
    state, _ = _step(state, quarter_id=101, high=False, critical=False)
    assert state.pending_verdict is False


def test_gap_drops_the_verdict():
    # EMS was down for a quarter: the baseline no longer matches, so no
    # stale "cleared" later.
    state, _ = _run(DemandAlertState(), 100, [(True, True)])
    state, _ = _step(state, quarter_id=102, high=False, critical=False)
    assert state.pending_verdict is False


def test_first_quarter_after_start_owes_nothing():
    state, _ = _step(DemandAlertState(), quarter_id=100, high=False, critical=False)
    assert state.pending_verdict is False


def test_resolve_cleared_when_warned_and_no_record():
    state = DemandAlertState(quarter_id=101, pending_verdict=True)
    state, events = resolve_quarter(state, old_peak=4.18, new_peak=4.18)
    assert events == [ALERT_CLEARED]
    assert state.pending_verdict is False


def test_resolve_new_peak_beats_cleared():
    state = DemandAlertState(quarter_id=101, pending_verdict=True)
    state, events = resolve_quarter(state, old_peak=4.18, new_peak=4.40)
    assert events == [ALERT_NEW_MONTH_PEAK]
    assert state.pending_verdict is False


def test_resolve_new_peak_without_prior_warning():
    # A record set without the estimate ever latching High is still a record.
    state = DemandAlertState(quarter_id=101, pending_verdict=False)
    state, events = resolve_quarter(state, old_peak=4.18, new_peak=4.40)
    assert events == [ALERT_NEW_MONTH_PEAK]


def test_resolve_nothing_when_quiet_and_no_record():
    state = DemandAlertState(quarter_id=101, pending_verdict=False)
    state, events = resolve_quarter(state, old_peak=4.18, new_peak=4.18)
    assert events == []


def test_resolve_month_rollover_is_cleared_not_new_peak():
    # New month: the source drops to the current quarter's value.
    state = DemandAlertState(quarter_id=101, pending_verdict=True)
    _, events = resolve_quarter(state, old_peak=4.18, new_peak=2.10)
    assert events == [ALERT_CLEARED]


def test_resolve_first_population_is_not_a_peak():
    state = DemandAlertState(quarter_id=101, pending_verdict=True)
    _, events = resolve_quarter(state, old_peak=None, new_peak=4.18)
    assert events == [ALERT_CLEARED]
    _, events = resolve_quarter(state, old_peak=0.0, new_peak=4.18)
    assert events == [ALERT_CLEARED]


def test_is_new_month_peak_rounding():
    assert is_new_month_peak(4.18, 4.184) is False   # same at 2 decimals
    assert is_new_month_peak(4.18, 4.19) is True
    assert is_new_month_peak(4.18, None) is False


def test_state_round_trips_through_dict():
    state = DemandAlertState(quarter_id=7, high_announced=True, pending_verdict=True)
    assert DemandAlertState.from_dict(state.as_dict()) == state
    assert DemandAlertState.from_dict(None) == DemandAlertState()
    assert DemandAlertState.from_dict({"quarter_id": "bad"}) == DemandAlertState()


def test_full_scenario_warning_then_cleared_next_quarter():
    """End to end: High and Critical in Q, verdict 'cleared' resolved in Q+1."""
    state = DemandAlertState()
    all_events = []
    # Quarter 100: climbs through High into Critical and back down.
    for flags in [(False, False), (True, False), (True, True), (True, False), (False, False)]:
        state, ev = _step(state, quarter_id=100, high=flags[0], critical=flags[1])
        all_events += ev
    # Quarter 101 starts (gates reset the flags), snapshot resolves ~1 min in.
    state, ev = _step(state, quarter_id=101, high=False, critical=False)
    all_events += ev
    state, ev = resolve_quarter(state, old_peak=4.18, new_peak=4.18)
    all_events += ev
    assert all_events == [ALERT_HIGH, ALERT_CRITICAL, ALERT_CLEARED]
    # Nothing more owed afterwards, even if resolve is called again.
    _, ev = resolve_quarter(state, old_peak=4.18, new_peak=4.18)
    assert ev == []


# --- sustain, announce-minute gates and the controller hold (0.7) -----------


def test_high_needs_sustained_cycles():
    # two cycles latched then gone: silent. Three in a row: announced on the third.
    _, events = _run(DemandAlertState(), 100,
                           [(True, False), (True, False), (False, False)], high_sustain=3)
    assert events == []
    state, events = _run(DemandAlertState(), 100,
                               [(True, False), (True, False), (True, False)], high_sustain=3)
    assert events == [ALERT_HIGH]
    assert state.high_cycles == 3


def test_flapping_resets_the_sustain_counter():
    _, events = _run(DemandAlertState(), 100,
                           [(True, False), (True, False), (False, False),
                            (True, False), (True, False)], high_sustain=3)
    assert events == []


def test_announce_minute_gate():
    # Latched and sustained at minute 7: silent. Minute 8: announced.
    state, events = _run(DemandAlertState(), 100, [(True, False)] * 3,
                               high_sustain=3, minute_in_quarter=7, high_alert_minute=8)
    assert events == []
    state, events = _step(state, quarter_id=100, high=True, critical=False,
                                high_sustain=3, minute_in_quarter=8, high_alert_minute=8)
    assert events == [ALERT_HIGH]


def test_critical_has_its_own_sustain_and_minute():
    state, events = _run(DemandAlertState(), 100, [(True, True)] * 2,
                               critical_sustain=2, minute_in_quarter=9,
                               critical_alert_minute=10, high_alert_minute=8)
    # High (sustain 1 here) announced at once; Critical waits for minute 10.
    assert events == [ALERT_HIGH]
    state, events = _step(state, quarter_id=100, high=True, critical=True,
                                critical_sustain=2, minute_in_quarter=10,
                                critical_alert_minute=10, high_alert_minute=8)
    assert events == [ALERT_CRITICAL]


def test_hold_defers_until_it_lapses_and_the_risk_is_still_there():
    # EMS is shedding (hold): sustained High stays silent...
    state, events = _run(DemandAlertState(), 100, [(True, False)] * 4,
                               high_sustain=3, hold=True)
    assert events == []
    # ...the shed worked: the flag drops, nothing is ever announced.
    state, events = _step(state, quarter_id=100, high=False, critical=False,
                                high_sustain=3, hold=False)
    assert events == []
    # Other quarter: the shed did not help, the hold lapses -> announced at once
    # (the counter kept running through the hold).
    state, events = _run(DemandAlertState(), 200, [(True, False)] * 4,
                               high_sustain=3, hold=True)
    state, events = _step(state, quarter_id=200, high=True, critical=False,
                                high_sustain=3, hold=False)
    assert events == [ALERT_HIGH]


def test_sustain_counters_are_not_persisted():
    # They change every cycle of a High; persisting them would write the
    # store each cycle, and a restart simply re-counts a few cycles.
    state = DemandAlertState(quarter_id=100, high_cycles=2, critical_cycles=1)
    assert "high_cycles" not in state.as_dict()
    assert DemandAlertState.from_dict(state.as_dict()).high_cycles == 0


def test_production_defaults_announce_high_at_minute_8_after_3_cycles():
    state = DemandAlertState()
    events = []
    for minute in (6, 6, 7, 7, 8):
        state, ev = step_alerts(state, quarter_id=100, high=True, critical=False,
                                minute_in_quarter=minute)
        events.extend(ev)
    assert events == [ALERT_HIGH]
