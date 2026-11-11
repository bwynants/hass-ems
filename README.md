# Energy Management System

A Home Assistant custom integration (**EMS**) that automatically manages household appliances based on solar production, grid export, and time-of-use tariffs.

---

## Features

- **Priority-based scheduling** — appliances run in priority order when solar surplus is available.
- **Per-appliance runtime modes** — `partial_solar`, `forced`, `full_solar`, `off_peak`, or `unmanaged`, selectable from the HA UI and persisted across restarts.
- **Hysteresis & debounce** — configurable on/off delay and minimum switch interval prevent rapid cycling, plus a fixed 0.5 A start margin: an appliance switches on only once `min_amps` + 0.5 A is available and stays on down to `min_amps` (independent of the setpoint step).
- **Averaging window** — excess power is averaged over a rolling window to smooth out transient spikes.
- **Not-consuming alarm** — a per-appliance problem sensor that trips when EMS has switched an appliance on but it draws nothing for 5 minutes, excluding a car that is nearly full.
- **EV force-charge below SoC** — give an appliance the car's state-of-charge sensor and it becomes an EV charger: while the car is plugged in and below a per-appliance threshold (a `number` entity, default 0 = off) it charges from the grid as in `forced` mode, then reverts to its own mode. Peak protection still applies.
- **Capacity-tariff (peak) protection** — appliance amps are progressively lowered (or the appliance turned off) against the lower of the instantaneous High limit and the **projected ceiling for the rest of the quarter** (High limit minus a configurable margin), so the monthly peak is never reached and a warning is rarely needed; an appliance shed for peak stays off until the next quarter, setpoints ramp up in bounded steps and never straight after a grid-driven cut; a Critical backstop forces everything off if needed.
- **Off-peak awareness** — appliances in `off_peak` mode run freely during off-peak hours; a breakeven calculation gates them during peak hours.
- **Connection sensor** — optional per-appliance binary sensor to skip appliances that are physically disconnected.

---

## Requirements

- Home Assistant 2026.8.0 or later. 2026.8 reworked the device registry so a device belongs to exactly one config entry and at most one subentry; EMS uses the new `via_device_id` link, which does not exist on earlier cores.
- A sensor entity providing **solar production** in Watts (required).
- Optional: house load sensor, net grid power sensor (import positive / export negative), peak-tariff sensor (`on` during peak hours).

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
| `peak_shed_safety` | ❌ | kW below the **High** limit the optimizer aims for over the rest of the quarter (default: `0.3`) — see *Proactive peak control* below |
| `cost_peak` | ❌ | Sensor/number entity with the import price during peak hours, €/kWh (unset: `0.40`) |
| `cost_offpeak` | ❌ | Sensor/number entity with the import price during off-peak hours, €/kWh (unset: `0.20`) |
| `gain_peak` | ❌ | Sensor/number entity with the compensation per exported kWh during peak hours, €/kWh (unset: `0.10`). Read every cycle (dynamic tariffs work); with the two import prices it drives the `off_peak`-mode breakeven decision during peak hours |
| `gain_offpeak` | ❌ | Sensor/number entity with the compensation per exported kWh during off-peak hours, €/kWh (unset: `0.10`). Point it at the same entity as `gain_peak` when your contract pays a single injection price. (A pre-0.5 entry's single `gain_export` keeps backing both until the options form is saved once) |
| `debug_log` | ❌ | When enabled, writes the full per-cycle decision trace to `ems_debug.log` in the config dir (2 MB × 3 rotation). **Off by default** — opt-in, since it forces DEBUG logging and disk I/O |

> **Quarterly demand (capacity tariff).** When the three demand source sensors are set, EMS exposes native entities for the Belgian *capaciteitstarief*: `Quarterly Demand Estimate`, `Quarterly Demand Month Max`, `Quarterly Demand High Limit`, `Quarterly Demand Critical Limit`, `Max Power To Peak Limit`, and binary sensors `Quarterly Demand High` / `Quarterly Demand Critical`. They refresh every ~10 s. Peak-aware control is **always on** (it simply has no effect until the demand sensors are configured). The optimizer scales each appliance's `fixed_amps` setpoint against a **grid ceiling**: the lower of the **High limit** (instantaneous) and the **projected ceiling** for the rest of the quarter (see *Proactive peak control* below). The import the house is already drawing counts against that ceiling in **every** mode: a charger in `off_peak`/`partial_solar`/`full_solar` is blocked (or, once running, throttled to `min_amps` and then shed) when base-load import plus its minimum would exceed the High limit, exactly as a `forced` one is. (Base load is accounted via the signed surplus budget, so the appliance's own draw isn't double-counted.) It **progressively lowers** the setpoint (and turns the appliance off when even the minimum won't fit) so demand stays under the High limit, and force-stops all managed appliances as a backstop if demand still reaches **Critical**. The switch-off of a *running* appliance is debounced: the budget has to stay below its `min_amps` for `PEAK_GATE_SHED_CYCLES` consecutive cycles (3 ≈ 30 s) — meanwhile it is throttled to `min_amps`. An appliance's own draw is added back into the budget from its power sensor, which polls slower than the grid meter, so the first cycle after a charger starts sees the full import but only part of the add-back; without the debounce that one stale cycle shed the charger 10 s after every start. Starting is never debounced. `Max Power To Peak Limit` remains a monitoring sensor. Threshold values are entered as plain numbers (no `input_number` helper needed).
>
> **Proactive peak control (0.7).** The High limit alone acts too late: the High *warning* fires on the projected quarter average, which reacts to one sample of the instantaneous draw, while the shed waits three cycles and only looks at the instantaneous ceiling — so a base-load step while a charger runs was announced before EMS had moved. Now the demand coordinator also projects the quarter: the average draw that would end it at `High limit − peak_shed_safety` (from the quarter's energy so far and the seconds remaining) is a second ceiling, skipped for the first minute of a quarter (the current-average source lags the reset), and the optimizer scales the setpoints against the lower of the two. The High limit is constant, so the projection can only ever tighten it, never balloon it late in a quiet quarter. The setpoint therefore **steps down progressively** as a quarter tightens; the throttle-to-min / shed sequence is the last resort, when even `min_amps` no longer fits. The two ceilings each carry one safety — the 1 A forced-mode reserve on the instantaneous one, `peak_shed_safety` on the projected one — and the effective ceiling is the lower of the two, so the budget never jumps when the binding ceiling changes hands. Three anti-flap rules go with it: **(1)** an appliance shed for peak (by this gate or the Critical backstop) is **locked out until the next quarter** — a restart would meet the same tight projection and be shed again; the lockout is persisted, so a reload mid-quarter honours it; **(2)** upward setpoint writes are capped at `FIXED_AMPS_MAX_STEP_UP` (2 A) per write, ramp-down is never limited; **(3)** after a **grid-driven cut** (a downward write while the draw is not fully solar-funded) no upward write happens for `PEAK_CUT_HOLD_SECONDS` (60 s) — the load that pushed the setpoint down is usually still there. The status sensor lists the appliances the peak logic acted on this cycle in `peak_limited_appliances`.

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
| `fixed_amps` ("Ampere to use") | ✅ | The number/input_number entity the optimizer dynamically sets based on available excess, scaled between the entity's own **min** and **max**. Those bounds become the appliance's min/max amps — so there are no separate min/max fields (e.g., `input_number.my_amps` with min 6, max 16). Its **step** is used too: the setpoint is quantised to the entity's own step (0.1 A entity → 0.1 A setpoints), falling back to `FIXED_AMPS_STEP` (0.5 A) only when the entity publishes none |
| `fixed_amps_hysteresis_seconds` | ❌ | Minimum seconds the `fixed_amps` setpoint is held before it may change to another value, preventing it from switching too often (default: `120`) |
| `power_sensor` | ❌ | Sensor (W) reporting the appliance's real power draw; when set, the optimizer deducts measured consumption from the budget instead of estimating. Falls back to `fixed_amps` setpoint, then `min_amps` |
| `partial_min_excess_amps` | ❌ | Minimum averaged excess **amps** required in `partial_solar` mode (default: `4.0`). Compared directly against the averaged surplus current; `0` disables the threshold |
| `connected_sensor` | ❌ | Binary sensor — `off` means appliance is disconnected and will be skipped |
| `hysteresis_seconds` | ❌ | Seconds to hold state before allowing next change (default: `120`) |
| `averaging_window_seconds` | ❌ | Rolling window in seconds for excess power averaging (default: `180`) |
| `ev_soc_sensor` | ❌ | State-of-charge sensor (%) of the car this charger serves. Setting it makes the appliance an **EV charger**: while `connected_sensor` is on and the SoC is below the appliance's *Force charge below* number entity, it is driven as `forced` (grid + solar, still under the peak limit) and reverts to its selected mode at the threshold. No hysteresis — SoC only rises while plugged in. An `unknown`/`unavailable` SoC never forces; `unmanaged` is never overridden. With two cars on one charger, point this at a template sensor that yields the plugged-in car's SoC (and `unknown` when none is) |

---

## Runtime Modes

Each appliance gets a **Select** entity to change its mode from the HA UI. Modes are saved persistently.

| Mode | Behaviour |
|---|---|
| `partial_solar` | Runs only when averaged excess solar ≥ `partial_min_excess_amps` A *(default)*. **Start/stop are asymmetric** with the same 0.5 A deadband as the `min_amps` gate (`ON_OFF_HYSTERESIS_AMPS`): to *start*, the averaged excess must clear the threshold by that margin; once *on* it holds down to the threshold itself, so an average hovering at the threshold can't flip it each time the time hysteresis expires |
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
| `number.<name>_force_charge_below` | Number (%) | *EV chargers only* (appliance has `ev_soc_sensor`). Car SoC below which a plugged-in car is force-charged; 0 = off. Persisted per appliance, applied on the next cycle |
| `binary_sensor.<name>_force_charging` | Binary sensor | *EV chargers only.* On while the SoC rule overrides the mode to `forced` this cycle — i.e. EMS *wants* to force-charge; the charger may still be throttled or held off by the quarter-hour peak limit. Off during Critical shed or with auto-switching off; unavailable during startup warm-up |
| `binary_sensor.<name>_not_consuming` | Binary sensor (problem) | *Appliances with a `power_sensor` only.* On when EMS has switched the appliance on but it has drawn less than 0.5 A for 5 minutes (start-up grace). Never on a power-sensor dropout (a reading held from before the dropout does not count either), never while the appliance's own switch is unavailable (a charger that drops off the network is a link problem, not a charging failure — see `Modbus down`-style sensors for that), and not while the car's SoC is ≥ 95 % — tapering off near full is normal. Off when EMS isn't commanding; unavailable during warm-up |

> **Upgrading from 0.2.x.** The mode selects move off the EMS device onto their appliance device. Entity ids, names, and history are unchanged, but dashboard cards or automations that target the EMS *device* rather than the entity will need repointing.

These EMS-wide entities are always created on the EMS device:

| Entity | Type | Description |
|---|---|---|
| `sensor.energy_management_system_status` | Sensor | Number of currently active appliances; attributes include `excess_amps`, `active_appliances`, `forced_appliances` (force-charged by the car-SoC rule), `not_consuming_appliances` (switched on but not drawing), `auto_switching`, `demand_high`, `demand_critical` |
| `Managed Consumption` | Sensor (A) | Amps the managed appliances are currently drawing |
| `Managed Consumption Power` | Sensor (kW) | The same managed draw, in kilowatts |
| `Spare Amps` | Sensor (A) | Available surplus budget, **clamped to ≥ 0** (reads 0 while importing). The optimizer's internal `excess_amps` stays signed — this is the user-facing display only |
| `Spare Power` | Sensor (kW) | The same surplus budget, in kilowatts (also ≥ 0) |
| `Auto switching` | Switch | Master enable for automatic appliance control |

When the quarterly-demand sources are configured, these EMS-wide entities are also created (all kW):

| Entity | Type | Description |
|---|---|---|
| `Quarterly Demand Estimate` | Sensor | Projected quarter-hour demand if current power continues |
| `Quarterly Demand Month Max` | Sensor | Monthly peak (≥ `minimal_peak_warning`) |
| `Quarterly Demand High Limit` | Sensor | `month_max − peak_high_safety` |
| `Quarterly Demand Critical Limit` | Sensor | `month_max − peak_critical_safety` |
| `Max Power To Peak Limit` | Sensor | Additional power that can be drawn now without exceeding the High limit *by the end of the current quarter* (cap 40 kW). Monitoring only — the optimizer scales against the lower of the High limit and the projected ceiling (which aims `peak_shed_safety` lower and is skipped at the quarter boundary) |
| `Quarterly Demand Shed Limit` | Sensor | `High limit − peak_shed_safety`: what the projected ceiling aims for |
| `Projected Ceiling` | Sensor | Average grid draw from now to the quarter's end that ends the quarter at the shed limit — the optimizer's second grid ceiling (the lower of this and the High limit binds). Unknown during the first minute of a quarter, when the projection is skipped |
| `Quarterly Demand High` | Binary | Estimate ≥ High limit (after 6 min, 0.2 kW hysteresis) |
| `Quarterly Demand Critical` | Binary | Estimate ≥ Critical limit (after 8 min, 0.2 kW hysteresis) |
| `Quarterly Demand Alert` | Event | Once-per-quarter peak announcements: `high` (first time High latches while Critical is off), `critical` (first time Critical latches; a later High stays silent), and after the quarter has ended, ~1 min into the next one when the month-max source is definitive, either `new_month_peak` (the month maximum rose, with `previous_peak`/`new_peak` attributes) or `cleared` (a warning went out but no record was set). Never more than one High, one Critical and one closing message per quarter hour; a High that drops and comes back does not repeat. The binary sensors latch from minute 6 (High) / 8 (Critical); the **announcement** waits for minute 8 / 10, needs the flag sustained for 3 / 2 consecutive cycles (a 15 s burst is not a peak risk), and is **held while the controller is throttling or shedding for peak** (for `PEAK_ACTION_HOLD_SECONDS` after its last action) — a risk EMS is already removing is only announced if it is still there once the action has shown on the meter. Every event carries `month_max` and `estimate`. Trigger on it with `event.received` and filter on `event_type`; the per-quarter flags are persisted, so an EMS reload mid-quarter does not re-announce |

---

## Update Interval

The coordinator polls every **10 seconds** (`CONTROLLER_UPDATE_INTERVAL`). Hysteresis and averaging windows are expressed in seconds and are sampled at this rate. The quarterly-demand entities refresh on a separate **~10 second** cycle.

---

## Restart Behaviour

A restart or reload must not disturb a running appliance, so the controller does two things before it decides anything.

**It waits for its sources.** Home Assistant sets this integration up before the inverter, meter and power integrations have populated their entities, and a missing reading is indistinguishable from "no surplus". Until every configured source entity reports — grid, solar, house load, voltage, tariff, and each managed appliance's switch, power sensor, setpoint and connected sensor — the controller observes without commanding, and without feeding those blank readings into the averaging history. `Spare Amps` reads unknown during this window and the status sensor's `warming_up` attribute is `true`, listing what it is waiting on. The wait is capped at **180 seconds** (`STARTUP_GRACE_SECONDS`): past that it starts anyway and logs a warning naming whatever is still unavailable, so a deleted sensor can't stall control permanently. Critical quarter-hour demand overrides the wait, since shedding works off the appliances' own on/off state rather than the budget.

**It adopts what the appliances are already doing.** Each appliance's on/off state, current setpoint and hysteresis timers are restored from `.storage` and reconciled against the live entities (the physical state wins — an appliance may have been switched by hand while HA was down). A short restart therefore resumes mid-hysteresis-window rather than re-arming it; a long outage lets the window expire naturally. The first cycle after startup is a no-op for an appliance that should keep running, and a genuine off decision still has to outlast the hysteresis window.

Look for `control starting; adopted appliance state: …` in the log to see exactly what was adopted.

**It stays silent while HA shuts down.** Shutdown tears the source integrations down one at a time, and a template built on their entities can collapse to a valid-looking default the moment they go unavailable (a tariff sensor reading "off-peak" because its meter counters vanished). A cycle run on that picture would command an appliance milliseconds before HA dies and leave it that way for the whole restart. Once `hass.is_stopping` is set, the controller skips its cycle and ignores tariff-trigger refreshes; the entities keep their last values.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Appliance never turns on | Verify `solar_production_sensor` is available and returning a numeric value |
| Appliance turns on/off rapidly | Increase `hysteresis_seconds` |
| `Not consuming` is on | EMS switched the appliance on but its power sensor reads ~0 for 5+ minutes: charger fault, cable, or the car itself refusing (e.g. a charge limit set *in the car* below 95 % — the SoC exemption only covers ≥ 95 %). Check the appliance's own status before suspecting EMS |
| Car plugged in below the threshold but not charging | Is `Force charging` `on`? If **off**: the SoC sensor is `unknown`, the threshold is 0, the connected sensor is off, or the mode is `unmanaged`. If **on** but the charger is off/throttled: the quarter-hour **High** limit is blocking it (`Quarterly Demand High Limit`, `Max Power To Peak Limit`). Note forced/EV charging only scales up on grid power when the quarterly-demand sensors are configured; without them it imports at `min_amps` only |
| All appliances forced off | Quarter-hour demand reached **Critical** — check `Quarterly Demand Critical` and your `peak_critical_safety` margin |
| Appliance amps keep dropping | Quarter-hour demand near the **High** limit — the optimizer is throttling to avoid the peak (`Max Power To Peak Limit`); with the projected ceiling it starts doing so `peak_shed_safety` kW earlier. Look for `setpoint cut` in the log |
| Appliance stays off for up to 15 minutes after a peak shed | Expected — an appliance shed for the quarter-hour peak is locked out until the next quarter (`shed for the quarter-hour peak ... locked out` in the log). It restarts on the next quarter's first cycle that has room for `min_amps` |
| Appliance ramps up slowly after a load switched off | Expected — upward setpoint writes are capped at 2 A per write and held for 60 s after a grid-driven cut |
| Setpoints stuck / "grid sensors stale" warning | Neither the `grid_export_sensor` nor the `demand_power_consumed_sensor` has reported in >600 s (frozen feed). The optimizer holds setpoints (no ramp-up) to avoid a runaway; fix the sensor's connection. Both directions of flow are checked, and the window is deliberately wide: meters that publish only on change (ESPHome with a delta filter, MQTT, a template chain off either) go quiet for minutes on a flat load while perfectly healthy. A feed that really dies goes `unavailable` instead, which the read fallback and the start-up gate handle |
| Nothing happens for a while after a restart | Expected — the controller is warming up. Check the status sensor's `warming_up_waiting_on` attribute for the source entities it is still waiting on |
| Appliance stops and restarts after every HA restart | Should no longer happen; if it does, enable the debug log and check for `control starting; adopted appliance state: …` — the state next to the appliance's name is what the controller adopted |
| Options dialog doesn't open | Ensure HA version ≥ 2024.6.0 |
