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
    DEMAND_QUARTER_SECONDS,
    DEMAND_MAX_POWER_CAP_KW,
    DEMAND_HYSTERESIS_KW,
    DEMAND_HIGH_GATE_MINUTES,
    DEMAND_CRITICAL_GATE_MINUTES,
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
        seconds_remaining = max(DEMAND_QUARTER_SECONDS - seconds_done, 1)

        current_so_far = (self._read_kw(current_avg_id) or 0.0) if seconds_done > 0 else 0.0
        power_consumed = self._read_kw(power_consumed_id) or 0.0
        month_max_src = self._read_kw(month_max_id) or 0.0

        minimal = float(config.get(CONF_MINIMAL_PEAK_WARNING, DEFAULT_MINIMAL_PEAK_WARNING))
        high_safety = float(config.get(CONF_PEAK_HIGH_SAFETY, DEFAULT_PEAK_HIGH_SAFETY))
        crit_safety = float(config.get(CONF_PEAK_CRITICAL_SAFETY, DEFAULT_PEAK_CRITICAL_SAFETY))

        month_max = max(month_max_src, minimal)
        high_limit = max(month_max - high_safety, 0.0)
        critical_limit = max(month_max - crit_safety, 0.0)

        estimate = current_so_far + power_consumed * seconds_remaining / DEMAND_QUARTER_SECONDS

        power_to_consume = max(
            (high_limit - current_so_far) * DEMAND_QUARTER_SECONDS / seconds_remaining, 0.0
        )
        max_power_to_peak = min(power_to_consume, DEMAND_MAX_POWER_CAP_KW)

        # High / Critical binary states with a measurement-window gate and a
        # kW deadband to avoid flapping.
        if minute_in_quarter < DEMAND_HIGH_GATE_MINUTES:
            high = False
        elif self._high_on:
            high = estimate >= (high_limit - DEMAND_HYSTERESIS_KW)
        else:
            high = estimate >= high_limit
        self._high_on = high

        if minute_in_quarter < DEMAND_CRITICAL_GATE_MINUTES:
            critical = False
        elif self._critical_on:
            critical = estimate >= (critical_limit - DEMAND_HYSTERESIS_KW)
        else:
            critical = estimate >= critical_limit
        self._critical_on = critical

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
