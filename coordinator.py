from __future__ import annotations

from datetime import timedelta
import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util
from .const import (
    DOMAIN, CONF_PEAK_TARIFF_SENSOR, CONF_NAME, CONF_VOLTAGE,
    CONF_AUTO_SWITCHING, DEFAULT_AUTO_SWITCHING, DEFAULT_VOLTAGE,
    CONTROLLER_UPDATE_INTERVAL, get_appliances,
)
from .helpers import read_sensor
from . import control
from . import ems_core

_LOGGER = logging.getLogger(__name__)


class SolarApplianceCoordinator(DataUpdateCoordinator):
    """Coordinator — the imperative shell around the pure ``ems_core`` engine.

    Each cycle it reads a snapshot of the sensors/config into ``ems_core``
    dataclasses (via :mod:`control`), calls the pure decision functions, and
    applies the returned commands via HA services. It owns the runtime state
    (``appliance_states``), config persistence, and the demand-coordinator link.
    """
    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry, update_interval=timedelta(seconds=CONTROLLER_UPDATE_INTERVAL))
        self.entry = entry
        # appliance uid (config subentry id) -> ems_core.ApplianceState
        self.appliance_states: dict[str, ems_core.ApplianceState] = {}
        self._enabled = entry.options.get(CONF_AUTO_SWITCHING, DEFAULT_AUTO_SWITCHING)
        self._last_soc = None
        # Set by __init__.py after construction; the fast quarter-hour demand
        # coordinator, read here for peak-aware control.
        self.demand_coordinator = None
        # Number of pending update-listener calls to skip (live per-appliance /
        # auto-switching setting changes applied without a full reload). A
        # counter, not a flag: it is only incremented when the update actually
        # changed something (a no-op update fires no listener and would leave a
        # flag latched), and rapid consecutive changes each consume one.
        self.suppress_reloads = 0

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
        # Applied live below; skip the reload the update listener would do.
        # Only count it when the update actually changed something — a no-op
        # update fires no listener.
        if self.hass.config_entries.async_update_entry(self.entry, options=options):
            self.suppress_reloads += 1
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
            # and briefly disturb control. Only count the skip when the update
            # actually changed something (a no-op fires no listener).
            if self.hass.config_entries.async_update_subentry(
                self.entry,
                subentry,
                data=new_data,
                title=title,
            ):
                self.suppress_reloads += 1
            await self.async_refresh()
            return True
        except Exception as err:
            _LOGGER.exception("Failed to persist %s for appliance %s: %s", key, appliance_id, err)
            return False

    def _read_sensor(self, entity_id: str | None, default: float = 0.0) -> float:
        """Read a numeric sensor (watts-normalised for power) or return default."""
        return read_sensor(self.hass, entity_id, default)

    # ---- Quarter-hour demand (peak control) reads from the demand coordinator ----
    def _demand_data(self):
        """Return the demand coordinator's data when the feature is active."""
        dc = self.demand_coordinator
        if dc is None or not getattr(dc, "feature_active", False):
            return None
        return dc.data

    def _is_demand_critical(self) -> bool:
        """True when the quarter-hour demand is critical (peak control always on)."""
        demand = self._demand_data()
        return bool(demand and demand.get("critical"))

    def _get_peak_amps(self, voltage: float):
        """Grid-import ceiling in amps the optimizer scales into, or None.

        This is the **High limit** itself, NOT Max Power To Peak. The High limit
        is a fixed ceiling, so it can't balloon or collapse at the quarter
        boundary the way Max Power To Peak does (its remaining-time term makes it
        spike for one cycle when the current-average source lags the reset, which
        would shed a running appliance). Keeping total grid draw under the High
        limit at every instant keeps every quarter's average under it too.

        Base load is not subtracted here: ``excess_amps`` (the optimizer's
        ``remaining``) already nets it out, so ``forced_budget = remaining +
        ceiling`` is the true max draw. Max Power To Peak stays a monitoring
        sensor only.
        """
        demand = self._demand_data()
        if not demand or voltage <= 0:
            return None
        kw = demand.get("high_limit")
        if kw is None:
            return None
        return kw * 1000.0 / voltage

    async def _async_update_data(self):
        config = {**self.entry.data, **self.entry.options}
        appliances = get_appliances(self.entry)
        _LOGGER.debug(
            "==== EMS cycle start: %d appliance(s), auto_switching=%s ====",
            len(appliances), self.enabled,
        )

        voltage = self._read_sensor(config.get(CONF_VOLTAGE), default=DEFAULT_VOLTAGE)
        inputs, raw_by_uid = control.build_appliance_inputs(
            self.hass, appliances, self, voltage
        )
        energy = control.build_energy_input(self.hass, config, voltage)
        now = dt_util.utcnow()
        # The core keys everything by appliance uid; map back to display names
        # for the user-facing data and logs.
        names_by_uid = {i.uid: i.name for i in inputs}

        def _kw(amps: float) -> float:
            """Amps → kW at this cycle's voltage (for the *_kw sensor values)."""
            return round(amps * voltage / 1000.0, 2)

        # SoC / protection threshold, derived from the battery appliances.
        battery = ems_core.battery_status(inputs)
        self._last_soc = battery.soc
        if battery.soc is not None or battery.min_soc is not None:
            _LOGGER.debug(
                "battery: soc=%s, min_soc=%s, protection=%s",
                battery.soc, battery.min_soc, battery.protection_mode,
            )

        # Amps the managed appliances are currently drawing (added back below).
        managed_amps = ems_core.managed_consumption_amps(inputs)
        _LOGGER.debug("managed appliances currently drawing %.2f A", managed_amps)

        demand = self._demand_data()
        if demand:
            _LOGGER.debug(
                "demand: estimate=%s kW high_limit=%s crit_limit=%s high=%s critical=%s max_to_peak=%s kW",
                demand.get("estimate"), demand.get("high_limit"), demand.get("critical_limit"),
                demand.get("high"), demand.get("critical"), demand.get("max_power_to_peak"),
            )

        # Total surplus = grid surplus + managed draw added back (gross surplus).
        grid_surplus = ems_core.calculate_excess_amps(
            energy.grid_w, energy.solar_w, energy.house_load_w, voltage
        )
        excess_amps = grid_surplus + managed_amps
        _LOGGER.debug(
            "budget: excess=%.2f A (grid surplus %.2f A + managed add-back %.2f A)",
            excess_amps, grid_surplus, managed_amps,
        )

        # Roll the excess into the averaging history on EVERY cycle — critical
        # and disabled ones included — so the rolling average stays continuous
        # and the first post-critical cycle doesn't act on stale samples.
        ems_core.record_excess(inputs, self.appliance_states, excess_amps)

        # Critical demand: force everything off to protect the monthly peak.
        if self._is_demand_critical():
            if self.enabled:
                _LOGGER.warning(
                    "demand CRITICAL -> throttling managed appliances to min, then shedding"
                )
                shed = ems_core.critical_shed(inputs, self.appliance_states, now)
                await control.apply_commands(
                    self.hass, shed.kept, shed.fixed_amps_commands, inputs, raw_by_uid
                )
                kept, kept_amps = shed.kept, shed.kept_amps
            else:
                # Not intervening: nothing is "kept at min", but the appliances
                # are still drawing — report the real managed draw, not 0.
                kept, kept_amps = [], managed_amps
                _LOGGER.warning("demand CRITICAL (auto-switching off; not intervening)")
            kept_names = [names_by_uid.get(u, u) for u in kept]
            _LOGGER.debug(
                "==== EMS cycle end: critical (kept at min: %s) ====", kept_names or "none"
            )
            return {
                "excess_amps": 0,
                "excess_kw": 0,
                "soc": self._last_soc,
                "active_appliances": kept_names,
                "appliances": appliances,
                "auto_switching": self.enabled,
                "demand_critical": True,
                "managed_amps": round(kept_amps, 2),
                "managed_power": _kw(kept_amps),
            }

        if not self.enabled:
            _LOGGER.debug(
                "auto-switching OFF -> optimizer skipped; spare=%.2f A", excess_amps
            )
            _LOGGER.debug("==== EMS cycle end: auto-switching off ====")
            return {
                "excess_amps": round(excess_amps, 2),
                "excess_kw": _kw(excess_amps),
                "soc": self._last_soc,
                "active_appliances": [],
                "appliances": appliances,
                "auto_switching": False,
                "managed_amps": round(managed_amps, 2),
                "managed_power": _kw(managed_amps),
            }

        peak_amps = self._get_peak_amps(voltage)
        _LOGGER.debug(
            "peak headroom: %s",
            "off (no demand feature)" if peak_amps is None else f"{peak_amps:.2f} A",
        )
        result = ems_core.optimize(
            inputs, self.appliance_states, excess_amps, peak_amps, energy, battery, now
        )
        await control.apply_commands(
            self.hass, result.active, result.fixed_amps_commands, inputs, raw_by_uid
        )

        active_names = [names_by_uid.get(u, u) for u in result.active]
        _LOGGER.debug(
            "result: active=%s | decided draw=%.2f A | spare(excess)=%.2f A",
            active_names, result.decided_amps, excess_amps,
        )
        _LOGGER.debug("==== EMS cycle end ====")
        return {
            "excess_amps": round(excess_amps, 2),
            "excess_kw": _kw(excess_amps),
            "soc": self._last_soc,
            "active_appliances": active_names,
            "appliances": appliances,
            "auto_switching": True,
            # Report the draw the optimizer just decided (fresh setpoints), so
            # Managed Consumption updates this cycle instead of lagging one.
            "managed_amps": round(result.decided_amps, 2),
            "managed_power": _kw(result.decided_amps),
        }
