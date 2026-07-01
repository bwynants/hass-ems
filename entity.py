"""Shared entity base for the Energy Management System integration."""
from __future__ import annotations

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER


class EMSEntity(CoordinatorEntity):
    """Base entity binding all EMS entities to the single "EMS" device.

    Provides the shared ``device_info`` so every platform entity attaches to the
    one device keyed on the config entry. ``_attr_has_entity_name`` is enabled by
    default; entities that supply their own full name (e.g. the per-appliance
    mode select) set it back to ``False``.
    """

    _attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.entry_id)},
            name="EMS",
            manufacturer=MANUFACTURER,
        )
