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

    @property
    def feature_active(self) -> bool:
        """True when the demand sources are configured and data is available."""
        return bool(self.data and self.data.get("configured"))

    def _read_kw(self, entity_id: str | None) -> float | None:
        """Read a sensor value in kW, or None if unavailable."""
        return read_kw(self.hass, entity_id)

    def _inactive(self) -> dict:
        """Return the data shape used when the feature is not configured."""
        self._high_on = False
        self._critical_on = False
        return {
            "configured": False,
            "estimate": None,
            "month_max": None,
            "high_limit": None,
            "critical_limit": None,
            "max_power_to_peak": None,
            "high": False,
            "critical": False,
            "previous_month_max": None,
        }

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

        # Retain the previous month-max whenever the value changes, so a new
        # peak can be reported against what it was before. Persisted on change —
        # but only once the source has actually reported this runtime: persisting
        # the minimal-floor fallback would fabricate a bogus "previous peak".
        if self._last_month_max is not None:
            if self._month_max_value is None:
                self._month_max_value = month_max
                await self._store.async_save(
                    {"month_max_value": self._month_max_value, "previous_month_max": self._previous_month_max}
                )
            elif round(month_max, 2) != round(self._month_max_value, 2):
                self._previous_month_max = self._month_max_value
                self._month_max_value = month_max
                await self._store.async_save(
                    {"month_max_value": self._month_max_value, "previous_month_max": self._previous_month_max}
                )

        _LOGGER.debug(
            "demand: estimate=%.2f month_max=%.2f high_limit=%.2f crit_limit=%.2f "
            "max_to_peak=%.2f high=%s critical=%s (min_in_q=%d)",
            estimate, month_max, high_limit, critical_limit,
            max_power_to_peak, high, critical, minute_in_quarter,
        )

        return {
            "configured": True,
            "estimate": round(estimate, 2),
            "month_max": round(month_max, 2),
            "high_limit": round(high_limit, 2),
            "critical_limit": round(critical_limit, 2),
            "max_power_to_peak": round(max_power_to_peak, 2),
            "minute_in_quarter": minute_in_quarter,
            "high": high,
            "critical": critical,
            "previous_month_max": round(self._previous_month_max, 2) if self._previous_month_max is not None else None,
        }
