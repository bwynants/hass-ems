"""fixed_amps setpoint stepping and write-gating — pure functions."""
from __future__ import annotations

import math
from datetime import datetime

from .const import FIXED_AMPS_DEADBAND, FIXED_AMPS_STEP
from .models import FixedAmpsDecision


def _quantise_down(value: float, step: float) -> float:
    """Largest multiple of ``step`` not exceeding ``value`` (fp-safe)."""
    return round(math.floor(round(value / step, 9)) * step, 6)


def step_target(
    scaled: float,
    min_a: float,
    max_a: float,
    last_val: float | None,
    deadband: float = FIXED_AMPS_DEADBAND,
    step: float = FIXED_AMPS_STEP,
) -> float:
    """Quantise ``scaled`` to a ``step``-amp setpoint with a deadband/hysteresis.

    Run at increment N only once N + deadband is available; once at N, hold it
    while the value stays within ``[N - db, N + step + db)``; step UP when it
    reaches ``(N + step) + db`` and step DOWN when it falls below ``N - db``,
    landing on the adjacent increment without skipping. With the defaults
    (step=0.5, db=0.25): 6.8 -> 6.5, holds through [6.25, 6.75), steps up to 7.0
    at 7.25 and down to 6.0 at 6.24. Full max is allowed once the budget reaches
    it. Result is clamped to ``[min_a, max_a]``.
    """
    db = deadband
    if scaled >= max_a:
        target = float(max_a)
    elif last_val is not None and (last_val - db) <= scaled < (last_val + step + db):
        target = last_val                              # within hysteresis band → hold
    elif last_val is None or scaled >= last_val + step + db:
        target = _quantise_down(scaled - db, step)     # step up (or first set)
    else:
        target = _quantise_down(scaled + db, step)     # step down to the adjacent increment
    return max(float(min_a), min(float(max_a), target))


def decide(
    scaled: float,
    min_a: float,
    max_a: float,
    last_val: float | None,
    last_change: datetime | None,
    now: datetime,
    interval: float,
    current_entity_value: float | None,
    deadband: float = FIXED_AMPS_DEADBAND,
    step: float = FIXED_AMPS_STEP,
) -> FixedAmpsDecision:
    """Decide the effective amperage and whether to write a new setpoint.

    Mirrors the old ``_apply_fixed_amps`` gating: the first setpoint is always
    written; an unchanged target whose entity has drifted is re-asserted; a
    changed target is written only once the hold window (``interval`` seconds)
    has elapsed; otherwise the setpoint is held (no write).
    """
    target = step_target(scaled, min_a, max_a, last_val, deadband, step)

    # First write: nothing to gate.
    if last_val is None:
        return FixedAmpsDecision(effective=target, write_value=target)

    if target == last_val:
        # Target unchanged. Skip only if the entity already holds it; if it has
        # drifted (e.g. set externally), re-assert instead of sticking stale.
        if current_entity_value is None or current_entity_value == target:
            return FixedAmpsDecision(effective=last_val, write_value=None)
        return FixedAmpsDecision(effective=target, write_value=target)

    # The value wants to change; only allow it once the window has elapsed.
    if last_change is None or (now - last_change).total_seconds() >= interval:
        return FixedAmpsDecision(effective=target, write_value=target)

    return FixedAmpsDecision(effective=last_val, write_value=None)
