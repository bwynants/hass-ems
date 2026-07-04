"""Tests for ems_core.demand — quarter-hour projection + High/Critical latch."""
from ems_core.demand import compute_demand


def _demand(**kw):
    base = dict(
        current_so_far=0.0, power_consumed=0.0, month_max_src=5.0,
        minimal=4.0, high_safety=0.4, crit_safety=0.2,
        seconds_done=0, minute_in_quarter=0, high_on=False, critical_on=False,
    )
    base.update(kw)
    return compute_demand(**base)


def test_month_max_floor():
    # month_max_src below minimal -> floored to minimal
    r = _demand(month_max_src=1.0, minimal=4.0)
    assert r.month_max == 4.0
    assert r.high_limit == 4.0 - 0.4
    assert r.critical_limit == 4.0 - 0.2


def test_estimate_projection():
    # halfway through the quarter (450s done), 8 kW draw, nothing so far
    r = _demand(current_so_far=0.0, power_consumed=8.0, seconds_done=450, minute_in_quarter=7)
    # remaining = 450s ; estimate = 0 + 8 * 450/900 = 4.0
    assert round(r.estimate, 3) == 4.0


def test_high_gate_suppresses_before_6_min():
    # estimate way over the limit but only 5 min into the quarter -> not High
    r = _demand(month_max_src=5.0, current_so_far=10.0, power_consumed=10.0,
                seconds_done=300, minute_in_quarter=5)
    assert r.high is False


def test_high_triggers_after_gate():
    r = _demand(month_max_src=5.0, current_so_far=10.0, power_consumed=10.0,
                seconds_done=360, minute_in_quarter=6)
    assert r.high is True


def test_high_hysteresis_latch():
    # limit = 5 - 0.4 = 4.6; estimate 4.5 is below limit but within 0.2 deadband
    # once already latched on, it stays on.
    off = _demand(month_max_src=5.0, current_so_far=4.5, power_consumed=0.0,
                  seconds_done=600, minute_in_quarter=10, high_on=False)
    assert off.high is False   # 4.5 < 4.6 and not latched
    on = _demand(month_max_src=5.0, current_so_far=4.5, power_consumed=0.0,
                 seconds_done=600, minute_in_quarter=10, high_on=True)
    assert on.high is True     # latched: 4.5 >= 4.6 - 0.2


def test_max_power_to_peak_cap():
    # huge headroom is capped at 40 kW
    r = _demand(month_max_src=100.0, current_so_far=0.0, seconds_done=1,
                minute_in_quarter=0)
    assert r.max_power_to_peak == 40.0
