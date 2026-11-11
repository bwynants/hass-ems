from __future__ import annotations

from datetime import timedelta
import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util
from .const import (
    DOMAIN, CONF_PEAK_TARIFF_SENSOR, CONF_NAME, CONF_VOLTAGE,
    CONF_AUTO_SWITCHING, DEFAULT_AUTO_SWITCHING, DEFAULT_VOLTAGE,
    CONTROLLER_UPDATE_INTERVAL, CONF_GRID_EXPORT, GRID_STALE_SECONDS, get_appliances,
    CONF_SOLAR_PRODUCTION, CONF_HOUSE_LOAD,
    CONF_ENTITY_ID, CONF_POWER_SENSOR, CONF_FIXED_AMPS, CONF_CONNECTED_SENSOR,
    CONF_DEMAND_POWER_CONSUMED_SENSOR, MODE_UNMANAGED, STARTUP_GRACE_SECONDS,
    PEAK_ACTION_HOLD_SECONDS,
)
from .helpers import read_sensor, sensors_stale, not_ready_entities
from . import control
from . import ems_core

_LOGGER = logging.getLogger(__name__)

# Runtime-state persistence. Storing the hysteresis timestamps (not just the
# on/off flags) is what makes a restart transparent: a short outage leaves the
# window still running, a long one lets it expire on its own.
STATE_STORAGE_VERSION = 1
STATE_SAVE_DELAY_SECONDS = 30
# How long after a peak action the demand side holds its announcement.
_PEAK_ACTION_HOLD = timedelta(seconds=PEAK_ACTION_HOLD_SECONDS)

# Every configured entity a control decision reads. The price fields are
# deliberately absent: they have safe defaults (and may hold a plain number), so
# they must never hold up the start.
_GLOBAL_SOURCE_KEYS = (
    CONF_GRID_EXPORT, CONF_SOLAR_PRODUCTION, CONF_HOUSE_LOAD,
    CONF_VOLTAGE, CONF_PEAK_TARIFF_SENSOR,
)
_APPLIANCE_SOURCE_KEYS = (
    CONF_ENTITY_ID, CONF_POWER_SENSOR, CONF_FIXED_AMPS,
    CONF_CONNECTED_SENSOR,
)


def _parse_dt(value):
    """Parse a persisted ISO timestamp, tolerating anything unexpected."""
    if not value:
        return None
    return dt_util.parse_datetime(value)


def _parse_number(value, cast=float):
    try:
        return cast(value) if value is not None else None
    except (ValueError, TypeError):
        return None


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
        # Set by __init__.py after construction; the fast quarter-hour demand
        # coordinator, read here for peak-aware control.
        self.demand_coordinator = None
        # Set by __init__.py after construction; registry id of the parent
        # "EMS" device, used as via_device_id by the per-appliance devices.
        self.main_device_id: str | None = None
        # Number of pending update-listener calls to skip (live per-appliance /
        # auto-switching setting changes applied without a full reload). A
        # counter, not a flag: it is only incremented when the update actually
        # changed something (a no-op update fires no listener and would leave a
        # flag latched), and rapid consecutive changes each consume one.
        self.suppress_reloads = 0
        # Startup warm-up. Control is held until the source sensors report (or
        # the grace expires); ``_seed_states_from_entities`` then adopts the
        # appliances' real state as the controller's starting point.
        self._control_started = False
        self._warmup_deadline = None
        self._warmup_logged = False
        # Runtime-state persistence (survives both a reload and a restart).
        self._store = Store(hass, STATE_STORAGE_VERSION, f"{DOMAIN}_state_{entry.entry_id}")
        self._persisted_snapshot: dict | None = None
        # Until when the controller counts as "acting for peak" (throttling,
        # cutting or shedding). Published in the data; the demand coordinator
        # holds its High/Critical announcement while it is in the future.
        self._peak_action_until = None

    # ---- Runtime-state persistence -------------------------------------------

    async def async_load_state(self) -> None:
        """Restore the persisted per-appliance runtime state.

        Call once before the first refresh. Restoring the hysteresis timestamps
        means a quick restart resumes mid-window instead of re-arming it, while
        a long outage lets the window expire on its own.
        """
        data = await self._store.async_load()
        if not data:
            return
        for uid, raw in (data.get("appliances") or {}).items():
            if not isinstance(raw, dict):
                continue
            self.appliance_states[uid] = ems_core.ApplianceState(
                is_on=bool(raw.get("is_on")),
                last_change=_parse_dt(raw.get("last_change")),
                last_fixed_amps=_parse_number(raw.get("last_fixed_amps")),
                last_fixed_amps_change=_parse_dt(raw.get("last_fixed_amps_change")),
                peak_shed_quarter=_parse_number(raw.get("peak_shed_quarter"), int),
            )
        self._persisted_snapshot = self._state_snapshot()
        _LOGGER.debug(
            "restored runtime state for %d appliance(s)", len(self.appliance_states)
        )

    def _state_snapshot(self) -> dict:
        """The persistable slice of the runtime state.

        ``excess_history`` is deliberately excluded: after any real outage those
        samples describe a house that no longer exists, and the averaging code
        already falls back to the instantaneous excess while it refills.
        """
        return {
            "appliances": {
                uid: {
                    "is_on": st.is_on,
                    "last_change": st.last_change.isoformat() if st.last_change else None,
                    "last_fixed_amps": st.last_fixed_amps,
                    "last_fixed_amps_change": (
                        st.last_fixed_amps_change.isoformat()
                        if st.last_fixed_amps_change else None
                    ),
                    # The per-quarter shed lockout must survive a reload
                    # mid-quarter, or the reload restarts the appliance into
                    # the same tight projection.
                    "peak_shed_quarter": st.peak_shed_quarter,
                }
                for uid, st in self.appliance_states.items()
            }
        }

    def _persist_state(self) -> None:
        """Queue a debounced save, but only when something actually changed.

        ``async_delay_save`` restarts its timer on every call, so saving
        unconditionally on each 10 s cycle would postpone the write forever.
        """
        snapshot = self._state_snapshot()
        if snapshot == self._persisted_snapshot:
            return
        self._persisted_snapshot = snapshot
        self._store.async_delay_save(lambda: snapshot, STATE_SAVE_DELAY_SECONDS)

    async def async_save_state_now(self) -> None:
        """Flush the runtime state immediately (on unload, ahead of a reload).

        A reload constructs a new coordinator that loads the store right away —
        without this flush it would read back the state from before the change.
        """
        snapshot = self._state_snapshot()
        self._persisted_snapshot = snapshot
        await self._store.async_save(snapshot)

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
            # Shutdown tears the source integrations down one by one, and
            # each teardown flips its entities to unavailable — which a
            # template built on them may turn into a *valid-looking* value.
            # That is a tariff "change" we must not act on: it is the house
            # disappearing, not the tariff switching. Rendering it here would
            # command an appliance a few ms before HA dies and leave it in
            # that state for the whole restart (2026-10-27 19:42:44: Slimmelezer
            # disconnect -> tariff read off-peak -> charger ON into the outage).
            if self.hass.is_stopping:
                _LOGGER.debug("Trigger entity changed during shutdown; ignored")
                return
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

    def _demand_amps(self, key: str, voltage: float):
        """A demand-coordinator kW value as amps, or None when unavailable.

        ``high_limit`` is the instantaneous grid ceiling the optimizer scales
        into — the High limit itself, NOT Max Power To Peak: it is constant, so
        it can't balloon or collapse at the quarter boundary (when the
        current-average source lags the reset). ``projected_ceiling`` is the
        second ceiling for the rest of the quarter, None at that boundary.

        Base load is not subtracted here: ``excess_amps`` (the optimizer's
        ``remaining``) already nets it out — it is SIGNED, negative while the
        house imports — so ``remaining + ceiling`` is the true max draw.
        """
        demand = self._demand_data()
        if not demand:
            return None
        return ems_core.kw_to_amps(demand.get(key), voltage)

    def _note_peak_action(self, now) -> None:
        """Record that the controller is acting for peak this cycle.

        Published as ``peak_action_until``; the demand coordinator holds the
        High/Critical announcement until then and warns only if the risk
        outlives the shed.
        """
        self._peak_action_until = now + _PEAK_ACTION_HOLD

    # ---- Startup warm-up ------------------------------------------------------

    def _source_entities(self, config: dict, inputs, raw_by_uid) -> list:
        """Every configured entity the control decision reads this cycle.

        Unmanaged appliances are excluded — EMS neither reads nor commands
        them, so a dead sensor on one must not hold up the whole controller.
        """
        ids = [config.get(key) for key in _GLOBAL_SOURCE_KEYS]
        for app in inputs:
            if app.mode == MODE_UNMANAGED:
                continue
            raw = raw_by_uid.get(app.uid) or {}
            ids.extend(raw.get(key) for key in _APPLIANCE_SOURCE_KEYS)
        return ids

    def _seed_states_from_entities(self, inputs, now) -> None:
        """Adopt the appliances' real state as the controller's starting point.

        Without this the controller starts every appliance from ``is_on=False``
        with no hysteresis timer, so its first cycle commands a running
        appliance off — and, because ``last_change`` is still None, commands it
        straight back on once the budget recovers. Seeding the switch state, the
        setpoint the appliance is actually running at, and the timers makes the
        first cycle a no-op for an appliance that should keep running; a genuine
        off decision still has to survive the hysteresis window first.
        """
        adopted = []
        for app in inputs:
            state = self.appliance_states.setdefault(app.uid, ems_core.ApplianceState())
            # Physical truth wins over whatever was restored: the appliance may
            # have been switched by hand while we were down.
            state.is_on = app.entity_on
            if state.last_change is None:
                # Nothing restored (fresh install, or a newly added appliance):
                # arm the window now so the first decision can't flip it.
                state.last_change = now
            if app.has_fixed_amps and app.fixed_amps_value is not None:
                state.last_fixed_amps = app.fixed_amps_value
                if state.last_fixed_amps_change is None:
                    state.last_fixed_amps_change = now
            adopted.append(f"{app.name}={'on' if app.entity_on else 'off'}")
        # Force the adopted state out to the store on the next persist.
        self._persisted_snapshot = None
        _LOGGER.info(
            "control starting; adopted appliance state: %s", ", ".join(adopted) or "none"
        )

    def _warmup_data(self, appliances, inputs, names_by_uid, managed_amps, kw, missing):
        """Observation-only result while warming up: report, never decide."""
        on_names = [
            names_by_uid.get(app.uid, app.uid)
            for app in inputs
            if app.mode != MODE_UNMANAGED and app.entity_on
        ]
        return {
            # The budget isn't trustworthy yet. Report unknown rather than a
            # blank 0, which would read as a genuine "no surplus".
            "excess_amps": None,
            "excess_kw": None,
            # No decision yet -> the force-charge binary sensors read unavailable.
            "forced_appliance_ids": None,
            "forced_appliances": [],
            "not_consuming_appliance_ids": None,
            "not_consuming_appliances": [],
            "active_appliances": on_names,
            "appliances": appliances,
            "auto_switching": self.enabled,
            "managed_amps": round(managed_amps, 2),
            "managed_power": kw(managed_amps),
            "warming_up": True,
            "warming_up_waiting_on": missing,
        }

    async def _async_update_data(self):
        # Never command anything while Home Assistant is going down. The
        # sources are being torn down in arbitrary order, so any cycle run now
        # sees a half-dead house: templates over unavailable entities collapse
        # to defaults, and a decision made on them is written to the appliance
        # and then frozen there for the whole restart. Hold the last data
        # (the entities keep their values) and decide nothing.
        if self.hass.is_stopping:
            _LOGGER.debug("==== EMS cycle skipped: Home Assistant is stopping ====")
            return self.data if self.data is not None else {}

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

        def _names(uids) -> list[str]:
            return [names_by_uid.get(u, u) for u in uids]

        def _kw(amps: float) -> float:
            """Amps → kW at this cycle's voltage (for the *_kw sensor values)."""
            return round(amps * voltage / 1000.0, 2)

        # Amps the managed appliances are currently drawing (added back below).
        managed_amps = ems_core.managed_consumption_amps(inputs)
        _LOGGER.debug("managed appliances currently drawing %.2f A", managed_amps)

        # Hold every decision until the source sensors are actually reporting.
        # At HA start this entry is set up before the inverter/meter/power
        # integrations have populated, and a blank reading looks exactly like
        # "no surplus" — which used to switch a running appliance off and then,
        # once the sensors arrived, back on again.
        if not self._control_started:
            if self._warmup_deadline is None:
                self._warmup_deadline = now + timedelta(seconds=STARTUP_GRACE_SECONDS)
            missing = not_ready_entities(
                self.hass, self._source_entities(config, inputs, raw_by_uid)
            )
            expired = now >= self._warmup_deadline
            # Critical demand overrides the wait: the shed protects the monthly
            # peak and works off the appliances' own on/off state, not the
            # budget, so it is safe to run on incomplete sources.
            critical = self._is_demand_critical()
            if missing and not expired and not critical:
                if not self._warmup_logged:
                    self._warmup_logged = True
                    _LOGGER.info(
                        "warming up: holding control until these report: %s",
                        ", ".join(missing),
                    )
                # No record_excess here on purpose: blank samples in the
                # averaging history kept suppressing the run condition for
                # minutes after the sensors had come good.
                _LOGGER.debug(
                    "==== EMS cycle end: warming up (waiting on %s) ====", ", ".join(missing)
                )
                return self._warmup_data(
                    appliances, inputs, names_by_uid, managed_amps, _kw, missing
                )
            if missing:
                _LOGGER.warning(
                    "starting control with %s still unavailable (%s)",
                    ", ".join(missing),
                    "demand critical" if critical else f"warm-up grace of {STARTUP_GRACE_SECONDS}s expired",
                )
            self._seed_states_from_entities(inputs, now)
            self._control_started = True

        demand = self._demand_data()
        if demand:
            _LOGGER.debug(
                "demand: estimate=%s kW high_limit=%s crit_limit=%s high=%s critical=%s "
                "max_to_peak=%s kW projected_ceiling=%s kW",
                demand.get("estimate"), demand.get("high_limit"), demand.get("critical_limit"),
                demand.get("high"), demand.get("critical"), demand.get("max_power_to_peak"),
                demand.get("projected_ceiling"),
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
                self._note_peak_action(now)
            else:
                # Not intervening: nothing is "kept at min", but the appliances
                # are still drawing — report the real managed draw, not 0.
                kept, kept_amps = [], managed_amps
                _LOGGER.warning("demand CRITICAL (auto-switching off; not intervening)")
            self._persist_state()
            kept_names = _names(kept)
            _LOGGER.debug(
                "==== EMS cycle end: critical (kept at min: %s) ====", kept_names or "none"
            )
            return {
                "excess_amps": 0,
                "excess_kw": 0,
                "forced_appliance_ids": [],
                "forced_appliances": [],
                "not_consuming_appliance_ids": [],
                "not_consuming_appliances": [],
                "active_appliances": kept_names,
                "appliances": appliances,
                "auto_switching": self.enabled,
                "demand_critical": True,
                "peak_limited_appliances": kept_names if self.enabled else [],
                "peak_action_until": self._peak_action_until,
                "managed_amps": round(kept_amps, 2),
                "managed_power": _kw(kept_amps),
            }

        if not self.enabled:
            # Persist anyway: the adopted startup state (and any appliance added
            # while switched off) must survive to the next restart, or the first
            # cycle after re-enabling starts from a blank slate again.
            self._persist_state()
            _LOGGER.debug(
                "auto-switching OFF -> optimizer skipped; spare=%.2f A", excess_amps
            )
            _LOGGER.debug("==== EMS cycle end: auto-switching off ====")
            return {
                "excess_amps": round(excess_amps, 2),
                "excess_kw": _kw(excess_amps),
                "forced_appliance_ids": [],
                "forced_appliances": [],
                "not_consuming_appliance_ids": [],
                "not_consuming_appliances": [],
                "active_appliances": [],
                "appliances": appliances,
                "auto_switching": False,
                "managed_amps": round(managed_amps, 2),
                "managed_power": _kw(managed_amps),
            }

        peak_amps = self._demand_amps("high_limit", voltage)
        proj_amps = self._demand_amps("projected_ceiling", voltage)
        _LOGGER.debug(
            "peak headroom: %s%s",
            "off (no demand feature)" if peak_amps is None else f"{peak_amps:.2f} A",
            "" if proj_amps is None else f", projected {proj_amps:.2f} A",
        )
        # A frozen grid feed makes the surplus add-back unverifiable; hold
        # setpoints (no ramp-up) so it can't feed a runaway. Both directions of
        # flow count as proof of life: the net/export sensor and the meter's
        # import sensor. A signed net sensor derived from a template only writes
        # state when its value *changes*, so a flat load can leave it quiet for
        # minutes while the meter behind it is reporting normally — judging one
        # side alone holds the ramp on a healthy system.
        grid_stale = sensors_stale(
            self.hass,
            (
                config.get(CONF_GRID_EXPORT),
                config.get(CONF_DEMAND_POWER_CONSUMED_SENSOR),
            ),
            GRID_STALE_SECONDS,
            now,
        )
        if grid_stale:
            _LOGGER.warning(
                "grid sensors stale (no import or export report in >%ds); holding setpoints",
                GRID_STALE_SECONDS,
            )
        result = ems_core.optimize(
            inputs, self.appliance_states, excess_amps, peak_amps, energy, now,
            hold_ramp=grid_stale, proj_amps=proj_amps,
        )
        await control.apply_commands(
            self.hass, result.active, result.fixed_amps_commands, inputs, raw_by_uid
        )
        if result.peak_limited:
            self._note_peak_action(now)
        self._persist_state()

        active_names = _names(result.active)
        forced_names = _names(result.forced)
        _LOGGER.debug(
            "result: active=%s | forced=%s | decided draw=%.2f A | spare(excess)=%.2f A",
            active_names, forced_names, result.decided_amps, excess_amps,
        )
        _LOGGER.debug("==== EMS cycle end ====")
        return {
            "excess_amps": round(excess_amps, 2),
            "excess_kw": _kw(excess_amps),
            # Appliances the car-SoC rule drove as `forced` this cycle: uids for
            # the per-appliance binary sensors, names for the status attribute.
            "forced_appliance_ids": list(result.forced),
            "forced_appliances": forced_names,
            # Switched on by EMS but drawing nothing past the grace period.
            "not_consuming_appliance_ids": list(result.not_consuming),
            "not_consuming_appliances": _names(result.not_consuming),
            # Throttled, cut or shed for the quarter-hour peak this cycle, and
            # until when that counts as "acting" for the demand side's hold.
            "peak_limited_appliances": _names(result.peak_limited),
            "peak_action_until": self._peak_action_until,
            "active_appliances": active_names,
            "appliances": appliances,
            "auto_switching": True,
            # Report the draw the optimizer just decided (fresh setpoints), so
            # Managed Consumption updates this cycle instead of lagging one.
            "managed_amps": round(result.decided_amps, 2),
            "managed_power": _kw(result.decided_amps),
        }
