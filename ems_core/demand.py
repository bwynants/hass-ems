"""Quarter-hour (capacity-tariff) demand math for the EMS core — pure."""
from __future__ import annotations

from .const import (
    DEMAND_QUARTER_SECONDS,
    DEMAND_MAX_POWER_CAP_KW,
    DEMAND_HYSTERESIS_KW,
    DEMAND_HIGH_GATE_MINUTES,
    DEMAND_CRITICAL_GATE_MINUTES,
)
from .models import DemandResult


def compute_demand(
    *,
    current_so_far: float,
    power_consumed: float,
    month_max_src: float,
    minimal: float,
    high_safety: float,
    crit_safety: float,
    seconds_done: int,
    minute_in_quarter: int,
    high_on: bool,
    critical_on: bool,
) -> DemandResult:
    """Project quarter-hour demand and derive High/Critical state.

    All power values are kW. ``current_so_far`` is the running average over the
    quarter so far (the shell zeroes it when ``seconds_done`` is 0);
    ``power_consumed`` is the current draw used to project to the quarter's end.
    ``high_on``/``critical_on`` are the previous latched flags (for hysteresis);
    the new latches are returned in the result.
    """
    seconds_remaining = max(DEMAND_QUARTER_SECONDS - seconds_done, 1)

    month_max = max(month_max_src, minimal)
    high_limit = max(month_max - high_safety, 0.0)
    critical_limit = max(month_max - crit_safety, 0.0)

    estimate = current_so_far + power_consumed * seconds_remaining / DEMAND_QUARTER_SECONDS

    power_to_consume = max(
        (high_limit - current_so_far) * DEMAND_QUARTER_SECONDS / seconds_remaining, 0.0
    )
    max_power_to_peak = min(power_to_consume, DEMAND_MAX_POWER_CAP_KW)

    # High / Critical binary states with a measurement-window gate and a kW
    # deadband to avoid flapping.
    if minute_in_quarter < DEMAND_HIGH_GATE_MINUTES:
        high = False
    elif high_on:
        high = estimate >= (high_limit - DEMAND_HYSTERESIS_KW)
    else:
        high = estimate >= high_limit

    if minute_in_quarter < DEMAND_CRITICAL_GATE_MINUTES:
        critical = False
    elif critical_on:
        critical = estimate >= (critical_limit - DEMAND_HYSTERESIS_KW)
    else:
        critical = estimate >= critical_limit

    return DemandResult(
        estimate=estimate,
        month_max=month_max,
        high_limit=high_limit,
        critical_limit=critical_limit,
        max_power_to_peak=max_power_to_peak,
        high=high,
        critical=critical,
        high_on=high,
        critical_on=critical,
    )
