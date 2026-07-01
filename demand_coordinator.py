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
            return self._inactive()

        now = dt_util.now()
        minute_in_quarter = now.minute % 15
        seconds_done = minute_in_quarter * 60 + now.second

        current_so_far = (self._read_kw(current_avg_id) or 0.0) if seconds_done > 0 else 0.0
        power_consumed = self._read_kw(power_consumed_id) or 0.0
        month_max_src = self._read_kw(month_max_id) or 0.0

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
        self._high_on = r.high_on
        self._critical_on = r.critical_on

        # Retain the previous month-max whenever the value changes, so a new
        # peak can be reported against what it was before. Persisted on change.
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
            "high": high,
            "critical": critical,
            "previous_month_max": round(self._previous_month_max, 2) if self._previous_month_max is not None else None,
        }
