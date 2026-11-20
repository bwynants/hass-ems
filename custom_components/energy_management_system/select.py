"""Select platform for runtime mode changes (persistent)."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from .const import DOMAIN, MODES, DEFAULT_MODE, CONF_RUNTIME_MODE, CONF_RUNTIME_MODES, CONF_NAME, get_appliances
from .entity import EMSApplianceEntity
from .coordinator import SolarApplianceCoordinator

async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities):
    coordinator: SolarApplianceCoordinator = hass.data[DOMAIN][entry.entry_id]
    # One call per appliance: config_subentry_id is per-call, and it is what
    # makes both the appliance device and this entity belong to the subentry.
    for app in get_appliances(entry):
        async_add_entities(
            [SolarApplianceModeSelect(coordinator, app)],
            config_subentry_id=app["subentry_id"],
        )


class SolarApplianceModeSelect(EMSApplianceEntity, SelectEntity):
    """Persistent mode selector for each appliance."""

    # The appliance device carries the user-provided name, so the entity name is
    # just the translated "Mode" suffix; the same key translates the options.
    _attr_translation_key = "appliance_mode"

    def __init__(self, coordinator: SolarApplianceCoordinator, appliance):
        super().__init__(coordinator)
        self.appliance = appliance
        # The config subentry id: the stable identity for uniqueness/persistence.
        self._appliance_id = appliance["subentry_id"]
        self._attr_unique_id = f"{DOMAIN}_{self._appliance_id}_mode"
        self._attr_options = MODES
        self._appliance_name = appliance[CONF_NAME]

    @property
    def current_option(self) -> str:
        """Return current mode (from persistent storage)."""
        return self.coordinator.get_appliance_persistent_setting(
            self._appliance_id,
            CONF_RUNTIME_MODE,
            CONF_RUNTIME_MODES,
            DEFAULT_MODE,
        )

    async def async_select_option(self, option: str) -> None:
        """Change mode and save persistently."""
        if option not in MODES:
            return

        # Persist to the subentry when available
        if await self.coordinator.async_set_appliance_persistent_setting(
            self._appliance_id,
            CONF_RUNTIME_MODE,
            option,
            title=self._appliance_name,
        ):
            return

        # If subentry update isn't available or failed, fall back to options to avoid losing user change
        entry = self.coordinator.config_entry
        options = dict(entry.options)
        runtime_modes = dict(options.get(CONF_RUNTIME_MODES, {}))
        runtime_modes[self._appliance_id] = option
        options[CONF_RUNTIME_MODES] = runtime_modes
        # Suppress the reload the update listener would otherwise do: a mode
        # change is applied live via async_refresh, so a full rebuild would only
        # reset coordinator state for no reason (mirrors the coordinator's own
        # options writes).
        if self.hass.config_entries.async_update_entry(entry, options=options):
            self.coordinator.suppress_reloads += 1
        await self.coordinator.async_refresh()
