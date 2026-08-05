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


class EMSApplianceEntity(EMSEntity):
    """Base entity for the per-appliance devices.

    Each appliance is a config subentry, and since HA 2026.8 a device belongs to
    exactly one config entry and at most one subentry — so every appliance gets
    its own device, hung under the main "EMS" device via ``via_device_id``. The
    platform must add these entities with ``config_subentry_id`` so the device
    ends up owned by the subentry and is cleaned up when the appliance is
    deleted.
    """

    _appliance_id: str
    _appliance_name: str

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._appliance_id)},
            name=self._appliance_name,
            manufacturer=MANUFACTURER,
            via_device_id=self.coordinator.main_device_id,
        )
