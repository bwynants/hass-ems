"""Tests for ems_core.fixed_amps — whole-amp stepping + write gating."""
from datetime import datetime, timedelta

from ems_core.fixed_amps import step_target, decide

DB = 0.5
T0 = datetime(2026, 7, 1, 8, 0, 0)


def test_step_first_set_rounds_down_with_margin():
    # 6.8 available, no prior value -> run at 6 (round down with 0.5 margin)
    assert step_target(6.8, 6, 16, None) == 6.0
    # 7.4 -> still 6 (needs 7.5 to reach 7)
    assert step_target(7.4, 6, 16, None) == 6.0


def test_step_up_at_half_above_next():
    # holding at 6, step up to 7 only when 7.5 is available
    assert step_target(7.49, 6, 16, 6.0) == 6.0   # still holds
    assert step_target(7.5, 6, 16, 6.0) == 7.0    # steps up


def test_step_down_at_half_below():
    # holding at 6, step down to 5 only once it falls below 5.5
    assert step_target(5.6, 5, 16, 6.0) == 6.0    # holds (within band)
    assert step_target(5.49, 5, 16, 6.0) == 5.0   # steps down to adjacent amp


def test_step_hold_band():
    # within [N-0.5, N+1.5) it holds N
    assert step_target(6.0, 6, 16, 6.0) == 6.0
    assert step_target(7.0, 6, 16, 6.0) == 6.0


def test_step_max_clamp():
    assert step_target(20, 6, 16, 6.0) == 16.0
    assert step_target(16, 6, 16, 6.0) == 16.0


def test_step_clamps_to_min():
    assert step_target(2, 6, 16, None) == 6.0


def test_decide_first_write():
    d = decide(6.8, 6, 16, None, None, T0, 120, None)
    assert d.write_value == 6.0 and d.effective == 6.0


def test_decide_hold_when_unchanged_and_entity_matches():
    d = decide(6.2, 6, 16, 6.0, T0, T0, 120, current_entity_value=6.0)
    assert d.write_value is None and d.effective == 6.0


def test_decide_reasserts_on_drift():
    # target unchanged (6) but entity drifted to 9 -> re-assert
    d = decide(6.2, 6, 16, 6.0, T0, T0, 120, current_entity_value=9.0)
    assert d.write_value == 6.0 and d.effective == 6.0


def test_decide_suppresses_change_within_hold_window():
    # wants to step up to 7 but only 30s since last change (< 120s hold)
    now = T0 + timedelta(seconds=30)
    d = decide(7.5, 6, 16, 6.0, T0, now, 120, current_entity_value=6.0)
    assert d.write_value is None and d.effective == 6.0


def test_decide_allows_change_after_hold_window():
    now = T0 + timedelta(seconds=121)
    d = decide(7.5, 6, 16, 6.0, T0, now, 120, current_entity_value=6.0)
    assert d.write_value == 7.0 and d.effective == 7.0
