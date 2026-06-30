import logging
from logging.handlers import RotatingFileHandler

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from .coordinator import SolarApplianceCoordinator
from .demand_coordinator import QuarterlyDemandCoordinator
from .const import DOMAIN, PLATFORMS

# Dedicated debug log file, written to the HA config dir so it can be read
# over the (SMB) config share. Self-contained; remove this block to disable.
_PKG_LOGGER = logging.getLogger("custom_components.energy_management_system")
_DEBUG_LOG_FILENAME = "ems_debug.log"
_DEBUG_HANDLER_FLAG = "_sam_debug_file_handler"


def _setup_debug_file_handler(path: str) -> None:
    """Attach a rotating file handler to the integration logger (blocking I/O)."""
    if getattr(_PKG_LOGGER, _DEBUG_HANDLER_FLAG, False):
        return
    handler = RotatingFileHandler(
        path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    _PKG_LOGGER.addHandler(handler)
    _PKG_LOGGER.setLevel(logging.DEBUG)
    setattr(_PKG_LOGGER, _DEBUG_HANDLER_FLAG, True)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when config or appliance subentries change.

    Per-appliance runtime settings (mode, enabled) and the global auto-switching
    toggle are applied live via the coordinator and don't need a full reload —
    reloading there would reset state and briefly disturb control. The coordinator
    sets ``suppress_reload`` for those updates so we skip the rebuild.
    """
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None and getattr(coordinator, "suppress_reload", False):
        coordinator.suppress_reload = False
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Set up the dedicated debug log file off the event loop (file open blocks).
    await hass.async_add_executor_job(
        _setup_debug_file_handler, hass.config.path(_DEBUG_LOG_FILENAME)
    )

    # The fast (~10s) quarter-hour demand coordinator is created first so the
    # main coordinator can read its values for peak-aware control.
    demand_coordinator = QuarterlyDemandCoordinator(hass, entry)
    await demand_coordinator.async_load()
    await demand_coordinator.async_config_entry_first_refresh()

    coordinator = SolarApplianceCoordinator(hass, entry)
    coordinator.demand_coordinator = demand_coordinator
    await coordinator.async_config_entry_first_refresh()

    # Re-run the optimizer immediately when a tariff/off-peak input changes,
    # instead of waiting for the next scheduled poll.
    coordinator.setup_trigger_listeners()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok