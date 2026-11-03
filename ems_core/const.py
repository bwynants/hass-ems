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

# Appliance / control defaults.
DEFAULT_VOLTAGE = 230
DEFAULT_HYSTERESIS = 120
DEFAULT_MIN_AMPS = 6
DEFAULT_PRIORITY = 100
DEFAULT_MAX_AMPS = 16
DEFAULT_FIXED_AMPS_HYSTERESIS_SECONDS = 120
DEFAULT_AVERAGING = 180
DEFAULT_AUTO_SWITCHING = True
# Minimum averaged excess, in AMPS, required to run in partial_solar mode (per
# appliance). Compared directly against the averaged surplus amps; the start
# side carries ON_OFF_HYSTERESIS_AMPS on top.
DEFAULT_PARTIAL_MIN_EXCESS_AMPS = 4.0

# Extra grid (peak) headroom, in amps, that a forced appliance leaves unused
# when scaling against the quarter-hour demand limit. Filling the headroom
# completely drives the peak to the cap, which sheds the appliance and causes
# on/off oscillation; reserving this margin lets it run steadily below the cap.
FORCED_PEAK_HEADROOM_AMPS = 1.0

# Consecutive cycles the peak gate must see a RUNNING appliance's budget below
# its min_amps before it is shed (3 cycles ≈ 30 s at the 10 s interval). The
# appliance's own draw is added back into the budget from its power sensor,
# which polls slower than the grid meter: for a cycle after a charger starts
# the meter already shows the full import while the sensor still reports the
# ramp, so the budget momentarily reads as "no room for the appliance" — and a
# single-cycle gate shed it 10 s after every start. During the grace cycles
# the appliance is throttled to min_amps, so a real overload is still curbed
# at once; only the switch-off waits. A 30 s overshoot cannot move a
# quarter-hour average meaningfully. Starting is never debounced: an OFF
# appliance stays off the moment the budget is short.
PEAK_GATE_SHED_CYCLES = 3

# Solar production (W) at or below which it is treated as "sundown" (no solar to
# follow). An off_peak-mode appliance, during off-peak hours at sundown, then
# scales up on cheap grid like a forced appliance (additive budget, up to the
# peak limit) instead of dribbling at min_amps.
SUNDOWN_SOLAR_W = 100.0

# EV charger: below this car SoC (%) a plugged-in car is charged as if the
# appliance were in `forced` mode. 0 disables the override (nothing is < 0).
DEFAULT_FORCE_CHARGE_BELOW_SOC = 0

# "Should be consuming but isn't" diagnostic. An appliance EMS has switched on
# that draws less than NOT_CONSUMING_AMPS for NOT_CONSUMING_GRACE_SECONDS is
# flagged — the grace covers a charger's start-up delay. A car at or above
# SOC_FULL_PERCENT is exempt: tapering off near full is normal, not a fault.
NOT_CONSUMING_AMPS = 0.5
NOT_CONSUMING_GRACE_SECONDS = 300
SOC_FULL_PERCENT = 95

# Off-peak breakeven default tariff rates (€/kWh). Not exposed in the UI, so the
# breakeven gate always uses these unless a config override is supplied.
DEFAULT_COST_PEAK = 0.40
DEFAULT_COST_OFFPEAK = 0.20
DEFAULT_GAIN_PEAK = 0.10
DEFAULT_GAIN_OFFPEAK = 0.10

# How long after startup the controller may wait for its source sensors before
# it commands anything. Our config entry is set up before the inverter/meter
# integrations have populated, and deciding from those blank readings switches a
# running appliance off (and back on once they arrive). Bounded: past the grace
# the controller starts anyway, so a permanently removed sensor can't freeze it.
STARTUP_GRACE_SECONDS = 180

# A grid feed with no report from either direction for this long is treated as
# frozen: the optimizer then holds appliance setpoints (no ramp-up), so a stale
# grid reading can't feed a surplus-add-back runaway for appliances with no power
# sensor.
#
# Generous on purpose. Meter integrations commonly publish only on *change* (an
# ESPHome P1 reader with a delta filter, MQTT, a template chain off either), so a
# genuinely flat house load leaves every grid source quiet while the feed is
# perfectly healthy — measured quiet gaps of ~7 min overnight. Below ~10 min this
# threshold mostly reports flat load, not a fault. A feed that really dies goes
# `unavailable`, which the read fallback and the start-up gate already cover.
GRID_STALE_SECONDS = 600

# Main coordinator cycle interval (s) and the fixed_amps setpoint quantisation.
# The setpoint is quantised to FIXED_AMPS_STEP-amp increments, rounded DOWN with
# the deadband margin: run at step N only once N + margin is available (e.g. with
# a 0.5 A step and 0.25 A deadband, 6.8 -> 6.5, and it steps up to 7.0 only at
# 7.25). The margin also acts as a deadband against flicker. A finer step suits
# chargers with sub-amp (mA) current regulation, e.g. Peblar — which is why
# these are only the FALLBACKS: an appliance's "ampere to use" entity publishes
# its own ``step``, and that is what its setpoint is quantised to (with the
# deadband at half of it, the same ratio as the two defaults below).
CONTROLLER_UPDATE_INTERVAL = 10
FIXED_AMPS_DEADBAND = 0.25
FIXED_AMPS_STEP = 0.5

# On/off hysteresis (deadband) in amps, independent of the setpoint step above.
# An appliance that is OFF starts only once min_amps + this margin is available;
# once ON it stays on down to min_amps. Fixed rather than derived from the
# entity's step: a 0.1 A charger would otherwise start and stop on 0.1 A of
# noise around its minimum, damped only by the time-based hysteresis window.
ON_OFF_HYSTERESIS_AMPS = 0.5

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

# Quarterly demand constants.
DEMAND_QUARTER_SECONDS = 900
DEMAND_MAX_POWER_CAP_KW = 40.0
DEMAND_HYSTERESIS_KW = 0.2
# Minute in the quarter from which the High / Critical flags may LATCH (the
# binary sensors and the critical backstop shed). Earlier the projection
# rests on too little of the quarter to be trusted.
DEMAND_HIGH_GATE_MINUTES = 6
DEMAND_CRITICAL_GATE_MINUTES = 8
# Minute in the quarter from which High / Critical may be ANNOUNCED (the
# event entity). Later than the latch on purpose: the sensors may show the
# situation early, but a spoken warning two minutes later is still in time
# and skips the projections that settle on their own.
DEMAND_HIGH_ALERT_MINUTES = 8
DEMAND_CRITICAL_ALERT_MINUTES = 10
# Consecutive demand cycles (~10 s each) the flag must stay latched before it
# is announced. The projection reacts to a single sample of the instantaneous
# draw, so a 15 s burst (a kettle, a one-cycle setpoint glitch) crossed the
# limit and was announced as a peak risk the next cycle had already undone.
DEMAND_HIGH_ALERT_SUSTAIN_CYCLES = 3
DEMAND_CRITICAL_ALERT_SUSTAIN_CYCLES = 2
# While the controller is throttling or shedding for peak, hold the
# announcement for this long after its last action: warn only if the
# situation is still there once the shed has had its effect on the meter.
PEAK_ACTION_HOLD_SECONDS = 60

# Proactive peak control. The optimizer projects the quarter: the grid draw
# that would end the quarter at (High limit − this margin) becomes the
# ceiling for the rest of the quarter, whenever it is lower than the
# instantaneous High ceiling. Setpoints are lowered against it first; the
# shed is the last resort. kW below the High limit.
DEFAULT_PEAK_SHED_SAFETY = 0.3
# The projection is skipped this long into a quarter: the current-average
# source resets a few seconds after the boundary, and a stale value with
# ~900 s remaining projects a ceiling that is nonsense in either direction.
DEMAND_PROJECTION_SKIP_SECONDS = 60
# Setpoint anti-flap. An upward write may raise the setpoint by at most this
# many amps per write (ramp-down is never limited); and after a reduction that
# was grid-ceiling driven (a base-load step, a quarter tightening) no upward
# write happens for PEAK_CUT_HOLD_SECONDS — the load that pushed us down is
# usually still there, and sawing the setpoint up and down is what makes the
# meter and the projection chase each other.
FIXED_AMPS_MAX_STEP_UP = 2.0
PEAK_CUT_HOLD_SECONDS = 60
DEMAND_UPDATE_INTERVAL_SECONDS = 10
# How long held (last-known) demand readings may bridge a source dropout
# before the feature reports itself unavailable instead of acting on stale
# data (frozen estimates re-latching Critical every quarter, frozen headroom).
DEMAND_STALE_SECONDS = 300
