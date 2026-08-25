"""Dataclasses for the EMS decision core — plain data, stdlib only.

These are the boundary types between the HA shell and the pure core: the shell
reads sensors/config into the ``*Input`` types, the core returns the ``*Result``
types and ``SetFixedAmps`` commands, and the shell applies them.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from .const import APPLIANCE_HISTORY_MAXLEN, FIXED_AMPS_STEP


@dataclass
class ApplianceInput:
    """A fully-resolved snapshot of one appliance for a single control cycle.

    All sensor reads and the persisted runtime mode are resolved by the shell up
    front so the core never touches Home Assistant.

    ``uid`` is the stable identifier (the config subentry id) used to key the
    runtime ``ApplianceState`` between cycles; ``name`` is the user-editable
    display name used in logs and commands. Keying state by ``uid`` means a
    rename doesn't reset hysteresis/averaging state and two appliances that
    happen to share a name can't corrupt each other's state.
    """
    uid: str
    name: str
    priority: int
    min_amps: int
    max_amps: int
    mode: str
    has_fixed_amps: bool
    fixed_amps_value: float | None
    fixed_amps_hold_seconds: int
    partial_min_excess: float
    hysteresis_seconds: int
    averaging_window_seconds: int
    connected: bool
    entity_on: bool
    measured_amps: float | None
    is_battery: bool
    ignore_battery_protection: bool
    min_soc_override: float | None
    soc_hysteresis_percent: float
    # True when a power sensor is configured for this appliance. When it is but
    # ``measured_amps`` is None (sensor unavailable), the surplus add-back must
    # NOT fall back to the fixed_amps setpoint — see energy.actual_amps.
    has_power_sensor: bool = False
    # Granularity of this appliance's setpoint, taken from the "ampere to use"
    # entity's own ``step``. The default only applies when that entity doesn't
    # publish one.
    fixed_amps_step: float = FIXED_AMPS_STEP
    # Battery-only fields (populated when is_battery is True).
    battery_soc: float | None = None
    battery_min_soc: float | None = None
    battery_protection_mode: str | None = None


@dataclass
class ApplianceState:
    """Mutable runtime state for one appliance, persisted between cycles."""
    is_on: bool = False
    last_change: datetime | None = None
    last_fixed_amps: float | None = None
    last_fixed_amps_change: datetime | None = None
    excess_history: deque = field(
        default_factory=lambda: deque(maxlen=APPLIANCE_HISTORY_MAXLEN)
    )
    soc_blocked: bool = False
    # Last real measured draw (amps), held across a power-sensor dropout so the
    # add-back doesn't fall through to the commanded setpoint (runaway guard).
    last_measured_amps: float | None = None
    # True while a full_solar appliance is in the transient-spike condition
    # (current budget covers, sustained average doesn't) — used to log the
    # reason once on entry instead of every cycle.
    full_solar_spike: bool = False


@dataclass
class EnergyInput:
    """Grid/solar snapshot and tariff context for a control cycle."""
    grid_w: float | None
    solar_w: float
    house_load_w: float
    voltage: float
    off_peak: bool
    cost_peak: float
    cost_offpeak: float
    gain_export: float


@dataclass
class BatteryStatus:
    """Derived battery SoC-protection status (None fields ⇒ no battery)."""
    soc: float | None = None
    min_soc: float | None = None
    protection_mode: str | None = None


@dataclass
class SetFixedAmps:
    """Command: write ``value`` amps to the appliance's fixed_amps entity.

    ``uid`` is the appliance's stable id (config subentry id), so the command
    targets the right appliance even when display names collide.
    """
    uid: str
    value: float


@dataclass
class FixedAmpsDecision:
    """Outcome of the fixed_amps stepping/gating logic.

    ``effective`` is the amperage to use for budgeting; ``write_value`` is the
    value to command (None ⇒ hold, don't write).
    """
    effective: float
    write_value: float | None


@dataclass
class OptimizeResult:
    """Result of a normal optimize cycle. ``active`` holds appliance uids."""
    active: list[str]
    decided_amps: float
    fixed_amps_commands: list[SetFixedAmps]


@dataclass
class ShedResult:
    """Result of a critical-demand shed cycle. ``kept`` holds appliance uids."""
    kept: list[str]
    kept_amps: float
    fixed_amps_commands: list[SetFixedAmps]


@dataclass
class DemandResult:
    """Quarter-hour demand computation (all kW except the boolean flags).

    ``high``/``critical`` are also the new latch state — feed them back as the
    ``high_on``/``critical_on`` inputs on the next cycle.
    """
    estimate: float
    month_max: float
    high_limit: float
    critical_limit: float
    max_power_to_peak: float
    high: bool
    critical: bool
