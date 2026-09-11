import logging
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from queue import SimpleQueue

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from .coordinator import SolarApplianceCoordinator
from .demand_coordinator import QuarterlyDemandCoordinator
from .const import (
    DOMAIN, MANUFACTURER, PLATFORMS, SUBENTRY_TYPE_APPLIANCE,
    CONF_DEBUG_LOG, DEFAULT_DEBUG_LOG, CONF_EV_SOC_SENSOR, get_appliances,
)

# unique_id suffixes of sensors orphaned by past renames:
# managed_consumption -> managed_consumption_amps,
# managed_consumption_kw -> managed_consumption_power.
_STALE_UNIQUE_ID_SUFFIXES = ("managed_consumption", "managed_consumption_kw")

# Entry ids whose one-time registry migrations have already run this process, so
# the idempotent cleanups don't re-scan the registry on every reload. Reset on
# HA restart (fresh module), which is when a fresh migration pass is wanted.
_MIGRATED_ENTRIES: set[str] = set()

# (domain, unique_id suffix) of the per-appliance entities that only exist for
# EV chargers (appliances with a car SoC sensor).
_EV_ENTITY_SUFFIXES = (
    ("number", "force_charge_below_soc"),
    ("binary_sensor", "force_charging"),
)

# Dedicated debug log file, written to the HA config dir so it can be read
# over the (SMB) config share. Self-contained; remove this block to disable.
_PKG_LOGGER = logging.getLogger("custom_components.energy_management_system")
_DEBUG_LOG_FILENAME = "ems_debug.log"
_DEBUG_HANDLER_FLAG = "_sam_debug_file_handler"


# Handler/listener refs so the debug log can be torn down cleanly on unload.
_DEBUG_QUEUE_HANDLER: QueueHandler | None = None
_DEBUG_LISTENER: QueueListener | None = None


def _setup_debug_file_handler(path: str) -> None:
    """Attach a queue-backed rotating file handler to the integration logger.

    The logger gets a QueueHandler (a cheap in-memory put per record); the
    actual file writes and rollovers happen on the QueueListener's own thread,
    so the event loop never blocks on disk I/O for the debug log. Opt-in — only
    called when the debug-log config option is enabled.
    """
    global _DEBUG_QUEUE_HANDLER, _DEBUG_LISTENER
    if getattr(_PKG_LOGGER, _DEBUG_HANDLER_FLAG, False):
        return
    file_handler = RotatingFileHandler(
        path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    queue: SimpleQueue = SimpleQueue()
    listener = QueueListener(queue, file_handler)
    listener.start()
    queue_handler = QueueHandler(queue)
    queue_handler.setLevel(logging.DEBUG)
    _PKG_LOGGER.addHandler(queue_handler)
    _PKG_LOGGER.setLevel(logging.DEBUG)
    setattr(_PKG_LOGGER, _DEBUG_HANDLER_FLAG, True)
    _DEBUG_QUEUE_HANDLER, _DEBUG_LISTENER = queue_handler, listener


def _teardown_debug_file_handler() -> None:
    """Detach the debug handler and stop its listener thread (on unload)."""
    global _DEBUG_QUEUE_HANDLER, _DEBUG_LISTENER
    if _DEBUG_QUEUE_HANDLER is not None:
        _PKG_LOGGER.removeHandler(_DEBUG_QUEUE_HANDLER)
        _DEBUG_QUEUE_HANDLER = None
    if _DEBUG_LISTENER is not None:
        _DEBUG_LISTENER.stop()  # flushes the queue and joins the writer thread
        _DEBUG_LISTENER = None
    if getattr(_PKG_LOGGER, _DEBUG_HANDLER_FLAG, False):
        setattr(_PKG_LOGGER, _DEBUG_HANDLER_FLAG, False)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when config or appliance subentries change.

    Per-appliance runtime settings (mode, enabled) and the global auto-switching
    toggle are applied live via the coordinator and don't need a full reload —
    reloading there would reset state and briefly disturb control. The coordinator
    sets ``suppress_reload`` for those updates so we skip the rebuild.
    """
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None and getattr(coordinator, "suppress_reloads", 0) > 0:
        coordinator.suppress_reloads -= 1
        return
    await hass.config_entries.async_reload(entry.entry_id)


def _cleanup_stale_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove registry entries orphaned by unique_id renames.

    Without this the old entities linger as unavailable duplicates, and the
    renamed power sensor keeps a collision-suffixed ``_2`` entity_id.
    """
    ent_reg = er.async_get(hass)
    for suffix in _STALE_UNIQUE_ID_SUFFIXES:
        unique_id = f"{DOMAIN}_{entry.entry_id}_{suffix}"
        entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id:
            _PKG_LOGGER.info("Removing stale entity %s (unique_id %s)", entity_id, unique_id)
            ent_reg.async_remove(entity_id)

    # The rename collision left the managed_consumption_power entity with a
    # `_2`-suffixed entity_id; give it the clean id once the stale one is gone.
    unique_id = f"{DOMAIN}_{entry.entry_id}_managed_consumption_power"
    entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, unique_id)
    if entity_id and entity_id.endswith("_2"):
        clean_id = entity_id[:-2]
        if not ent_reg.async_get(clean_id):
            _PKG_LOGGER.info("Renaming %s -> %s", entity_id, clean_id)
            ent_reg.async_update_entity(entity_id, new_entity_id=clean_id)

    _cleanup_orphaned_appliance_entities(entry, ent_reg)


def _cleanup_orphaned_appliance_entities(
    entry: ConfigEntry, ent_reg: er.EntityRegistry
) -> None:
    """Remove mode selects left behind by appliances deleted long ago.

    Before the selects were assigned to their appliance subentry they were not
    cleaned up when the subentry went away, so old installs can carry selects
    for appliances that no longer exist. Now that each select belongs to its
    subentry, Home Assistant removes them itself; this only clears the backlog.
    """
    live_ids = {
        subentry.subentry_id
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_TYPE_APPLIANCE
    }
    prefix, suffix = f"{DOMAIN}_", "_mode"
    for registry_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        unique_id = registry_entry.unique_id
        if registry_entry.domain != "select":
            continue
        if not (unique_id.startswith(prefix) and unique_id.endswith(suffix)):
            continue
        appliance_id = unique_id[len(prefix) : -len(suffix)]
        if appliance_id in live_ids:
            continue
        _PKG_LOGGER.info(
            "Removing orphaned appliance entity %s (unique_id %s)",
            registry_entry.entity_id,
            unique_id,
        )
        ent_reg.async_remove(registry_entry.entity_id)


def _cleanup_ev_entities_without_soc_sensor(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the EV entities of appliances whose car SoC sensor was cleared.

    The platforms only create them while the sensor is configured; clearing it
    in a reconfigure reloads the entry, but the registry keeps the rows (HA only
    removes entities with their subentry), so they would linger as unavailable.
    Cheap enough to run on every setup: two lookups per appliance.
    """
    ent_reg = er.async_get(hass)
    for app in get_appliances(entry):
        if app.get(CONF_EV_SOC_SENSOR):
            continue
        for domain, suffix in _EV_ENTITY_SUFFIXES:
            unique_id = f"{DOMAIN}_{app['subentry_id']}_{suffix}"
            entity_id = ent_reg.async_get_entity_id(domain, DOMAIN, unique_id)
            if entity_id:
                _PKG_LOGGER.info(
                    "Removing %s (appliance has no car SoC sensor)", entity_id
                )
                ent_reg.async_remove(entity_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Optional per-cycle debug log to ems_debug.log — opt-in (off by default),
    # set up off the event loop (file open blocks).
    config = {**entry.data, **entry.options}
    if config.get(CONF_DEBUG_LOG, DEFAULT_DEBUG_LOG):
        await hass.async_add_executor_job(
            _setup_debug_file_handler, hass.config.path(_DEBUG_LOG_FILENAME)
        )

    # Clean up entities orphaned by past unique_id renames before the platforms
    # register the current set. Idempotent one-time migrations — run once per
    # process (ongoing appliance deletions are handled by HA subentry ownership),
    # so a reload doesn't re-scan the registry.
    if entry.entry_id not in _MIGRATED_ENTRIES:
        _cleanup_stale_entities(hass, entry)
        _MIGRATED_ENTRIES.add(entry.entry_id)
    # Not a one-time migration: a reconfigure that clears the SoC sensor reloads
    # the entry, and that reload must drop the now-orphaned EV entities.
    _cleanup_ev_entities_without_soc_sensor(hass, entry)

    # The fast (~10s) quarter-hour demand coordinator is created first so the
    # main coordinator can read its values for peak-aware control.
    demand_coordinator = QuarterlyDemandCoordinator(hass, entry)
    await demand_coordinator.async_load()
    await demand_coordinator.async_config_entry_first_refresh()

    coordinator = SolarApplianceCoordinator(hass, entry)
    coordinator.demand_coordinator = demand_coordinator
    # Restore the appliances' runtime state (on/off, hysteresis timers, current
    # setpoints) BEFORE the first refresh, so that cycle starts from what the
    # appliances were actually doing instead of from a blank slate.
    await coordinator.async_load_state()
    await coordinator.async_config_entry_first_refresh()

    # Re-run the optimizer immediately when a tariff/off-peak input changes,
    # instead of waiting for the next scheduled poll.
    coordinator.setup_trigger_listeners()

    # Register the parent "EMS" device up front. The entities would create it
    # anyway via their device_info, but the per-appliance devices need its
    # registry id for via_device_id before the platforms are forwarded.
    main_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="EMS",
        manufacturer=MANUFACTURER,
    )
    coordinator.main_device_id = main_device.id

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        # Flush the runtime state now: a reload builds a new coordinator that
        # loads the store immediately, and a pending delayed save would lose
        # the last few minutes of hysteresis/setpoint state.
        if coordinator is not None:
            await coordinator.async_save_state_now()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        # Stop the debug log's listener thread and detach its handler.
        await hass.async_add_executor_job(_teardown_debug_file_handler)
    return unload_ok