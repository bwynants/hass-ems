from __future__ import annotations

from datetime import timedelta
import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.event import async_track_state_change_event
from .const import (
    DOMAIN, CONF_PEAK_TARIFF_SENSOR, CONF_NAME,
    CONF_AUTO_SWITCHING, DEFAULT_AUTO_SWITCHING,
    CONTROLLER_UPDATE_INTERVAL, get_appliances,
)
from . import battery
from .optimizer import ApplianceOptimizerMixin
from .demand_control import DemandControlMixin
from .helpers import read_sensor, read_sensor_optional, read_binary

_LOGGER = logging.getLogger(__name__)


class SolarApplianceCoordinator(ApplianceOptimizerMixin, DemandControlMixin, DataUpdateCoordinator):
    """Coordinator

    Periodically reads sensor data, runs the optimizer and planner,
    and applies control decisions. The optimizer / demand-control logic lives in
    the mixins (:mod:`optimizer`, :mod:`demand_control`); battery SoC reads in
    :mod:`battery`. This class owns the update cycle, config persistence and the
    thin sensor-read wrappers the mixins call.
    """
    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry, update_interval=timedelta(seconds=CONTROLLER_UPDATE_INTERVAL))
        self.entry = entry
        self.appliance_states = {}
        self._enabled = entry.options.get(CONF_AUTO_SWITCHING, DEFAULT_AUTO_SWITCHING)
        self._last_excess = 0.0
        self._last_soc = None
        self._battery_min_soc = None
        self._battery_protection_mode = None
        # Set by __init__.py after construction; the fast quarter-hour demand
        # coordinator, read here for peak-aware control.
        self.demand_coordinator = None
        # When True, the next config-entry update listener call skips the full
        # reload (used for live per-appliance / auto-switching setting changes).
        self.suppress_reload = False

    @property
    def enabled(self) -> bool:
        """Return whether the controller is enabled."""
        return self._enabled

    async def async_set_auto_switching(self, value: bool) -> None:
        """Set whether automatic switching is enabled and persist the option.

        Use this method for user-driven changes so the coordinator's enabled
        flag is only changed via an explicit API (prevents incidental toggles).
        """
        self._enabled = value
        options = dict(self.entry.options)
        options[CONF_AUTO_SWITCHING] = value
        # Applied live below; don't let the update listener reload the entry.
        self.suppress_reload = True
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        await self.async_refresh()
        _LOGGER.info("Auto switching set to %s", value)

    def setup_trigger_listeners(self) -> None:
        """Refresh immediately when an off-peak / tariff input changes state.

        Without this, a tariff transition would only take effect on the next
        scheduled poll. The listener unsubscribe is tied to the config entry
        so it is cleaned up on unload/reload.
        """
        config = {**self.entry.data, **self.entry.options}
        entities = [
            e
            for e in (
                config.get(CONF_PEAK_TARIFF_SENSOR),
            )
            if e
        ]
        if not entities:
            return

        @callback
        def _on_trigger_change(event) -> None:
            _LOGGER.debug(
                "Trigger entity %s changed (%s -> %s); requesting refresh",
                event.data.get("entity_id"),
                getattr(event.data.get("old_state"), "state", None),
                getattr(event.data.get("new_state"), "state", None),
            )
            self.hass.async_create_task(self.async_request_refresh())

        self.entry.async_on_unload(
            async_track_state_change_event(self.hass, entities, _on_trigger_change)
        )
        _LOGGER.debug("Watching tariff trigger entities for changes: %s", entities)

    def _get_subentry(self, appliance_id: str | None):
        if not appliance_id:
            return None
        return getattr(self.entry, "subentries", {}).get(appliance_id)

    def get_appliance_persistent_setting(
        self,
        appliance_id: str | None,
        key: str,
        fallback_key: str | None = None,
        default=None,
    ):
        """Return a persistent appliance setting from subentry or fallback options."""
        if not appliance_id:
            return default
        subentry = self._get_subentry(appliance_id)
        if subentry and key in subentry.data:
            return subentry.data.get(key, default)
        if fallback_key:
            return self.entry.options.get(fallback_key, {}).get(appliance_id, default)
        return default

    async def async_set_appliance_persistent_setting(
        self,
        appliance_id: str | None,
        key: str,
        value,
        title: str | None = None,
    ) -> bool:
        """Persist a per-appliance setting to the config subentry."""
        if not appliance_id:
            return False
        subentry = self._get_subentry(appliance_id)
        if not subentry or not hasattr(self.hass.config_entries, "async_update_subentry"):
            return False
        try:
            new_data = dict(subentry.data)
            new_data[key] = value
            title = title or getattr(subentry, "title", None) or self.entry.data.get(CONF_NAME)
            # Applied live via async_refresh below; a per-appliance setting change
            # (mode/enabled) doesn't need a full reload, which would reset state
            # and briefly disturb control. Tell the update listener to skip it.
            self.suppress_reload = True
            self.hass.config_entries.async_update_subentry(
                self.entry,
                subentry,
                data=new_data,
                title=title,
            )
            await self.async_refresh()
            return True
        except Exception as err:
            self.suppress_reload = False
            _LOGGER.exception("Failed to persist %s for appliance %s: %s", key, appliance_id, err)
            return False

    def _read_sensor(self, entity_id: str | None, default: float = 0.0) -> float:
        """Read a numeric sensor (watts-normalised for power) or return default."""
        return read_sensor(self.hass, entity_id, default)

    def _read_sensor_optional(self, entity_id: str | None) -> float | None:
        """Read a numeric sensor (watts-normalised for power) or return None."""
        return read_sensor_optional(self.hass, entity_id)

    def _read_binary(self, entity_id: str | None) -> bool | None:
        """Read a binary sensor, returns True/False/None."""
        return read_binary(self.hass, entity_id)

    async def _async_update_data(self):
        data = {**self.entry.data, **self.entry.options}
        appliances = get_appliances(self.entry)
        _LOGGER.debug(
            "==== EMS cycle start: %d appliance(s), auto_switching=%s ====",
            len(appliances), self.enabled,
        )

        # SoC and the protection threshold are derived from the appliances that
        # are flagged as batteries (each carries its own SoC sensor and min_soc).
        self._last_soc = battery.current_soc(self.hass, appliances)
        self._battery_min_soc = battery.battery_min_soc(appliances)
        self._battery_protection_mode = battery.battery_protection_mode(appliances)
        if self._last_soc is not None or self._battery_min_soc is not None:
            _LOGGER.debug(
                "battery: soc=%s, min_soc=%s, protection=%s",
                self._last_soc, self._battery_min_soc, self._battery_protection_mode,
            )

        # Amps the managed appliances are currently drawing (exposed as a sensor
        # and added back to the grid-export budget further down).
        managed_amps = self._get_managed_consumption_amps(appliances, data)
        _LOGGER.debug("managed appliances currently drawing %.2f A", managed_amps)

        # Quarter-hour demand snapshot (drives peak-aware control), if configured.
        demand = self._demand_data()
        if demand:
            _LOGGER.debug(
                "demand: estimate=%s kW high_limit=%s crit_limit=%s high=%s critical=%s max_to_peak=%s kW",
                demand.get("estimate"), demand.get("high_limit"), demand.get("critical_limit"),
                demand.get("high"), demand.get("critical"), demand.get("max_power_to_peak"),
            )

        # Quarter-hour demand is critical: force everything off to protect the
        # monthly peak (emergency backstop if graceful throttling can't keep up).
        if self._is_demand_critical(data):
            if self.enabled:
                _LOGGER.warning(
                    "demand CRITICAL -> throttling managed appliances to min, then shedding"
                )
                kept, kept_amps = self._critical_shed(appliances)
                await self._apply_appliance_states(appliances, kept)
            else:
                kept, kept_amps = [], 0.0
                _LOGGER.warning(
                    "demand CRITICAL (auto-switching off; not intervening)"
                )
            _LOGGER.debug(
                "==== EMS cycle end: critical (kept at min: %s) ====", kept or "none"
            )
            return {
                "excess_amps": 0,
                "soc": self._last_soc,
                "active_appliances": kept,
                "appliances": appliances,
                "auto_switching": self.enabled,
                "demand_critical": True,
                "managed_amps": round(kept_amps, 2),
            }

        # Total surplus available for managed loads = grid surplus + the draw
        # already taken by managed appliances (added back to work from gross).
        grid_surplus = self._calculate_excess_amps(data)
        excess_amps = grid_surplus + managed_amps
        self._last_excess = excess_amps
        _LOGGER.debug(
            "budget: excess=%.2f A (grid surplus %.2f A + managed add-back %.2f A)",
            excess_amps, grid_surplus, managed_amps,
        )

        self._update_histories(appliances, excess_amps)

        if not self.enabled:
            _LOGGER.debug(
                "auto-switching OFF -> optimizer skipped; spare=%.2f A", excess_amps
            )
            _LOGGER.debug("==== EMS cycle end: auto-switching off ====")
            return {
                "excess_amps": round(excess_amps, 2),
                "soc": self._last_soc,
                "active_appliances": [],
                "appliances": appliances,
                "auto_switching": False,
                "managed_amps": round(managed_amps, 2),
            }

        peak_amps = self._get_peak_amps(data)
        _LOGGER.debug(
            "peak headroom: %s",
            "off (no demand feature)" if peak_amps is None else f"{peak_amps:.2f} A",
        )
        active, decided_amps = self._optimize_appliances(appliances, excess_amps, data, peak_amps)
        await self._apply_appliance_states(appliances, active)

        _LOGGER.debug(
            "result: active=%s | decided draw=%.2f A | spare(excess)=%.2f A",
            active, decided_amps, excess_amps,
        )
        _LOGGER.debug("==== EMS cycle end ====")
        return {
            "excess_amps": round(excess_amps, 2),
            "soc": self._last_soc,
            "active_appliances": active,
            "appliances": appliances,
            "auto_switching": True,
            # Report the draw the optimizer just decided (fresh setpoints), so
            # Managed Consumption updates this cycle instead of lagging one.
            "managed_amps": round(decided_amps, 2),
        }
