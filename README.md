# Energy Management System

A Home Assistant custom integration (**EMS**) that automatically manages household appliances based on solar production, battery state of charge (SoC), grid export, and time-of-use tariffs.

---

## Features

- **Priority-based scheduling** — appliances run in priority order when solar surplus is available.
- **Per-appliance runtime modes** — `partial_solar`, `forced`, or `off_peak`, selectable from the HA UI and persisted across restarts.
- **Hysteresis & debounce** — configurable on/off delay and minimum switch interval prevent rapid cycling.
- **Averaging window** — excess power is averaged over a rolling window to smooth out transient spikes.
- **Battery SoC protection** — appliances are blocked when battery drops below a configurable minimum, with hysteresis to avoid flapping.
- **Capacity-tariff (peak) protection** — appliance amps are progressively lowered (or the appliance turned off) using the quarter-hour demand prediction so the monthly peak is never reached; a Critical backstop forces everything off if needed.
- **Off-peak awareness** — appliances in `off_peak` mode run freely during off-peak hours; a breakeven calculation gates them during peak hours.
- **Connection sensor** — optional per-appliance binary sensor to skip appliances that are physically disconnected.

---

## Requirements

- Home Assistant 2024.6.0 or later.
- A sensor entity providing **solar production** in Watts (required).
- Optional: house load sensor, net grid power sensor (import positive / export negative), battery SoC sensor, off-peak sensor.

---

## Installation

1. Copy the `solar_appliance_manager` folder into your `custom_components` directory:
   ```
   config/custom_components/solar_appliance_manager/
   ```
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration** and search for **Energy Management System**.

---

## Configuration

### Initial Setup

| Field | Required | Description |
|---|---|---|
| `solar_production_sensor` | ✅ | Entity ID of your solar production sensor (W) |
| `house_load_sensor` | ❌ | Entity ID of your house load sensor (W) |
| `grid_export_sensor` | ❌ | Entity ID of your net grid power sensor (W), **import positive / export negative**. The exported (negative) part is used as the surplus budget |
| `off_peak_sensor` | ❌ | Binary sensor — `on` during off-peak hours |
| `voltage` | ❌ | Grid voltage in Volts (default: `230`) |
| `demand_power_current_peak_sensor` | ❌ | Sensor with the running average power over the current 15-min quarter (kW/W). Enables the quarterly-demand feature |
| `demand_power_consumed_sensor` | ❌ | Current power-consumption sensor, used to project demand to the quarter's end |
| `demand_month_max_sensor` | ❌ | Sensor with the highest quarter-hour demand recorded this month |
| `minimal_peak_warning` | ❌ | Floor for the monthly peak, kW (default: `4`) — limits never drop below this |
| `peak_high_safety` | ❌ | kW below the monthly peak for the **High** limit (default: `0.4`) |
| `peak_critical_safety` | ❌ | kW below the monthly peak for the **Critical** limit (default: `0.2`) |

> **Battery protection** is configured per-appliance, not globally. Add an appliance and flag it as a battery (`is_battery`); it then supplies the SoC sensor, threshold (`min_soc`) and protection mode used to block the other appliances. See *Adding Appliances* below.

> **Quarterly demand (capacity tariff).** When the three demand source sensors are set, EMS exposes native entities for the Belgian *capaciteitstarief*: `Quarterly Demand Estimate`, `Quarterly Demand Month Max`, `Quarterly Demand High Limit`, `Quarterly Demand Critical Limit`, `Max Power To Peak Limit`, and binary sensors `Quarterly Demand High` / `Quarterly Demand Critical`. They refresh every ~10 s. Peak-aware control is **always on** (it simply has no effect until the demand sensors are configured): the optimizer uses `Max Power To Peak Limit` to **progressively lower** each appliance's `fixed_amps` setpoint (and turn it off when even the minimum won't fit) so demand stays under the **High** limit — preventing the peak — and force-stops all managed appliances as a backstop if demand still reaches **Critical**. Threshold values are entered as plain numbers (no `input_number` helper needed).

Excess power (amps) is calculated from the net grid power when a grid sensor is set
(export, i.e. negative, becomes surplus; while importing it is clamped to 0),
otherwise from the solar − house-load estimate:
```
excess_amps = -grid_power_W / voltage      (when grid_power_W < 0, else 0)
            = (solar_W - house_load_W) / voltage   (fallback, no grid sensor)
```
The current draw of managed appliances is then added back so allocation works from
the gross surplus.

### Adding Appliances

After setup, open the EMS **Options** to add appliances one at a time.

| Field | Required | Description |
|---|---|---|
| `name` | ✅ | Display name (must be unique) |
| `entity_id` | ✅ | Switch entity to control |
| `priority` | ❌ | Lower number = higher priority (default: `100`) |
| `min_amps` | ✅ | Minimum amps needed to run (default: `6`) |
| `max_amps` | ✅ | Maximum amps the appliance draws (default: `32`) |
| `fixed_amps` | ❌ | Number entity; optimizer dynamically sets this entity based on available excess, scaled between `min_amps` and `max_amps` (e.g., `input_number.my_amps`) |
| `fixed_amps_hysteresis_seconds` | ❌ | Minimum seconds the `fixed_amps` setpoint is held before it may change to another value, preventing it from switching too often (default: `120`) |
| `power_sensor` | ❌ | Sensor (W) reporting the appliance's real power draw; when set, the optimizer deducts measured consumption from the budget instead of estimating. Falls back to `fixed_amps` setpoint, then `min_amps` |
| `partial_min_excess` | ❌ | Minimum excess Watts required in `partial_solar` mode (default: `1000`) |
| `connected_sensor` | ❌ | Binary sensor — `off` means appliance is disconnected and will be skipped |
| `hysteresis_seconds` | ❌ | Seconds to hold state before allowing next change (default: `120`) |
| `averaging_window_seconds` | ❌ | Rolling window in seconds for excess power averaging (default: `180`) |
| `min_soc_override` | ❌ | Per-appliance minimum SoC %, overrides the battery appliance's threshold |
| `ignore_battery_protection` | ❌ | Skip SoC checks for this appliance (default: `false`) |
| `soc_hysteresis_percent` | ❌ | SoC % above `min_soc` required to re-enable after block (default: `5`) |
| `is_battery` | ❌ | Flag this appliance as a battery (default: `false`). A battery supplies the SoC sensor and threshold that protect the other appliances, and is itself never blocked by SoC protection |
| `battery_soc_sensor` | ❌ | (Battery only) State-of-charge sensor (%) for this battery |
| `min_soc` | ❌ | (Battery only) Other appliances are blocked while this battery's SoC is below this value (default: `20`) |
| `battery_protection_mode` | ❌ | (Battery only) `strict` or `relaxed` (default: `strict`) |

---

## Runtime Modes

Each appliance gets a **Select** entity to change its mode from the HA UI. Modes are saved persistently.

| Mode | Behaviour |
|---|---|
| `partial_solar` | Runs only when averaged excess solar ≥ `partial_min_excess` W *(default)* |
| `full_solar` | Solar-only — runs **only** when the remaining solar excess fully covers the appliance (`remaining ≥ min_amps`); never imports from the grid. No partial-coverage or off-peak fallback |
| `forced` | Runs whenever it can reach `min_amps`. Solar surplus and grid peak-headroom are **additive** — solar is used *on top of* grid power, scaling the setpoint up to `max_amps` (the grid-imported part stays bounded by the High/peak limit) |
| `off_peak` | Runs freely during off-peak hours; during peak hours uses a breakeven calculation against export/peak tariff rates |

---

## Entities Created

For each configured appliance EMS creates:

| Entity | Type | Description |
|---|---|---|
| `switch.<name>` | Switch | Reflects active state; manual calls pass through to the underlying switch |
| `select.<name>_mode` | Select | Runtime mode selector (`partial_solar` / `forced` / `off_peak`) |

A single EMS-wide status sensor is also created:

| Entity | Type | Description |
|---|---|---|
| `sensor.solar_appliance_manager_status` | Sensor | Number of currently active appliances; attributes include `excess_amps`, `soc`, `active_appliances`, `demand_high`, `demand_critical` |

When the quarterly-demand sources are configured, these EMS-wide entities are also created (all kW):

| Entity | Type | Description |
|---|---|---|
| `Quarterly Demand Estimate` | Sensor | Projected quarter-hour demand if current power continues |
| `Quarterly Demand Month Max` | Sensor | Monthly peak (≥ `minimal_peak_warning`) |
| `Quarterly Demand High Limit` | Sensor | `month_max − peak_high_safety` |
| `Quarterly Demand Critical Limit` | Sensor | `month_max − peak_critical_safety` |
| `Max Power To Peak Limit` | Sensor | Additional power that can be drawn now without exceeding the High limit (cap 40 kW) |
| `Quarterly Demand High` | Binary | Estimate ≥ High limit (after 6 min, 0.2 kW hysteresis) |
| `Quarterly Demand Critical` | Binary | Estimate ≥ Critical limit (after 8 min, 0.2 kW hysteresis) |

---

## Update Interval

The coordinator polls every **30 seconds**. Hysteresis and averaging windows are expressed in seconds and are sampled at this rate. The quarterly-demand entities refresh on a separate **~10 second** cycle.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Appliance never turns on | Verify `solar_production_sensor` is available and returning a numeric value |
| Appliance turns on/off rapidly | Increase `hysteresis_seconds` |
| Appliance blocked despite sufficient solar | Check `battery_soc_sensor` — SoC may be below `min_soc` |
| All appliances forced off | Quarter-hour demand reached **Critical** — check `Quarterly Demand Critical` and your `peak_critical_safety` margin |
| Appliance amps keep dropping | Quarter-hour demand near the **High** limit — the optimizer is throttling to avoid the peak (`Max Power To Peak Limit`) |
| Options dialog doesn't open | Ensure HA version ≥ 2024.6.0 |
