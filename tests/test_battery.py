"""Tests for ems_core.battery — SoC/threshold/mode reducers."""
from ems_core.battery import battery_status
from ems_core.const import (
    MODE_PARTIAL_SOLAR, BATTERY_MODE_STRICT, BATTERY_MODE_RELAXED, DEFAULT_MIN_SOC,
)
from ems_core.models import ApplianceInput


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


def test_no_battery_returns_none():
    st = battery_status([_app(name="load")])
    assert st.soc is None and st.min_soc is None and st.protection_mode is None


def test_single_battery():
    st = battery_status([
        _app(name="bat", is_battery=True, battery_soc=42, battery_min_soc=20,
             battery_protection_mode=BATTERY_MODE_RELAXED),
        _app(name="load"),
    ])
    assert st.soc == 42
    assert st.min_soc == 20
    assert st.protection_mode == BATTERY_MODE_RELAXED


def test_multi_battery_conservative():
    st = battery_status([
        _app(name="b1", is_battery=True, battery_soc=55, battery_min_soc=20,
             battery_protection_mode=BATTERY_MODE_RELAXED),
        _app(name="b2", is_battery=True, battery_soc=30, battery_min_soc=25,
             battery_protection_mode=BATTERY_MODE_STRICT),
    ])
    assert st.soc == 30           # lowest SoC
    assert st.min_soc == 25       # highest threshold
    assert st.protection_mode == BATTERY_MODE_STRICT  # strictest wins


def test_battery_missing_threshold_uses_default():
    st = battery_status([_app(name="bat", is_battery=True, battery_soc=50)])
    assert st.min_soc == DEFAULT_MIN_SOC


def test_battery_without_soc_reading():
    st = battery_status([_app(name="bat", is_battery=True, battery_min_soc=20)])
    assert st.soc is None
    assert st.min_soc == 20
