"""Tests for ems_core.optimizer — allocation, throttling, SoC, hysteresis, shed."""
from datetime import datetime, timedelta

from ems_core.optimizer import optimize, critical_shed
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
    # 8 down-with-margin to 7 (run at 7 until 8.5 available).
    assert res.fixed_amps_commands[0].value == 7.0


def test_peak_headroom_sheds_when_below_min():
    # headroom 3A < min 6A => appliance cannot run
    app = _app(mode=MODE_PARTIAL_SOLAR, min_amps=6, partial_min_excess=0)
    states = {}
    res = optimize([app], states, excess_amps=16.0, peak_amps=3.0,
                   energy=_energy(), battery=NO_BAT, now=T0)
    assert res.active == []


def test_priority_order_high_priority_claims_headroom_first():
    # two appliances, headroom only enough for one at min (6A); lower number wins
    a1 = _app(name="hi", priority=1, min_amps=6, mode=MODE_PARTIAL_SOLAR, partial_min_excess=0)
    a2 = _app(name="lo", priority=50, min_amps=6, mode=MODE_PARTIAL_SOLAR, partial_min_excess=0)
    states = {}
    res = optimize([a2, a1], states, excess_amps=6.0, peak_amps=6.0,
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


def test_full_solar_off_on_transient_spike():
    # Current budget covers (10 >= 6) but the sustained average does not (2 < 6)
    # -> full_solar must NOT commit (would import once the spike passes).
    app = _app(mode=MODE_FULL_SOLAR, min_amps=6, averaging_window_seconds=180)
    st = ApplianceState()
    st.excess_history.extend([2.0, 2.0, 2.0])
    states = {"a": st}
    res = optimize([app], states, excess_amps=10.0, peak_amps=None,
                   energy=_energy(), battery=NO_BAT, now=T0)
    assert res.active == []


def test_full_solar_on_when_sustained_and_current():
    # Both the sustained average and the current budget cover -> runs.
    app = _app(mode=MODE_FULL_SOLAR, min_amps=6, averaging_window_seconds=180)
    st = ApplianceState()
    st.excess_history.extend([8.0, 8.0, 8.0])
    states = {"a": st}
    res = optimize([app], states, excess_amps=8.0, peak_amps=None,
                   energy=_energy(), battery=NO_BAT, now=T0)
    assert res.active == ["a"]


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


def test_on_off_hysteresis_holds_recent_state():
    # full_solar so the run condition is purely "remaining >= min" (no averaging).
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
