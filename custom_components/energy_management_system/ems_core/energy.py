"""Energy/amps calculations for the EMS core — pure functions."""
from __future__ import annotations

from .const import DEFAULT_MIN_AMPS, MODE_UNMANAGED
from .models import ApplianceInput

# Multipliers to normalise a power reading to watts.
_POWER_UNIT_MULTIPLIERS: dict[str, float] = {
    "w": 1.0,
    "kw": 1000.0,
    "mw": 1_000_000.0,
}


def normalise_power(value: float, unit: str | None) -> float:
    """Convert a power reading to watts based on its unit_of_measurement."""
    if unit is None:
        return value
    return value * _POWER_UNIT_MULTIPLIERS.get(unit.lower().strip(), 1.0)


def calculate_excess_amps(
    grid_w: float | None, solar_w: float, house_w: float, voltage: float
) -> float:
    """Excess surplus in amps (signed).

    Prefers the measured net grid power (import positive, export negative) and
    returns it SIGNED: export → positive surplus, import → negative. It must stay
    signed so the managed-consumption add-back reconstructs the true gross
    surplus (solar − house). Falls back to the solar − house estimate when no
    grid reading is present. Returns 0 when voltage is unusable.
    """
    if voltage <= 0:
        return 0
    if grid_w is not None:
        return -grid_w / voltage
    return (solar_w - house_w) / voltage


def kw_to_amps(kw: float | None, voltage: float) -> float | None:
    """A power in kW as amps at ``voltage``; None when either is unusable."""
    if kw is None or voltage <= 0:
        return None
    return kw * 1000.0 / voltage


def scaled_amps(min_a: float, max_a: float, remaining_amps: float) -> float:
    """Scale amperage to the available excess, clamped between min and max."""
    if remaining_amps >= max_a:
        return float(max_a)
    if remaining_amps <= min_a:
        return float(min_a)
    return float(remaining_amps)


def breakeven(min_amps, cost_peak, cost_offpeak, gain_export):
    """Averaged excess (amps) above which running at peak beats waiting for off-peak.

    ``gain_export`` is the export compensation that applies NOW (the solar the
    appliance would self-consume is otherwise exported at that price), i.e.
    ``EnergyInput.gain_now`` — the peak-period rate, since this gate only
    runs during peak hours.

    Fails CLOSED on a degenerate or negative denominator (export gain at or
    above the peak cost): running at peak can then never beat waiting, so the
    threshold saturates to infinity instead of 0 ("always run").
    """
    denom = cost_peak - gain_export
    if denom > 0.001:
        return min_amps * (cost_peak - cost_offpeak) / denom
    return float("inf")


def actual_amps(app: ApplianceInput) -> float:
    """The appliance's effective draw in amps (never None), for the surplus add-back.

    Prefers the measured power sensor. If a power sensor is configured but
    currently unavailable, falls back to ``min_amps`` — deliberately NOT the
    fixed_amps setpoint: the setpoint is the optimizer's own command, so adding
    it back into the surplus would create a positive-feedback runaway (a stale
    measurement makes add-back = setpoint → higher budget → higher setpoint →
    …, ramping to max). Only when there is NO power sensor does it use the
    setpoint (there the grid reading is the sole feedback, and a healthy grid
    keeps the add-back honest). Falls back to ``min_amps`` last.
    """
    if app.measured_amps is not None:
        return app.measured_amps
    if app.has_power_sensor:
        return app.min_amps if app.min_amps is not None else DEFAULT_MIN_AMPS
    if app.has_fixed_amps and app.fixed_amps_value is not None:
        return app.fixed_amps_value
    return app.min_amps if app.min_amps is not None else DEFAULT_MIN_AMPS


def managed_consumption_amps(appliances: list[ApplianceInput]) -> float:
    """Sum the amps managed, switched-on appliances are currently drawing.

    Added back to the net grid-export budget so allocation works from the gross
    surplus. Unmanaged appliances are base load and excluded.
    """
    total = 0.0
    for app in appliances:
        if app.mode == MODE_UNMANAGED:
            continue
        if app.entity_on:
            total += actual_amps(app)
    return total
