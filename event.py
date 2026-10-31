"""Event platform: once-per-quarter peak alerts from the quarterly-demand coordinator.

One event entity on the EMS device fires ``high`` / ``critical`` /
``cleared`` / ``new_month_peak`` as decided by :mod:`ems_core.demand_alerts`
— at most one High, one Critical and one closing message per quarter hour.
Automations consume it with an ``event.received`` trigger filtered on
``event_type`` and pick the numbers (``month_max``, ``estimate``,
``previous_peak``, ``new_peak``) off the entity attributes.
"""
from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import callback

from .const import DOMAIN
from .coordinator import SolarApplianceCoordinator
from .demand_coordinator import QuarterlyDemandCoordinator
from .ems_core.demand_alerts import ALERT_TYPES
from .entity import EMSEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator: SolarApplianceCoordinator = hass.data[DOMAIN][entry.entry_id]
    demand: QuarterlyDemandCoordinator = coordinator.demand_coordinator
    async_add_entities([QuarterlyDemandAlertEvent(demand, entry.entry_id)])


class QuarterlyDemandAlertEvent(EMSEntity, EventEntity):
    """Fires each alert the demand coordinator emits, exactly once."""

    _attr_translation_key = "quarterly_demand_alert"
    _attr_event_types = list(ALERT_TYPES)
    _attr_icon = "mdi:bell-alert-outline"

    def __init__(self, coordinator: QuarterlyDemandCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_demand_alert"
        # Highest alert ``seq`` already fired. Starts at 0 so anything the
        # coordinator published before this entity was added (first refresh
        # at startup) is still delivered on the first update.
        self._last_seq = 0

    @property
    def available(self) -> bool:
        return bool(
            super().available
            and self.coordinator.data
            and self.coordinator.data.get("configured")
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._fire_pending(write_state=False)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._fire_pending(write_state=True)

    def _fire_pending(self, *, write_state: bool) -> None:
        alerts = (self.coordinator.data or {}).get("alerts") or []
        for alert in alerts:
            seq = alert.get("seq", 0)
            if seq <= self._last_seq:
                continue
            self._last_seq = seq
            attrs = {k: v for k, v in alert.items() if k not in ("seq", "type")}
            self._trigger_event(alert["type"], attrs)
        if write_state:
            self.async_write_ha_state()
