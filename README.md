# Solar Appliance Manager

A Home Assistant custom integration that automatically manages household appliances based on solar production, battery state of charge (SoC), grid export, and time-of-use tariffs.

---

## Features

- **Priority-based scheduling** — appliances run in priority order when solar surplus is available.
- **Per-appliance runtime modes** — `partial_solar`, `forced`, or `off_peak`, selectable from the HA UI and persisted across restarts.
- **Hysteresis & debounce** — configurable on/off delay and minimum switch interval prevent rapid cycling.
- **Averaging window** — excess power is averaged over a rolling window to smooth out transient spikes.
- **Battery SoC protection** — appliances are blocked when battery drops below a configurable minimum, with hysteresis to avoid flapping.
- **Capacity tariff lockout** — all appliances are forced off when a capacity tariff sensor is active.
- **Off-peak awareness** — appliances in `off_peak` mode run freely during off-peak hours; a breakeven calculation gates them during peak hours.
- **Connection sensor** — optional per-appliance binary sensor to skip appliances that are physically disconnected.

---

## Requirements

- Home Assistant 2024.6.0 or later.
- A sensor entity providing **solar production** in Watts (required).
- Optional: house load sensor, grid export sensor, battery SoC sensor, off-peak sensor, capacity tariff sensor.

---

## Installation

1. Copy the `solar_appliance_manager` folder into your `custom_components` directory:
   ```
   config/custom_components/solar_appliance_manager/
   ```
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration** and search for **Solar Appliance Manager**.

---

## Configuration

### Initial Setup

| Field | Required | Description |
|---|---|---|
| `solar_production_sensor` | ✅ | Entity ID of your solar production sensor (W) |
| `house_load_sensor` | ❌ | Entity ID of your house load sensor (W) |
| `grid_export_sensor` | ❌ | Entity ID of your grid export sensor (W) |
| `battery_soc_sensor` | ❌ | Entity ID of your battery SoC sensor (%) |
| `off_peak_sensor` | ❌ | Binary sensor — `on` during off-peak hours |
| `capacity_tariff_sensor` | ❌ | Binary sensor — `on` when capacity tariff is active |
| `voltage` | ❌ | Grid voltage in Volts (default: `230`) |
| `min_soc` | ❌ | Global minimum battery SoC % before blocking appliances (default: `20`) |
| `battery_protection_mode` | ❌ | `strict` or `relaxed` (default: `strict`) |

Excess power (amps) is calculated as:
```
excess_amps = (solar_W - house_load_W) / voltage
```

### Adding Appliances

After setup, open the integration's **Options** to add appliances one at a time.

| Field | Required | Description |
|---|---|---|
| `name` | ✅ | Display name (must be unique) |
| `entity_id` | ✅ | Switch entity to control |
| `priority` | ❌ | Lower number = higher priority (default: `100`) |
| `min_amps` | ✅ | Minimum amps needed to run (default: `6`) |
| `max_amps` | ✅ | Maximum amps the appliance draws (default: `32`) |
| `fixed_amps` | ❌ | Fixed amp draw; overrides `max_amps` for budget calculation |
| `partial_min_excess` | ❌ | Minimum excess Watts required in `partial_solar` mode (default: `1000`) |
| `connected_sensor` | ❌ | Binary sensor — `off` means appliance is disconnected and will be skipped |
| `hysteresis_seconds` | ❌ | Seconds of continuous excess required before switching on (default: `120`) |
| `min_switch_interval` | ❌ | Minimum seconds between state changes (default: `300`) |
| `averaging_window_seconds` | ❌ | Rolling window in seconds for excess power averaging (default: `180`) |
| `min_soc_override` | ❌ | Per-appliance minimum SoC %, overrides global setting |
| `ignore_battery_protection` | ❌ | Skip SoC checks for this appliance (default: `false`) |
| `soc_hysteresis_percent` | ❌ | SoC % above `min_soc` required to re-enable after block (default: `5`) |

---

## Runtime Modes

Each appliance gets a **Select** entity to change its mode from the HA UI. Modes are saved persistently.

| Mode | Behaviour |
|---|---|
| `partial_solar` | Runs only when averaged excess solar ≥ `partial_min_excess` W *(default)* |
| `forced` | Runs whenever there is any remaining solar budget (`remaining ≥ min_amps`) |
| `off_peak` | Runs freely during off-peak hours; during peak hours uses a breakeven calculation against export/peak tariff rates |

---

## Entities Created

For each configured appliance the integration creates:

| Entity | Type | Description |
|---|---|---|
| `switch.<name>` | Switch | Reflects active state; manual calls pass through to the underlying switch |
| `select.<name>_mode` | Select | Runtime mode selector (`partial_solar` / `forced` / `off_peak`) |

A single integration-wide status sensor is also created:

| Entity | Type | Description |
|---|---|---|
| `sensor.solar_appliance_manager_status` | Sensor | Number of currently active appliances; attributes include `excess_amps`, `soc`, `active_appliances`, `capacity_tariff_active` |

---

## Update Interval

The coordinator polls every **30 seconds**. Hysteresis and averaging windows are expressed in seconds and are sampled at this rate.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Appliance never turns on | Verify `solar_production_sensor` is available and returning a numeric value |
| Appliance turns on/off rapidly | Increase `hysteresis_seconds` and `min_switch_interval` |
| Appliance blocked despite sufficient solar | Check `battery_soc_sensor` — SoC may be below `min_soc` |
| All appliances forced off | Check `capacity_tariff_sensor` state |
| Options dialog doesn't open | Ensure HA version ≥ 2024.6.0 |
