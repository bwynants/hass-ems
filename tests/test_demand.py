"""Tests for ems_core.demand — quarter-hour projection + High/Critical latch."""
from ems_core.demand import compute_demand, rest_of_quarter_kw


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


# --- rest-of-quarter projection (the proactive ceiling) ---------------------


def test_rest_of_quarter_kw_basic():
    # 2.6 kW banked, 300 s left, aim 3.3 -> (3.3 - 2.6) * 900 / 300 = 2.1 kW
    assert round(rest_of_quarter_kw(2.6, 300, 3.3), 3) == 2.1
    # 60 s in, having drawn exactly the aim so far: the aim itself remains
    assert round(rest_of_quarter_kw(3.3 * 60 / 900, 840, 3.3), 3) == 3.3
    # quarter already over the aim: nothing may be drawn, never negative
    assert rest_of_quarter_kw(3.5, 300, 3.3) == 0.0


def test_max_power_to_peak_uses_the_same_projection():
    r = _demand(month_max_src=4.0, current_so_far=2.6, seconds_done=600, minute_in_quarter=10)
    # high limit 3.6: (3.6 - 2.6) * 900 / 300 = 3.0 kW
    assert round(r.max_power_to_peak, 3) == 3.0


def test_projected_ceiling_aims_at_the_shed_limit():
    r = _demand(month_max_src=4.0, current_so_far=2.6, seconds_done=600,
                minute_in_quarter=10, shed_safety=0.3)
    assert round(r.shed_limit, 3) == 3.3
    assert round(r.projected_ceiling, 3) == 2.1
    # no shed safety: the ceiling equals the High-limit projection
    r = _demand(month_max_src=4.0, current_so_far=2.6, seconds_done=600, minute_in_quarter=10)
    assert round(r.projected_ceiling, 3) == 3.0


def test_projected_ceiling_skipped_in_the_first_minute():
    # the source may still hold last quarter's value: no projection yet
    stale = _demand(month_max_src=4.0, current_so_far=3.0, seconds_done=10,
                    minute_in_quarter=0, shed_safety=0.3)
    assert stale.projected_ceiling is None
    ok = _demand(month_max_src=4.0, current_so_far=0.2, seconds_done=60,
                 minute_in_quarter=1, shed_safety=0.3)
    assert ok.projected_ceiling is not None


# --- High is gated while the controller still has load to shed (0.8) --------

def test_high_not_latched_while_a_managed_appliance_draws():
    # Estimate far over the High limit past the gate minute, but an appliance
    # is still on: the controller cuts or sheds it first, High stays off.
    r = _demand(current_so_far=3.0, power_consumed=6.0, seconds_done=420,
                minute_in_quarter=7, sheddable_on=True)
    assert r.estimate >= r.high_limit
    assert r.high is False


def test_high_latches_the_cycle_after_the_appliance_is_off():
    kw = dict(current_so_far=3.0, power_consumed=6.0, seconds_done=420, minute_in_quarter=7)
    assert _demand(sheddable_on=True, **kw).high is False
    assert _demand(sheddable_on=False, **kw).high is True


def test_latched_high_releases_when_an_appliance_starts():
    # A latched High holds while nothing is sheddable; a managed appliance
    # drawing again means there is load to shed, so it releases.
    kw = dict(current_so_far=3.0, power_consumed=6.0, seconds_done=420, minute_in_quarter=7)
    assert _demand(high_on=True, sheddable_on=False, **kw).high is True
    assert _demand(high_on=True, sheddable_on=True, **kw).high is False


def test_critical_is_held_back_while_high_is_gated():
    # Critical never stands without High: with an appliance still running
    # neither latches, however far over the limits the estimate is. Both
    # latch the cycle after the appliance is off.
    kw = dict(current_so_far=3.0, power_consumed=6.0, seconds_done=540, minute_in_quarter=9)
    r = _demand(sheddable_on=True, **kw)
    assert r.estimate >= r.critical_limit
    assert r.high is False and r.critical is False
    r = _demand(sheddable_on=False, **kw)
    assert r.high is True and r.critical is True


def test_latched_critical_releases_when_an_appliance_starts():
    kw = dict(current_so_far=3.0, power_consumed=6.0, seconds_done=540, minute_in_quarter=9)
    r = _demand(high_on=True, critical_on=True, sheddable_on=True, **kw)
    assert r.high is False and r.critical is False


def test_critical_always_implies_high():
    # Across the quarter, both gate states and both latch histories.
    for sec in range(0, 900, 30):
        for sheddable in (False, True):
            for high_on in (False, True):
                for critical_on in (False, True):
                    for consumed in (0.0, 4.3, 4.5, 4.7, 9.0):
                        r = _demand(current_so_far=4.5 * sec / 900, power_consumed=consumed,
                                    seconds_done=sec, minute_in_quarter=sec // 60,
                                    sheddable_on=sheddable, high_on=high_on,
                                    critical_on=critical_on)
                        assert r.high or not r.critical
                        r = _demand(current_so_far=4.5 * sec / 900, power_consumed=consumed,
                                    seconds_done=sec, minute_in_quarter=sec // 60,
                                    sheddable_on=sheddable, high_on=high_on,
                                    critical_on=critical_on, held_seconds=120.0)
                        assert r.high or not r.critical


def test_critical_hold_back_lapses_after_the_time_limit():
    # The optimizer should have shed long ago. If the estimate has been over
    # the Critical limit with an appliance still running for the full limit,
    # the gate lapses: High and Critical latch together (backstop sheds).
    from ems_core.const import CRITICAL_HOLD_BACK_SECONDS
    kw = dict(current_so_far=3.0, power_consumed=6.0, seconds_done=600,
              minute_in_quarter=10, sheddable_on=True)
    r = _demand(held_seconds=0.0, **kw)
    assert r.critical_held is True
    assert r.high is False and r.critical is False
    r = _demand(held_seconds=CRITICAL_HOLD_BACK_SECONDS - 10, **kw)
    assert r.high is False and r.critical is False
    r = _demand(held_seconds=CRITICAL_HOLD_BACK_SECONDS, **kw)
    assert r.high is True and r.critical is True
    # Still reported as held, so the shell's timer keeps running and the
    # flags don't drop back while the appliance is being shed.
    assert r.critical_held is True


def test_high_alone_is_held_back_without_a_time_limit():
    # Over High but under Critical with an appliance running: nothing is
    # pending behind the gate, the timer never starts, High stays off.
    r = _demand(current_so_far=2.0, power_consumed=8.1, seconds_done=600,
                minute_in_quarter=10, sheddable_on=True, held_seconds=600.0)
    assert r.high_limit <= r.estimate < r.critical_limit
    assert r.critical_held is False
    assert r.high is False and r.critical is False


def test_no_hold_without_a_running_appliance():
    r = _demand(current_so_far=3.0, power_consumed=6.0, seconds_done=600, minute_in_quarter=10)
    assert r.critical is True and r.critical_held is False


def test_base_load_only_peak_still_latches_high():
    # Nothing sheddable (a hob-only peak): unchanged behaviour.
    r = _demand(current_so_far=3.0, power_consumed=6.0, seconds_done=480, minute_in_quarter=8)
    assert r.high is True
