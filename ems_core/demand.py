"""Quarter-hour (capacity-tariff) demand math for the EMS core — pure."""
from __future__ import annotations

from .const import (
    DEMAND_QUARTER_SECONDS,
    DEMAND_MAX_POWER_CAP_KW,
    DEMAND_HYSTERESIS_KW,
    DEMAND_HIGH_GATE_MINUTES,
    DEMAND_CRITICAL_GATE_MINUTES,
    DEMAND_PROJECTION_SKIP_SECONDS,
    CRITICAL_HOLD_BACK_SECONDS,
)
from .models import DemandResult


def rest_of_quarter_kw(so_far_kw: float, seconds_remaining: int, limit_kw: float) -> float:
    """Average power the house may draw from now to the quarter's end so the
    quarter ends at ``limit_kw``; never negative."""
    remaining = max(seconds_remaining, 1)
    return max((limit_kw - so_far_kw) * DEMAND_QUARTER_SECONDS / remaining, 0.0)


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
    shed_safety: float = 0.0,
    sheddable_on: bool = False,
    held_seconds: float = 0.0,
) -> DemandResult:
    """Project quarter-hour demand and derive High/Critical state.

    All power values are kW. ``current_so_far`` is the running average over the
    quarter so far (the shell zeroes it when ``seconds_done`` is 0);
    ``power_consumed`` is the current draw used to project to the quarter's end.
    ``high_on``/``critical_on`` are the previous latched flags (for hysteresis);
    the returned ``high``/``critical`` are the new latch state. ``shed_safety``
    (kW under the High limit) sets the shed limit the projected ceiling aims
    for.

    ``sheddable_on`` is True while the controller still has a managed
    appliance drawing power. High then never latches (and a latched High is
    released): High means "a peak risk EMS cannot remove itself", and as long
    as there is load to cut or shed the controller does that instead. Once
    nothing sheddable is left the flag latches on the next cycle if the
    estimate is still at the limit. Critical is held back with it: it never
    stands without High, so it cannot latch either while an appliance is
    still running. The optimizer's own peak gate (projected ceiling, throttle
    to min, shed) is then what removes the load; the Critical backstop only
    comes into play once the controller has nothing active left.

    That hold-back is bounded. ``held_seconds`` is how long the estimate has
    been over the Critical limit with an appliance still running (the shell
    times the returned ``critical_held``). Past CRITICAL_HOLD_BACK_SECONDS
    the optimizer evidently is not shedding — its grid reading disagrees with
    the demand side — so the gate lapses: High and Critical latch together
    and the backstop sheds.
    """
    seconds_remaining = max(DEMAND_QUARTER_SECONDS - seconds_done, 1)

    month_max = max(month_max_src, minimal)
    high_limit = max(month_max - high_safety, 0.0)
    critical_limit = max(month_max - crit_safety, 0.0)
    shed_limit = max(high_limit - shed_safety, 0.0)

    estimate = current_so_far + power_consumed * seconds_remaining / DEMAND_QUARTER_SECONDS

    power_to_consume = rest_of_quarter_kw(current_so_far, seconds_remaining, high_limit)
    max_power_to_peak = min(power_to_consume, DEMAND_MAX_POWER_CAP_KW)

    # The ceiling the optimizer scales against for the rest of the quarter
    # (see optimizer.optimize). None in the first DEMAND_PROJECTION_SKIP_SECONDS
    # of a quarter: the current-average source lags the boundary by a few
    # seconds, and the finished quarter's value with ~900 s remaining projects
    # a ceiling that is nonsense in either direction.
    projected_ceiling = (
        None if seconds_done < DEMAND_PROJECTION_SKIP_SECONDS
        else rest_of_quarter_kw(current_so_far, seconds_remaining, shed_limit)
    )

    # High / Critical binary states with a measurement-window gate and a kW
    # deadband to avoid flapping.
    if minute_in_quarter < DEMAND_HIGH_GATE_MINUTES:
        high = False
    elif high_on:
        high = estimate >= (high_limit - DEMAND_HYSTERESIS_KW)
    else:
        high = estimate >= high_limit

    # Critical never stands without High (the limits and gate minutes are
    # config and constants; this keeps the order whatever they are).
    if minute_in_quarter < DEMAND_CRITICAL_GATE_MINUTES or not high:
        critical = False
    elif critical_on:
        critical = estimate >= (critical_limit - DEMAND_HYSTERESIS_KW)
    else:
        critical = estimate >= critical_limit

    # The appliance gate: while the controller still has load to shed, both
    # flags are held back — unless Critical has been pending behind the gate
    # for too long, which means the controller is not shedding.
    critical_held = sheddable_on and critical
    if sheddable_on and not (critical and held_seconds >= CRITICAL_HOLD_BACK_SECONDS):
        high = False
        critical = False

    return DemandResult(
        estimate=estimate,
        month_max=month_max,
        high_limit=high_limit,
        critical_limit=critical_limit,
        max_power_to_peak=max_power_to_peak,
        high=high,
        critical=critical,
        critical_held=critical_held,
        shed_limit=shed_limit,
        projected_ceiling=projected_ceiling,
    )
