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
    # partial_solar threshold in AMPS of averaged surplus (0 = no threshold).
    partial_min_excess_amps: float
    hysteresis_seconds: int
    averaging_window_seconds: int
    connected: bool
    entity_on: bool
    measured_amps: float | None
    # True when a power sensor is configured for this appliance. When it is but
    # ``measured_amps`` is None (sensor unavailable), the surplus add-back must
    # NOT fall back to the fixed_amps setpoint — see energy.actual_amps.
    has_power_sensor: bool = False
    # Granularity of this appliance's setpoint, taken from the "ampere to use"
    # entity's own ``step``. The default only applies when that entity doesn't
    # publish one.
    fixed_amps_step: float = FIXED_AMPS_STEP
    # EV charger: the car's state of charge (%) and the per-appliance threshold
    # below which a plugged-in car is charged as `forced`. Both None when the
    # appliance has no SoC sensor; ``soc`` is also None when the sensor is
    # unavailable/unknown (no car plugged in) — either disables the override.
    soc: float | None = None
    force_charge_below_soc: float | None = None


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
    # Last real measured draw (amps), held across a power-sensor dropout so the
    # add-back doesn't fall through to the commanded setpoint (runaway guard).
    last_measured_amps: float | None = None
    # True while a full_solar appliance is in the transient-spike condition
    # (current budget covers, sustained average doesn't) — used to log the
    # reason once on entry instead of every cycle.
    full_solar_spike: bool = False
    # True while the car-SoC force-charge override is active — used to log the
    # transitions once instead of every cycle.
    force_charging: bool = False
    # Last real car SoC reading, held across a sensor dropout while the car
    # stays plugged in (cloud car sensors go `unknown` intermittently); cleared
    # on disconnect so it can never carry over to another car.
    last_soc: float | None = None
    # When the appliance was first seen switched on by EMS yet drawing nothing;
    # None while it is off or drawing normally. Drives the "not consuming" flag.
    not_consuming_since: datetime | None = None
    # Consecutive cycles the peak gate has found this running appliance's
    # budget below min_amps; it is shed at PEAK_GATE_SHED_CYCLES. Reset to 0
    # whenever the budget covers min again or the appliance is off. Transient
    # (not persisted): a restart re-counts from a fresh measurement.
    peak_gate_low_cycles: int = 0


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
    # Export compensation per tariff period (€/kWh). Most contracts pay the
    # same in both, but dual-rate injection tariffs exist, so they are kept
    # apart and the one for the CURRENT period is used (see ``gain_now``).
    gain_peak: float
    gain_offpeak: float

    @property
    def gain_now(self) -> float:
        """Export compensation that applies in the current tariff period.

        This is the income forgone by self-consuming a kWh of solar *now*, so
        it is the opportunity-cost term of the off_peak-mode breakeven.
        """
        return self.gain_offpeak if self.off_peak else self.gain_peak


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
    # Uids whose mode the car-SoC rule overrode to `forced` this cycle —
    # reported even when the peak gate then kept the appliance off.
    forced: list[str] = field(default_factory=list)
    # Uids EMS has switched on that have drawn (almost) nothing for longer than
    # the grace period — the "should be consuming but isn't" problem flag.
    not_consuming: list[str] = field(default_factory=list)


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
