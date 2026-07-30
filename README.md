# Energy Management System

A Home Assistant custom integration (**EMS**) that automatically manages household appliances based on solar production, battery state of charge (SoC), grid export, and time-of-use tariffs.

---

## Features

- **Priority-based scheduling** — appliances run in priority order when solar surplus is available.
- **Per-appliance runtime modes** — `partial_solar`, `forced`, `full_solar`, `off_peak`, or `unmanaged`, selectable from the HA UI and persisted across restarts.
- **Hysteresis & debounce** — configurable on/off delay and minimum switch interval prevent rapid cycling.
- **Averaging window** — excess power is averaged over a rolling window to smooth out transient spikes.
- **Battery SoC protection** — appliances are blocked when battery drops below a configurable minimum, with hysteresis to avoid flapping.
- **Capacity-tariff (peak) protection** — appliance amps are progressively lowered (or the appliance turned off) using the quarter-hour demand prediction so the monthly peak is never reached; a Critical backstop forces everything off if needed.
- **Off-peak awareness** — appliances in `off_peak` mode run freely during off-peak hours; a breakeven calculation gates them during peak hours.
- **Connection sensor** — optional per-appliance binary sensor to skip appliances that are physically disconnected.

---

## Requirements

- Home Assistant 2026.8.0 or later. 2026.8 reworked the device registry so a device belongs to exactly one config entry and at most one subentry; EMS uses the new `via_device_id` link, which does not exist on earlier cores.
- A sensor entity providing **solar production** in Watts (required).
- Optional: house load sensor, net grid power sensor (import positive / export negative), battery SoC sensor, peak-tariff sensor (`on` during peak hours).

---

## Installation

1. Copy the `energy_management_system` folder into your `custom_components` directory (the folder name must match the integration domain):
   ```
   config/custom_components/energy_management_system/
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
| `peak_tariff_sensor` | ❌ | Binary sensor — `on` during **peak**-tariff hours (off ⇒ off-peak). When unset or unavailable, EMS assumes peak (the conservative direction) |
| `voltage` | ❌ | Grid voltage in Volts (default: `230`) |
| `demand_power_current_peak_sensor` | ❌ | Sensor with the running average power over the current 15-min quarter (kW/W). Enables the quarterly-demand feature |
| `demand_power_consumed_sensor` | ❌ | Current power-consumption sensor, used to project demand to the quarter's end |
| `demand_month_max_sensor` | ❌ | Sensor with the highest quarter-hour demand recorded this month |
| `minimal_peak_warning` | ❌ | Floor for the monthly peak, kW (default: `4`) — limits never drop below this |
| `peak_high_safety` | ❌ | kW below the monthly peak for the **High** limit (default: `0.4`) |
| `peak_critical_safety` | ❌ | kW below the monthly peak for the **Critical** limit (default: `0.2`) |
| `cost_peak` | ❌ | Sensor/number entity with the import price during peak hours, €/kWh (unset: `0.40`) |
| `cost_offpeak` | ❌ | Sensor/number entity with the import price during off-peak hours, €/kWh (unset: `0.20`) |
| `gain_export` | ❌ | Sensor/number entity with the compensation per exported kWh, €/kWh (unset: `0.10`). Read every cycle (dynamic tariffs work); the three prices drive the `off_peak`-mode breakeven decision during peak hours |

> **Battery protection** is configured per-appliance, not globally. Add an appliance and flag it as a battery (`is_battery`); it then supplies the SoC sensor, threshold (`min_soc`) and protection mode used to block the other appliances. See *Adding Appliances* below.

> **Quarterly demand (capacity tariff).** When the three demand source sensors are set, EMS exposes native entities for the Belgian *capaciteitstarief*: `Quarterly Demand Estimate`, `Quarterly Demand Month Max`, `Quarterly Demand High Limit`, `Quarterly Demand Critical Limit`, `Max Power To Peak Limit`, and binary sensors `Quarterly Demand High` / `Quarterly Demand Critical`. They refresh every ~10 s. Peak-aware control is **always on** (it simply has no effect until the demand sensors are configured). The optimizer scales each appliance's `fixed_amps` setpoint against the **High limit as a fixed grid ceiling** — keeping total grid demand under it *at every instant*, which by definition keeps every quarter's average under it too. The High limit (not `Max Power To Peak Limit`) is used deliberately: it's constant, so it can't balloon or collapse for a cycle at the quarter boundary (when the current-average source lags the reset) and spuriously shed a running appliance. (Base load is accounted via the signed surplus budget, so the appliance's own draw isn't double-counted.) It **progressively lowers** the setpoint (and turns the appliance off when even the minimum won't fit) so demand stays under the High limit, and force-stops all managed appliances as a backstop if demand still reaches **Critical**. `Max Power To Peak Limit` remains a monitoring sensor. Threshold values are entered as plain numbers (no `input_number` helper needed).

Excess power (amps) is calculated from the net grid power when a grid sensor is set,
otherwise from the solar − house-load estimate. The value is **signed**: exporting
gives a positive surplus, importing a negative one (it is deliberately *not*
clamped to 0 — the sign is needed so the managed add-back reconstructs the true
gross surplus):
```
excess_amps = -grid_power_W / voltage                  (grid sensor; signed)
            = (solar_W - house_load_W) / voltage       (fallback, no grid sensor)
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
| `fixed_amps` ("Ampere to use") | ✅ | The number/input_number entity the optimizer dynamically sets based on available excess, scaled between the entity's own **min** and **max**. Those bounds become the appliance's min/max amps — so there are no separate min/max fields (e.g., `input_number.my_amps` with min 6, max 16) |
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
| `full_solar` | Solar-only — never imports from the grid, no partial-coverage or off-peak fallback. **Start/stop are asymmetric:** to *start*, both the current *and* the averaged solar excess must cover the appliance (`remaining ≥ min_amps` **and** `avg_excess ≥ min_amps`), so a momentary spike won't commit it to a run it can't sustain; to *stay on*, only the averaged excess must still cover it (`avg_excess ≥ min_amps`), so a short solar dip won't drop a long-running appliance the next cycle |
| `forced` | Runs whenever it can reach `min_amps`. Solar surplus and grid peak-headroom are **additive** — solar is used *on top of* grid power, scaling the setpoint up to `max_amps` (the grid-imported part stays bounded by the High/peak limit). It reserves ~1 A (`FORCED_PEAK_HEADROOM_AMPS`) of grid headroom rather than scaling right up to the demand cap, so it runs steadily instead of being shed and re-started as the peak is repeatedly hit |
| `off_peak` | Runs freely during the **cheapest** tariff period. That is normally the off-peak window; but with dynamic tariffs, when the peak price drops **below** the off-peak price the peak window becomes the cheap one and it runs freely there too. Otherwise (peak hours, peak dearer) it uses a breakeven calculation against export/peak tariff rates. During off-peak hours **at sundown** (solar ≤ `SUNDOWN_SOLAR_W`) it scales up on cheap grid like `forced` — additive budget up to the peak limit — instead of running at `min_amps`. (Eligibility only — the quarter-hour demand cap still applies.) |
| `unmanaged` | EMS ignores the appliance entirely — it neither switches it on/off nor adjusts its amps, and its draw is treated as base load. Use this instead of a separate enable/disable switch |

---

## Entities Created

EMS registers one **EMS** device holding the integration-wide entities, plus **one device per appliance** — named after the appliance and shown as connected via the EMS device. Deleting an appliance removes its device and entities with it.

For each configured appliance EMS creates, on that appliance's own device:

| Entity | Type | Description |
|---|---|---|
| `select.<name>_mode` | Select | Runtime mode selector (`partial_solar` / `forced` / `full_solar` / `off_peak` / `unmanaged`) |

> **Upgrading from 0.2.x.** The mode selects move off the EMS device onto their appliance device. Entity ids, names, and history are unchanged, but dashboard cards or automations that target the EMS *device* rather than the entity will need repointing.

These EMS-wide entities are always created on the EMS device:

| Entity | Type | Description |
|---|---|---|
| `sensor.energy_management_system_status` | Sensor | Number of currently active appliances; attributes include `excess_amps`, `soc`, `active_appliances`, `auto_switching`, `demand_high`, `demand_critical` |
| `Managed Consumption` | Sensor (A) | Amps the managed appliances are currently drawing |
| `Managed Consumption Power` | Sensor (kW) | The same managed draw, in kilowatts |
| `Spare Amps` | Sensor (A) | Gross surplus budget (signed) the optimizer allocates from |
| `Spare Power` | Sensor (kW) | The same surplus budget, in kilowatts |
| `Auto switching` | Switch | Master enable for automatic appliance control |

When the quarterly-demand sources are configured, these EMS-wide entities are also created (all kW):

| Entity | Type | Description |
|---|---|---|
| `Quarterly Demand Estimate` | Sensor | Projected quarter-hour demand if current power continues |
| `Quarterly Demand Month Max` | Sensor | Monthly peak (≥ `minimal_peak_warning`) |
| `Quarterly Demand High Limit` | Sensor | `month_max − peak_high_safety` |
| `Quarterly Demand Critical Limit` | Sensor | `month_max − peak_critical_safety` |
| `Max Power To Peak Limit` | Sensor | Additional power that can be drawn now without exceeding the High limit *by the end of the current quarter* (cap 40 kW). Monitoring only — the optimizer scales against the fixed High limit instead, to stay boundary-stable |
| `Quarterly Demand High` | Binary | Estimate ≥ High limit (after 6 min, 0.2 kW hysteresis) |
| `Quarterly Demand Critical` | Binary | Estimate ≥ Critical limit (after 8 min, 0.2 kW hysteresis) |

---

## Update Interval

The coordinator polls every **10 seconds** (`CONTROLLER_UPDATE_INTERVAL`). Hysteresis and averaging windows are expressed in seconds and are sampled at this rate. The quarterly-demand entities refresh on a separate **~10 second** cycle.

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
