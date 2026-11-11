"""Tests for ems_core.fixed_amps — 0.5 A stepping + write gating."""
from datetime import datetime, timedelta

from ems_core.fixed_amps import step_target, decide

DB = 0.25
T0 = datetime(2026, 7, 1, 8, 0, 0)


def test_step_first_set_rounds_down_with_margin():
    # 6.8 available, no prior value -> run at 6.5 (round down to 0.5 with 0.25 margin)
    assert step_target(6.8, 6, 16, None) == 6.5
    # 7.2 -> still 6.5 (needs 7.25 to reach 7.0)
    assert step_target(7.2, 6, 16, None) == 6.5
    assert step_target(7.25, 6, 16, None) == 7.0


def test_step_up_at_deadband_above_next():
    # holding at 6.0, step up to 6.5 only when 6.75 is available
    assert step_target(6.74, 6, 16, 6.0) == 6.0   # still holds
    assert step_target(6.75, 6, 16, 6.0) == 6.5   # steps up one 0.5 increment


def test_step_down_at_deadband_below():
    # holding at 6.0, step down to 5.5 only once it falls below 5.75
    assert step_target(5.75, 5, 16, 6.0) == 6.0   # holds (within band)
    assert step_target(5.74, 5, 16, 6.0) == 5.5   # steps down to adjacent increment


def test_step_hold_band():
    # within [N-0.25, N+0.75) it holds N
    assert step_target(6.0, 6, 16, 6.0) == 6.0
    assert step_target(6.7, 6, 16, 6.0) == 6.0


def test_step_lands_on_half_increments():
    # first-set quantises to the nearest 0.5 below (scaled - 0.25)
    assert step_target(9.1, 6, 16, None) == 8.5    # 9.1 - 0.25 = 8.85 -> 8.5
    assert step_target(9.75, 6, 16, None) == 9.5   # 9.75 - 0.25 = 9.5


def test_step_max_clamp():
    assert step_target(20, 6, 16, 6.0) == 16.0
    assert step_target(16, 6, 16, 6.0) == 16.0


def test_step_clamps_to_min():
    assert step_target(2, 6, 16, None) == 6.0


def test_decide_first_write():
    d = decide(6.8, 6, 16, None, None, T0, 120, None)
    assert d.write_value == 6.5 and d.effective == 6.5


def test_decide_hold_when_unchanged_and_entity_matches():
    d = decide(6.2, 6, 16, 6.0, T0, T0, 120, current_entity_value=6.0)
    assert d.write_value is None and d.effective == 6.0


def test_decide_reasserts_on_drift():
    # target unchanged (6.0) but entity drifted to 9 -> re-assert
    d = decide(6.2, 6, 16, 6.0, T0, T0, 120, current_entity_value=9.0)
    assert d.write_value == 6.0 and d.effective == 6.0


def test_decide_suppresses_change_within_hold_window():
    # wants to step up to 6.5 but only 30s since last change (< 120s hold)
    now = T0 + timedelta(seconds=30)
    d = decide(6.75, 6, 16, 6.0, T0, now, 120, current_entity_value=6.0)
    assert d.write_value is None and d.effective == 6.0


def test_decide_allows_change_after_hold_window():
    now = T0 + timedelta(seconds=121)
    d = decide(6.75, 6, 16, 6.0, T0, now, 120, current_entity_value=6.0)
    assert d.write_value == 6.5 and d.effective == 6.5


# --- per-appliance step: the "ampere to use" entity's own granularity --------


def test_step_target_honours_a_finer_step():
    # A 0.1 A entity: first set rounds down to a 0.1 increment with a 0.05
    # margin (deadband = half a step), instead of collapsing onto 0.5 A.
    assert step_target(6.83, 6, 16, None, deadband=0.05, step=0.1) == 6.7
    assert step_target(6.85, 6, 16, None, deadband=0.05, step=0.1) == 6.8


def test_finer_step_holds_and_steps_by_one_increment():
    # Holding 7.0 with a 0.1 step: band is [6.95, 7.15).
    assert step_target(7.14, 6, 16, 7.0, deadband=0.05, step=0.1) == 7.0
    assert step_target(7.15, 6, 16, 7.0, deadband=0.05, step=0.1) == 7.1
    assert step_target(6.94, 6, 16, 7.0, deadband=0.05, step=0.1) == 6.9


def test_finer_step_decide_writes_the_fine_value():
    d = decide(6.83, 6, 16, None, None, T0, 120, None, deadband=0.05, step=0.1)
    assert d.write_value == 6.7 and d.effective == 6.7


# --- ramp-up cap and post-cut hold (0.7) -------------------------------------


def test_decide_caps_upward_step():
    # wants 11.5 from 6.0; capped at 6.0 + 2.0 = 8.0 on this write
    d = decide(11.8, 6, 16, 6.0, T0, T0 + timedelta(seconds=200), 120, 6.0, max_step_up=2.0)
    assert d.write_value == 8.0 and d.effective == 8.0


def test_decide_cap_quantises_to_the_step():
    # cap 1.25 with a 0.5 step lands on the increment below: 6.0 + 1.0
    d = decide(11.8, 6, 16, 6.0, T0, T0 + timedelta(seconds=200), 120, 6.0, max_step_up=1.25)
    assert d.write_value == 7.0


def test_decide_cap_never_limits_a_downward_write():
    d = decide(6.0, 6, 16, 12.0, T0, T0 + timedelta(seconds=200), 120, 12.0, max_step_up=2.0)
    assert d.write_value == 6.0


def test_decide_cap_does_not_apply_to_the_first_write():
    d = decide(11.8, 6, 16, None, None, T0, 120, None, max_step_up=2.0)
    assert d.write_value == 11.5

