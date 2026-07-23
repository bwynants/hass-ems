"""Tests for ems_core.energy — excess amps, scaling, breakeven, draw."""
from ems_core.energy import (
    normalise_power,
    calculate_excess_amps,
    scaled_amps,
    breakeven,
    actual_amps,
    managed_consumption_amps,
)
from ems_core.const import MODE_PARTIAL_SOLAR, MODE_UNMANAGED
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


def test_normalise_power_units():
    assert normalise_power(1.5, "kW") == 1500.0
    assert normalise_power(2000, "W") == 2000.0
    assert normalise_power(1, "MW") == 1_000_000.0
    assert normalise_power(500, None) == 500.0
    assert normalise_power(500, "weird") == 500.0  # unknown unit → ×1


def test_excess_amps_export_is_positive():
    # grid exporting 2300 W (negative) at 230 V => +10 A surplus
    assert calculate_excess_amps(-2300, 0, 0, 230) == 10.0


def test_excess_amps_import_is_negative():
    # grid importing 2300 W (positive) => -10 A (signed, not clamped)
    assert calculate_excess_amps(2300, 0, 0, 230) == -10.0


def test_excess_amps_fallback_solar_minus_house():
    assert calculate_excess_amps(None, 3000, 700, 230) == (3000 - 700) / 230


def test_excess_amps_zero_voltage():
    assert calculate_excess_amps(-2300, 0, 0, 0) == 0


def test_scaled_amps_clamps():
    assert scaled_amps(6, 16, 20) == 16.0   # above max
    assert scaled_amps(6, 16, 3) == 6.0     # below min
    assert scaled_amps(6, 16, 10) == 10.0   # in range


def test_breakeven():
    # 6 * (0.40-0.20)/(0.40-0.10) = 6 * 0.2/0.3 = 4.0
    assert breakeven(6, 0.40, 0.20, 0.10) == 4.0
    # Degenerate / negative denominator (export gain >= peak cost) fails
    # CLOSED: running at peak can never beat waiting, threshold saturates.
    assert breakeven(6, 0.10, 0.20, 0.10) == float("inf")
    assert breakeven(6, 0.10, 0.20, 0.20) == float("inf")


def test_actual_amps_priority_measured_then_fixed_then_min():
    assert actual_amps(_app(measured_amps=9.0, has_fixed_amps=True, fixed_amps_value=7)) == 9.0
    assert actual_amps(_app(has_fixed_amps=True, fixed_amps_value=7)) == 7
    assert actual_amps(_app(min_amps=6)) == 6


def test_actual_amps_stale_power_sensor_uses_min_not_setpoint():
    # A power sensor is configured but currently unavailable (measured=None).
    # The add-back must fall to min, NOT the fixed_amps setpoint — using the
    # optimizer's own commanded setpoint here creates a positive-feedback
    # runaway (add-back = setpoint -> higher budget -> higher setpoint -> ...).
    app = _app(has_power_sensor=True, measured_amps=None,
               has_fixed_amps=True, fixed_amps_value=16, min_amps=6)
    assert actual_amps(app) == 6
    # Without a power sensor, the setpoint is the only proxy and is still used.
    no_sensor = _app(has_power_sensor=False, measured_amps=None,
                     has_fixed_amps=True, fixed_amps_value=16, min_amps=6)
    assert actual_amps(no_sensor) == 16


def test_managed_consumption_sums_on_managed_only():
    apps = [
        _app(name="on", entity_on=True, has_fixed_amps=True, fixed_amps_value=8),
        _app(name="off", entity_on=False, has_fixed_amps=True, fixed_amps_value=8),
        _app(name="unmanaged", entity_on=True, mode=MODE_UNMANAGED, measured_amps=5),
    ]
    assert managed_consumption_amps(apps) == 8
