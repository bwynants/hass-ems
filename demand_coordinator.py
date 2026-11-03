"""Quarterly (15-minute) demand coordinator for the capacity-tariff feature.

Ports the logic of the ``peakpower.yaml`` template package into a native,
fast-refreshing coordinator. All values are expressed in kW. Source readings
are taken from user-selected sensors and normalised to kW so any W/kW source
works; the three threshold values come from plain numeric config fields.
"""
from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .helpers import read_kw
from .ems_core.demand import compute_demand
from .ems_core.demand_alerts import DemandAlertState, resolve_quarter, step_alerts
from .const import (
    DOMAIN,
    CONF_DEMAND_POWER_CURRENT_PEAK_SENSOR,
    CONF_DEMAND_POWER_CONSUMED_SENSOR,
    CONF_DEMAND_MONTH_MAX_SENSOR,
    CONF_MINIMAL_PEAK_WARNING,
    CONF_PEAK_HIGH_SAFETY,
    CONF_PEAK_CRITICAL_SAFETY,
    DEFAULT_MINIMAL_PEAK_WARNING,
    DEFAULT_PEAK_HIGH_SAFETY,
    DEFAULT_PEAK_CRITICAL_SAFETY,
    DEMAND_UPDATE_INTERVAL_SECONDS,
    DEMAND_STALE_SECONDS,
    DEMAND_QUARTER_SECONDS,
    DEMAND_HIGH_ALERT_MINUTES,
    DEMAND_CRITICAL_ALERT_MINUTES,
    DEMAND_HIGH_ALERT_SUSTAIN_CYCLES,
    DEMAND_CRITICAL_ALERT_SUSTAIN_CYCLES,
)

_LOGGER = logging.getLogger(__name__)


class QuarterlyDemandCoordinator(DataUpdateCoordinator):
    """Compute the quarter-hour demand metrics on a fast (~10s) cycle."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_demand",
            config_entry=entry,
            update_interval=timedelta(seconds=DEMAND_UPDATE_INTERVAL_SECONDS),
        )
        self.entry = entry
        self._high_on = False
        self._critical_on = False
        # The month-max value before its most recent change (the "was X kW"),
        # so a new monthly peak can be reported against the previous one.
        # Persisted so it survives restarts.
        self._store = Store(hass, 1, f"{DOMAIN}_demand_{entry.entry_id}")
        self._month_max_value: float | None = None
        self._previous_month_max: float | None = None
        # Armed at each quarter boundary; the "previous month max" baseline is
        # snapshotted once, ~1 min into the quarter (see the persist block).
        self._quarter_snapshot_armed = False
        # Once-per-quarter announcement policy (ems_core.demand_alerts). The
        # per-quarter flags are persisted with the month max so an EMS reload
        # mid-quarter doesn't repeat a warning. Emitted alerts are published in
        # ``data["alerts"]`` with a monotonically increasing ``seq`` the event
        # entity uses to fire each one exactly once.
        self._alert_state = DemandAlertState()
        self._alert_seq = 0
        # Set by the control coordinator whenever it throttles or sheds for
        # peak: until this instant the High/Critical announcement is held, so
        # a risk EMS is already removing is only announced if it outlives
        # the action.
        self.peak_action_until = None
        # Last known good readings, held when a source sensor is momentarily
        # unavailable: a dropout must not zero the estimate (which would
        # release the Critical latch mid-peak) or collapse the month max to
        # the minimal floor (false Critical + Store churn).
        self._last_current_avg = 0.0
        self._last_power_consumed = 0.0
        self._last_month_max: float | None = None
        # Consecutive cycles a configured source has been unavailable. Past
        # DEMAND_STALE_SECONDS the feature reports itself unavailable rather
        # than acting on frozen data.
        self._stale_cycles = 0
        # True once BOTH required sources have really reported in this runtime.
        # Until then the derived limits rest on the minimal-peak fallback, and
        # the optimizer's peak cap bypasses on/off hysteresis entirely — acting
        # on a fallback limit there would switch a running appliance straight
        # off at startup. Sticky: a later dropout is bridged by the held values.
        self._sourced_once = False

    async def async_load(self) -> None:
        """Load the persisted month-max history (call once before first refresh)."""
        data = await self._store.async_load()
        if not data:
            return

        def _f(v):
            try:
                return float(v) if v is not None else None
            except (ValueError, TypeError):
                return None

        self._month_max_value = _f(data.get("month_max_value"))
        self._previous_month_max = _f(data.get("previous_month_max"))
        self._alert_state = DemandAlertState.from_dict(data.get("alert_state"))

    @property
    def feature_active(self) -> bool:
        """True when the sources are configured and have genuinely reported.

        ``sourced`` is what keeps the main coordinator's peak control off until
        the limits are backed by real readings rather than the minimal-peak
        fallback. Sensor availability still keys off ``configured`` alone.
        """
        return bool(self.data and self.data.get("configured") and self.data.get("sourced"))

    def _read_kw(self, entity_id: str | None) -> float | None:
        """Read a sensor value in kW, or None if unavailable."""
        return read_kw(self.hass, entity_id)

    def _inactive(self) -> dict:
        """Return the data shape used when the feature is not configured."""
        self._high_on = False
        self._critical_on = False
        return {
            "configured": False,
            "sourced": False,
            "estimate": None,
            "month_max": None,
            "high_limit": None,
            "critical_limit": None,
            "max_power_to_peak": None,
            "high": False,
            "critical": False,
            "previous_month_max": None,
            "alerts": [],
            "so_far": None,
            "seconds_remaining": None,
            "quarter_id": None,
            "peak_action": False,
        }

    def _emit_alert(self, alerts: list[dict], alert_type: str, **attrs) -> None:
        """Append one alert to this cycle's list and log it."""
        self._alert_seq += 1
        alerts.append({"seq": self._alert_seq, "type": alert_type, **attrs})
        _LOGGER.info("demand alert: %s %s", alert_type, attrs)

    async def _async_update_data(self) -> dict:
        config = {**self.entry.data, **self.entry.options}
        current_avg_id = config.get(CONF_DEMAND_POWER_CURRENT_PEAK_SENSOR)
        month_max_id = config.get(CONF_DEMAND_MONTH_MAX_SENSOR)
        power_consumed_id = config.get(CONF_DEMAND_POWER_CONSUMED_SENSOR)

        # The feature needs at least the current-quarter average and the
        # monthly-max source; the power-consumed source is optional.
        if not current_avg_id or not month_max_id:
            # Also forget the held readings so a later reconfigure starts fresh.
            self._last_current_avg = 0.0
            self._last_power_consumed = 0.0
            self._last_month_max = None
            self._stale_cycles = 0
            self._sourced_once = False
            return self._inactive()

        now = dt_util.now()
        minute_in_quarter = now.minute % 15
        seconds_done = minute_in_quarter * 60 + now.second

        # Read the sources, holding the last known value on a dropout.
        current_raw = self._read_kw(current_avg_id)
        if current_raw is not None:
            self._last_current_avg = current_raw
        consumed_raw = self._read_kw(power_consumed_id)
        if consumed_raw is not None:
            self._last_power_consumed = consumed_raw
        month_raw = self._read_kw(month_max_id)
        if month_raw is not None:
            self._last_month_max = month_raw
        if not self._sourced_once and current_raw is not None and month_raw is not None:
            self._sourced_once = True
            _LOGGER.debug("demand sources reported; peak control now active")

        # Held values may only bridge SHORT dropouts. Past the stale limit,
        # report the feature unavailable instead of acting on frozen data
        # (a frozen estimate would re-latch Critical every quarter; a frozen
        # headroom would strangle the optimizer indefinitely).
        any_missing = (
            current_raw is None
            or month_raw is None
            or (bool(power_consumed_id) and consumed_raw is None)
        )
        self._stale_cycles = self._stale_cycles + 1 if any_missing else 0
        stale_limit = max(1, DEMAND_STALE_SECONDS // DEMAND_UPDATE_INTERVAL_SECONDS)
        if self._stale_cycles > stale_limit:
            if self._stale_cycles == stale_limit + 1:
                _LOGGER.warning(
                    "demand sources unavailable for over %d s; "
                    "quarterly-demand feature reporting unavailable",
                    DEMAND_STALE_SECONDS,
                )
            return self._inactive()

        current_so_far = self._last_current_avg if seconds_done > 0 else 0.0
        power_consumed = self._last_power_consumed
        # If the month-max sensor has never reported (e.g. unavailable at
        # startup), fall back to the persisted value instead of 0.
        month_max_src = (
            self._last_month_max
            if self._last_month_max is not None
            else (self._month_max_value or 0.0)
        )

        minimal = float(config.get(CONF_MINIMAL_PEAK_WARNING, DEFAULT_MINIMAL_PEAK_WARNING))
        high_safety = float(config.get(CONF_PEAK_HIGH_SAFETY, DEFAULT_PEAK_HIGH_SAFETY))
        crit_safety = float(config.get(CONF_PEAK_CRITICAL_SAFETY, DEFAULT_PEAK_CRITICAL_SAFETY))

        # Pure quarter-hour math lives in the core library.
        r = compute_demand(
            current_so_far=current_so_far,
            power_consumed=power_consumed,
            month_max_src=month_max_src,
            minimal=minimal,
            high_safety=high_safety,
            crit_safety=crit_safety,
            seconds_done=seconds_done,
            minute_in_quarter=minute_in_quarter,
            high_on=self._high_on,
            critical_on=self._critical_on,
        )
        estimate = r.estimate
        month_max = r.month_max
        high_limit = r.high_limit
        critical_limit = r.critical_limit
        max_power_to_peak = r.max_power_to_peak
        high = r.high
        critical = r.critical
        # high/critical are the new latch state; carry them into the next cycle.
        self._high_on = r.high
        self._critical_on = r.critical

        # Threshold alerts: at most one High and one Critical per quarter. The
        # quarter id is wall-clock based (epoch // 900 lines up with :00/:15/
        # :30/:45 for whole-quarter UTC offsets), so a restart lands in the
        # same quarter the persisted flags belong to.
        alerts: list[dict] = []
        quarter_id = int(now.timestamp() // DEMAND_QUARTER_SECONDS)
        peak_action = self.peak_action_until is not None and now < self.peak_action_until
        alert_state, alert_types = step_alerts(
            self._alert_state, quarter_id=quarter_id, high=high, critical=critical,
            minute_in_quarter=minute_in_quarter,
            hold=peak_action,
            high_sustain=DEMAND_HIGH_ALERT_SUSTAIN_CYCLES,
            critical_sustain=DEMAND_CRITICAL_ALERT_SUSTAIN_CYCLES,
            high_alert_minute=DEMAND_HIGH_ALERT_MINUTES,
            critical_alert_minute=DEMAND_CRITICAL_ALERT_MINUTES,
        )
        for alert_type in alert_types:
            self._emit_alert(
                alerts, alert_type,
                month_max=round(month_max, 2), estimate=round(estimate, 2),
                previous_peak=None, new_peak=None,
            )

        # Persist the month-max (used as the source fallback above, so it
        # survives a restart / startup dropout) and snapshot the "previous
        # month max" baseline. Only once the source has actually reported this
        # runtime — persisting the minimal-floor fallback would fabricate a
        # bogus peak.
        dirty = False
        if self._last_month_max is not None:
            if self._month_max_value is None or round(month_max, 2) != round(self._month_max_value, 2):
                self._month_max_value = month_max
                dirty = True

            # Snapshot the "previous month max" ONCE, ~1 minute into each
            # quarter: at that point month_max still reflects the peak from
            # BEFORE this quarter could set a new record, so a peak established
            # during the quarter is reported against the prior value. (Snapshot
            # once — not on every source change — so it doesn't chase a source
            # that climbs in small steps and end up equal to the new peak.)
            if seconds_done < 60:
                self._quarter_snapshot_armed = True
            elif self._quarter_snapshot_armed:
                self._quarter_snapshot_armed = False
                # The snapshot moment is also when the PREVIOUS quarter's
                # verdict is definitive: month_max now vs. the baseline taken
                # at its start says whether it set a record ("new monthly
                # peak") or merely scared us ("cleared"). Resolve before the
                # baseline moves on.
                alert_state, verdict_types = resolve_quarter(
                    alert_state, old_peak=self._previous_month_max, new_peak=month_max,
                )
                for alert_type in verdict_types:
                    self._emit_alert(
                        alerts, alert_type,
                        month_max=round(month_max, 2), estimate=round(estimate, 2),
                        previous_peak=(round(self._previous_month_max, 2)
                                       if self._previous_month_max is not None else None),
                        new_peak=round(month_max, 2),
                    )
                if (self._previous_month_max is None
                        or round(self._previous_month_max, 2) != round(month_max, 2)):
                    self._previous_month_max = month_max
                    dirty = True

        if alert_state != self._alert_state:
            self._alert_state = alert_state
            dirty = True

        if dirty:
            await self._store.async_save(
                {
                    "month_max_value": self._month_max_value,
                    "previous_month_max": self._previous_month_max,
                    "alert_state": self._alert_state.as_dict(),
                }
            )

        _LOGGER.debug(
            "demand: estimate=%.2f month_max=%.2f high_limit=%.2f crit_limit=%.2f "
            "max_to_peak=%.2f high=%s critical=%s (min_in_q=%d)",
            estimate, month_max, high_limit, critical_limit,
            max_power_to_peak, high, critical, minute_in_quarter,
        )

        return {
            "configured": True,
            "sourced": self._sourced_once,
            "estimate": round(estimate, 2),
            "month_max": round(month_max, 2),
            "high_limit": round(high_limit, 2),
            "critical_limit": round(critical_limit, 2),
            "max_power_to_peak": round(max_power_to_peak, 2),
            "high": high,
            "critical": critical,
            "previous_month_max": round(self._previous_month_max, 2) if self._previous_month_max is not None else None,
            "alerts": alerts,
            # The quarter as the optimizer projects it (see DemandSnapshot).
            "so_far": round(current_so_far, 3),
            "seconds_remaining": DEMAND_QUARTER_SECONDS - seconds_done,
            "quarter_id": quarter_id,
            "peak_action": peak_action,
        }
