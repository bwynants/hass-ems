"""Whole-amp fixed_amps setpoint stepping and write-gating — pure functions."""
from __future__ import annotations

from datetime import datetime

from .const import FIXED_AMPS_DEADBAND
from .models import FixedAmpsDecision


def step_target(
    scaled: float,
    min_a: float,
    max_a: float,
    last_val: float | None,
    deadband: float = FIXED_AMPS_DEADBAND,
) -> float:
    """Quantise ``scaled`` to a whole-amp setpoint with a deadband/hysteresis.

    Run at N amps only once N + deadband is available; once at N, hold it while
    the value stays within ``[N - db, N + 1 + db)``; step UP when it reaches
    ``(N + 1) + db`` (e.g. 6 -> 7 at 7.5) and step DOWN when it falls below
    ``N - db`` (e.g. 6 -> 5 at 5.5), landing on the adjacent amp without
    skipping. Full max is allowed once the budget reaches it. Result is clamped
    to ``[min_a, max_a]``.
    """
    db = deadband
    if scaled >= max_a:
        target = float(max_a)
    elif last_val is not None and (last_val - db) <= scaled < (last_val + 1 + db):
        target = last_val                       # within hysteresis band → hold
    elif last_val is None or scaled >= last_val + 1 + db:
        target = float(int(scaled - db))        # step up (or first set)
    else:
        target = float(int(scaled + db))        # step down to the adjacent amp
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
) -> FixedAmpsDecision:
    """Decide the effective amperage and whether to write a new setpoint.

    Mirrors the old ``_apply_fixed_amps`` gating: the first setpoint is always
    written; an unchanged target whose entity has drifted is re-asserted; a
    changed target is written only once the hold window (``interval`` seconds)
    has elapsed; otherwise the setpoint is held (no write).
    """
    target = step_target(scaled, min_a, max_a, last_val, deadband)

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
