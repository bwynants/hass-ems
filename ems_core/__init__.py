"""Energy Management System — pure decision core.

A Home-Assistant-independent library: it imports only the standard library and
other ``ems_core`` modules. The HA integration is a thin shell that reads
sensors into the dataclasses in :mod:`ems_core.models`, calls the functions
here, and applies the returned commands.

Public API is re-exported for convenience so callers can ``from ems_core import
optimize, compute_demand, ...``.
"""
from __future__ import annotations

from .models import (
    ApplianceInput,
    ApplianceState,
    EnergyInput,
    SetFixedAmps,
    FixedAmpsDecision,
    OptimizeResult,
    ShedResult,
    DemandResult,
)
from .energy import (
    normalise_power,
    calculate_excess_amps,
    scaled_amps,
    breakeven,
    actual_amps,
    managed_consumption_amps,
)
from .fixed_amps import step_target, decide
from .demand import compute_demand
from .optimizer import (
    optimize, critical_shed, record_excess, force_charge_active, effective_mode,
    not_consuming,
)

__all__ = [
    "ApplianceInput",
    "ApplianceState",
    "EnergyInput",
    "SetFixedAmps",
    "FixedAmpsDecision",
    "OptimizeResult",
    "ShedResult",
    "DemandResult",
    "normalise_power",
    "calculate_excess_amps",
    "scaled_amps",
    "breakeven",
    "actual_amps",
    "managed_consumption_amps",
    "step_target",
    "decide",
    "compute_demand",
    "optimize",
    "critical_shed",
    "record_excess",
    "force_charge_active",
    "effective_mode",
    "not_consuming",
]
