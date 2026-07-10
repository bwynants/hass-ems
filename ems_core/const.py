"""Domain constants and numeric defaults for the EMS decision core.

Pure values only — no Home Assistant imports and no config-key strings (those
stay in the integration's ``const.py``). The HA ``const.py`` re-exports these so
existing imports keep working.
"""
from __future__ import annotations

# Runtime modes. "unmanaged" means EMS ignores the appliance entirely (it
# neither switches it on/off nor adjusts it) — the replacement for the old
# per-appliance enable switch.
MODE_FORCED = "forced"
MODE_PARTIAL_SOLAR = "partial_solar"
MODE_FULL_SOLAR = "full_solar"
MODE_OFF_PEAK = "off_peak"
MODE_UNMANAGED = "unmanaged"
MODES = [MODE_UNMANAGED, MODE_OFF_PEAK, MODE_PARTIAL_SOLAR, MODE_FULL_SOLAR, MODE_FORCED]
DEFAULT_MODE = MODE_PARTIAL_SOLAR

# Battery SoC-protection modes.
BATTERY_MODE_STRICT = "strict"
BATTERY_MODE_RELAXED = "relaxed"
DEFAULT_BATTERY_MODE = BATTERY_MODE_STRICT
BATTERY_PROTECTION_MODES = [BATTERY_MODE_STRICT, BATTERY_MODE_RELAXED]

# Appliance / control defaults.
DEFAULT_VOLTAGE = 230
DEFAULT_MIN_SOC = 20
DEFAULT_HYSTERESIS = 120
DEFAULT_MIN_AMPS = 6
DEFAULT_PRIORITY = 100
DEFAULT_MAX_AMPS = 16
DEFAULT_FIXED_AMPS_HYSTERESIS_SECONDS = 120
DEFAULT_AVERAGING = 180
DEFAULT_SOC_HYSTERESIS = 5
DEFAULT_IGNORE_PROTECTION = False
DEFAULT_AUTO_SWITCHING = True
DEFAULT_IS_BATTERY = False
# Minimum excess watts required to run in partial_solar mode (per appliance).
DEFAULT_PARTIAL_MIN_EXCESS = 1000

# Extra grid (peak) headroom, in amps, that a forced appliance leaves unused
# when scaling against the quarter-hour demand limit. Filling the headroom
# completely drives the peak to the cap, which sheds the appliance and causes
# on/off oscillation; reserving this margin lets it run steadily below the cap.
FORCED_PEAK_HEADROOM_AMPS = 1.0

# Solar production (W) at or below which it is treated as "sundown" (no solar to
# follow). An off_peak-mode appliance, during off-peak hours at sundown, then
# scales up on cheap grid like a forced appliance (additive budget, up to the
# peak limit) instead of dribbling at min_amps.
SUNDOWN_SOLAR_W = 100.0

# Off-peak breakeven default tariff rates (€/kWh). Not exposed in the UI, so the
# breakeven gate always uses these unless a config override is supplied.
DEFAULT_COST_PEAK = 0.40
DEFAULT_COST_OFFPEAK = 0.20
DEFAULT_GAIN_EXPORT = 0.10

# Main coordinator cycle interval (s) and the fixed_amps setpoint quantisation.
# The setpoint is quantised to FIXED_AMPS_STEP-amp increments, rounded DOWN with
# the deadband margin: run at step N only once N + margin is available (e.g. with
# a 0.5 A step and 0.25 A deadband, 6.8 -> 6.5, and it steps up to 7.0 only at
# 7.25). The margin also acts as a deadband against flicker. A finer step suits
# chargers with sub-amp (mA) current regulation, e.g. Peblar.
CONTROLLER_UPDATE_INTERVAL = 10
FIXED_AMPS_DEADBAND = 0.25
FIXED_AMPS_STEP = 0.5

# How much wall-clock history to keep per appliance for excess averaging, and
# the resulting sample count (one sample per cycle). Derived from the cycle
# interval so the retained duration stays fixed (1 hour) regardless of the poll
# rate — this is also the longest averaging window that can be honoured in full.
APPLIANCE_HISTORY_SECONDS = 3600
APPLIANCE_HISTORY_MAXLEN = APPLIANCE_HISTORY_SECONDS // CONTROLLER_UPDATE_INTERVAL

# Quarterly demand defaults (kW). Thresholds entered as plain numbers in config.
DEFAULT_MINIMAL_PEAK_WARNING = 4.0
DEFAULT_PEAK_HIGH_SAFETY = 0.4
DEFAULT_PEAK_CRITICAL_SAFETY = 0.2

# Quarterly demand constants (match peakpower.yaml).
DEMAND_QUARTER_SECONDS = 900
DEMAND_MAX_POWER_CAP_KW = 40.0
DEMAND_HYSTERESIS_KW = 0.2
DEMAND_HIGH_GATE_MINUTES = 6
DEMAND_CRITICAL_GATE_MINUTES = 8
DEMAND_UPDATE_INTERVAL_SECONDS = 10
# Minutes at the END of a quarter during which the optimizer stops using the
# (tail-ballooning) Max Power To Peak headroom and falls back to the sustainable
# High-limit ceiling, so a last-moment ramp-up can't spike the next quarter's
# projection (the boundary sawtooth).
DEMAND_RAMP_FREEZE_MINUTES = 2
# Cap on how far the optimizer may dip into banked Max Power To Peak headroom,
# as a multiple of the High limit. The raw value balloons toward the quarter
# tail (up to DEMAND_MAX_POWER_CAP_KW); this bounds the instantaneous over-draw
# to a sane multiple so the appliance doesn't spike to its full max.
DEMAND_MAX_TO_PEAK_CAP_FACTOR = 1.5
# How long held (last-known) demand readings may bridge a source dropout
# before the feature reports itself unavailable instead of acting on stale
# data (frozen estimates re-latching Critical every quarter, frozen headroom).
DEMAND_STALE_SECONDS = 300
