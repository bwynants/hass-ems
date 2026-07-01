"""Peak-aware (capacity-tariff) control for the Energy Management System.

``DemandControlMixin`` reads the quarter-hour demand coordinator's snapshot and
derives the peak-headroom amp budget the optimizer throttles against, plus the
graceful critical-shed behaviour (throttle-to-min-then-off).

Mixed into ``SolarApplianceCoordinator``; relies on ``self.demand_coordinator``,
``self.hass``, ``self.appliance_states``, ``self._read_sensor``/
``self._read_sensor_optional``, ``self._is_unmanaged``/``self._is_entity_on``
and ``self._update_fixed_amps_entity`` (from the optimizer mixin).
"""
from __future__ import annotations

import logging

from homeassistant.util import dt as dt_util

from .const import (
    CONF_VOLTAGE, DEFAULT_VOLTAGE,
    CONF_NAME, CONF_MIN_AMPS, DEFAULT_MIN_AMPS, CONF_FIXED_AMPS,
)
from .helpers import default_appliance_state

_LOGGER = logging.getLogger(__name__)


class DemandControlMixin:
    """Quarter-hour demand (capacity-tariff) peak control for the coordinator."""

    def _demand_data(self):
        """Return the demand coordinator's data when the feature is active."""
        dc = self.demand_coordinator
        if dc is None or not getattr(dc, "feature_active", False):
            return None
        return dc.data

    def _is_demand_critical(self, config) -> bool:
        """True when the quarter-hour demand is critical.

        Peak-aware control is always on; it simply has no effect until the
        quarter-hour demand sensors are configured (``_demand_data`` is None).
        """
        demand = self._demand_data()
        return bool(demand and demand.get("critical"))

    def _get_peak_amps(self, config):
        """Peak headroom in amps (from Max Power To Peak Limit), or None.

        None disables the peak cap in the optimizer (demand feature not
        configured, or no value yet). Peak-aware control is always on.
        """
        demand = self._demand_data()
        if not demand:
            return None
        kw = demand.get("max_power_to_peak")
        if kw is None:
            return None
        voltage = self._read_sensor(config.get(CONF_VOLTAGE), default=DEFAULT_VOLTAGE)
        if voltage <= 0:
            return None
        return kw * 1000.0 / voltage

    def _critical_shed(self, appliances):
        """Shed load on critical demand, gracefully.

        Each running appliance that is above its minimum (and has an adjustable
        ``fixed_amps`` setpoint) is first throttled to ``min_amps`` and kept on;
        appliances already at the minimum, or with no adjustable setpoint, are
        switched off. So on the first critical cycle adjustable loads drop to min,
        and only if demand is still critical next cycle are they cut completely.

        Returns ``(kept_names, kept_amps)`` for the appliances kept on at min.
        """
        now = dt_util.utcnow()
        kept: list[str] = []
        kept_amps = 0.0
        for app in appliances:
            name = app.get(CONF_NAME)
            state = self.appliance_states.setdefault(name, default_appliance_state())
            if self._is_unmanaged(app) or not self._is_entity_on(app):
                continue
            min_a = app.get(CONF_MIN_AMPS, DEFAULT_MIN_AMPS)
            fixed_entity = app.get(CONF_FIXED_AMPS)
            current = self._read_sensor_optional(fixed_entity) if fixed_entity else None
            if current is not None and current > min_a:
                # Step down to min and keep it on this cycle.
                state["is_on"] = True
                state["last_change"] = now
                state["last_fixed_amps"] = float(min_a)
                state["last_fixed_amps_change"] = now
                self.hass.async_create_task(
                    self._update_fixed_amps_entity(app, float(min_a))
                )
                kept.append(name)
                kept_amps += min_a
                _LOGGER.warning(
                    "demand CRITICAL: %s throttled %.1f A -> min %d A (will shed if still critical)",
                    name, current, min_a,
                )
            else:
                # Already at min, or no adjustable setpoint: switch off.
                state["is_on"] = False
                state["last_change"] = now
                state["last_fixed_amps"] = None
                state["last_fixed_amps_change"] = None
                _LOGGER.warning(
                    "demand CRITICAL: %s shed (off)%s",
                    name, "" if fixed_entity else " (no adjustable setpoint)",
                )
        return kept, kept_amps
