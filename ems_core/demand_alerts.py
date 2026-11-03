"""Quarter-hour peak alert policy for the EMS core — pure.

Decides *what to announce* about the capacity-tariff quarter, once, instead
of leaving it to automations that fire on every High/Critical transition:

* ``high`` — the first time High latches in a quarter while Critical is off.
* ``critical`` — the first time Critical latches in a quarter. It also marks
  High as announced: a Critical already says everything High would.
* ``new_month_peak`` / ``cleared`` — the quarter's *verdict*, resolved after
  the quarter has ended, once the month-max source is definitive (the shell
  calls :func:`resolve_quarter` at its existing "previous month max"
  snapshot, ~1 minute into the next quarter). ``new_month_peak`` is reported
  whenever the month maximum rose; ``cleared`` only when a warning went out
  during the quarter and no record was set. Never both.

So a single quarter yields at most one ``high``, one ``critical`` and one
closing message. A High that drops and comes back is silent.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

ALERT_HIGH = "high"
ALERT_CRITICAL = "critical"
ALERT_CLEARED = "cleared"
ALERT_NEW_MONTH_PEAK = "new_month_peak"

ALERT_TYPES = (ALERT_HIGH, ALERT_CRITICAL, ALERT_CLEARED, ALERT_NEW_MONTH_PEAK)


@dataclass(frozen=True)
class DemandAlertState:
    """Per-quarter announcement bookkeeping (plain data, persistable).

    ``quarter_id`` identifies the quarter the ``*_announced`` flags belong to
    (any integer that increases by exactly 1 per quarter, e.g.
    ``timestamp // 900``). ``pending_verdict`` is True while the *previous*
    quarter had a warning whose closing message is still owed.
    """
    quarter_id: int | None = None
    high_announced: bool = False
    critical_announced: bool = False
    pending_verdict: bool = False
    # Consecutive cycles the flag has been latched (the sustain counters).
    high_cycles: int = 0
    critical_cycles: int = 0

    def as_dict(self) -> dict:
        return {
            "quarter_id": self.quarter_id,
            "high_announced": self.high_announced,
            "critical_announced": self.critical_announced,
            "pending_verdict": self.pending_verdict,
            "high_cycles": self.high_cycles,
            "critical_cycles": self.critical_cycles,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "DemandAlertState":
        if not data:
            return cls()
        qid = data.get("quarter_id")
        try:
            qid = int(qid) if qid is not None else None
        except (TypeError, ValueError):
            qid = None
        return cls(
            quarter_id=qid,
            high_announced=bool(data.get("high_announced", False)),
            critical_announced=bool(data.get("critical_announced", False)),
            pending_verdict=bool(data.get("pending_verdict", False)),
            high_cycles=int(data.get("high_cycles", 0) or 0),
            critical_cycles=int(data.get("critical_cycles", 0) or 0),
        )


def step_alerts(
    state: DemandAlertState,
    *,
    quarter_id: int,
    high: bool,
    critical: bool,
    minute_in_quarter: int | None = None,
    hold: bool = False,
    high_sustain: int = 1,
    critical_sustain: int = 1,
    high_alert_minute: int = 0,
    critical_alert_minute: int = 0,
) -> tuple[DemandAlertState, list[str]]:
    """Advance the alert state by one control cycle.

    ``high``/``critical`` are the latched flags from :func:`compute_demand`.
    Returns the new state and the threshold alerts to emit this cycle (zero or
    one of ``high``/``critical``).

    An announcement additionally needs the flag latched for ``*_sustain``
    consecutive cycles, the quarter to have reached ``*_alert_minute`` (when
    ``minute_in_quarter`` is given), and ``hold`` to be False — the shell
    raises ``hold`` while the controller is throttling or shedding for peak,
    so a risk EMS is already removing is not announced; if it is still there
    once the hold lapses, it is announced then. The sustain counters keep
    running through a hold, so a lapsed hold announces at once. Flapping
    resets the counters.

    A quarter change rolls the finished quarter's "warned" status into
    ``pending_verdict`` — but only for the *directly* following quarter. After
    a gap (EMS was down, sources stale) the verdict would compare against a
    baseline that no longer belongs to that quarter, so it is dropped.
    """
    events: list[str] = []

    if quarter_id != state.quarter_id:
        warned = state.high_announced or state.critical_announced
        consecutive = state.quarter_id is not None and quarter_id == state.quarter_id + 1
        state = DemandAlertState(
            quarter_id=quarter_id,
            pending_verdict=bool(warned and consecutive),
        )

    state = replace(
        state,
        high_cycles=state.high_cycles + 1 if high else 0,
        critical_cycles=state.critical_cycles + 1 if critical else 0,
    )

    def _minute_ok(minute: int) -> bool:
        return minute_in_quarter is None or minute_in_quarter >= minute

    if (
        critical and not state.critical_announced
        and state.critical_cycles >= critical_sustain
        and _minute_ok(critical_alert_minute) and not hold
    ):
        events.append(ALERT_CRITICAL)
        state = replace(state, critical_announced=True, high_announced=True)
    elif (
        high and not state.high_announced
        and state.high_cycles >= high_sustain
        and _minute_ok(high_alert_minute) and not hold
    ):
        events.append(ALERT_HIGH)
        state = replace(state, high_announced=True)

    return state, events


def is_new_month_peak(old_peak: float | None, new_peak: float | None) -> bool:
    """True when the month maximum rose from ``old_peak`` to ``new_peak``.

    ``old_peak`` of None/0 is the first-ever population (or a lost baseline)
    and never counts; a month rollover LOWERS the value and never counts either.
    Compared at 2 decimals, matching the sensor precision.
    """
    if old_peak is None or new_peak is None or old_peak <= 0:
        return False
    return round(new_peak, 2) > round(old_peak, 2)


def resolve_quarter(
    state: DemandAlertState,
    *,
    old_peak: float | None,
    new_peak: float | None,
) -> tuple[DemandAlertState, list[str]]:
    """Close out the previous quarter once the month maximum is definitive.

    Call once per quarter, after the quarter has ended and the month-max
    source has caught up. Emits ``new_month_peak`` when the maximum rose
    (whether or not a warning preceded it), otherwise ``cleared`` if a warning
    is still owed its all-clear. Clears ``pending_verdict`` either way.
    """
    events: list[str] = []
    if is_new_month_peak(old_peak, new_peak):
        events.append(ALERT_NEW_MONTH_PEAK)
    elif state.pending_verdict:
        events.append(ALERT_CLEARED)
    if state.pending_verdict:
        state = replace(state, pending_verdict=False)
    return state, events
