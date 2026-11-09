# Teslemetry and Tessie through Home Assistant

Pit Crew's `homeassistant` connection works with a car that Home Assistant (HA) knows through the **`teslemetry`** or
**`tessie`** integrations, using their own bundled mappings (`teslemetry_ha`, `tessie_ha`). This page says exactly what that
means, how it was checked, and what it cannot do.

> **How it was checked:** against Home Assistant core's source (`origin/dev` at `4368abee2a92`, 2026-11-09): `strings.json`
> entity names, `entity_registry_enabled_default`, the value functions in `sensor.py`/`binary_sensor.py`, and the platform
> service methods. The checkout has no release tags, so no release tag could be compared; only `dev` was read. **Neither profile
> has been run on a real car or a real HA.** The tests use synthetic states built from those source shapes
> (`tests/fixtures/integrations/{teslemetry,tessie}/`). Both profiles say `tested: false`.

(This is separate from Pit Crew's *direct* `teslemetry` / `tessie` connections, which call the Teslemetry / Tessie web APIs
with your token and need no Home Assistant. Pick one route per car.)

## Verdict

| Integration | Verdict |
|---|---|
| `tesla_fleet` | Works. Tested on a real Model Y (profile `tesla_fleet`). |
| `teslemetry` | **Needs its own profile (`teslemetry_ha`); it is not the `tesla_fleet` profile as-is.** The windows cover is named differently (`cover.<slug>_vent_windows`, so vent/close windows would fail on `tesla_fleet`), and Teslemetry ships battery range and the rear seat heaters disabled. Everything else shares the names. Source-verified, not run on a real car. |
| `tessie` | **Needs its own profile (`tessie_ha`).** Defrost is `switch.<slug>_defrost_mode`, the windows cover is `cover.<slug>_vent_windows`, the front seat heaters are `select.<slug>_seat_heater_left/right`. Source-verified, not run on a real car. |

Both can be supported for reading status and the safe commands. **Parked/moving cannot be proven from HA's gear and speed
entities for either one** (see below), so unlock, trunk, frunk and window-vent stay refused until the car is plugged in or a
TeslaMate state entity says it is parked. That is deliberate (fail closed), not a bug.

## What differs from `tesla_fleet`

Entity ids are `<domain>.<slug>_<name slug>` where the name comes from `strings.json` (Teslemetry and Tessie set
`translation_key = <data key>`; tesla_fleet does the same).

* **Windows cover**: `Windows` (tesla_fleet) vs `Vent windows` (teslemetry, tessie) → `cover.<slug>_vent_windows`.
* **Tessie defrost**: `Defrost mode` → `switch.<slug>_defrost_mode`. (tesla_fleet and teslemetry: `Defrost`.)
* **Tessie seat heaters**: `Seat heater left` / `right` (no "front"). Rear ones are `Seat heater rear left/center/right` in all three.
* **Disabled by default** (`entity_registry_enabled_default=False`): in `tesla_fleet` and `teslemetry`, *Shift state*, *Speed*,
  *Odometer*, the four *Tire pressure* sensors and the four *Tire pressure warning* binary sensors. `teslemetry` also ships
  *Battery range* disabled. **`tessie` ships all of these enabled.** The rear seat heaters are disabled in `tesla_fleet` and
  `teslemetry`. Pit Crew tells you which to enable (`/car discover`, `/car`, `car_status`).
* **Streaming vs polling (Teslemetry only).** With Teslemetry telemetry streaming, most sensors are `RestoreSensor` /
  `RestoreEntity`: after an HA restart they show their *last* value with a fresh timestamp until the car next sends that field.
  Polled sensors are disabled when the vehicle is stream-only. `tesla_fleet` and `tessie` are pure polling (Tessie every 10 s,
  but of Tessie's cache; `api.state(use_cache=True)`).
* **Data timestamps.** None of the three integrations exposes the time the *car* last reported. HA's `last_reported` is the
  time the integration wrote the value, so for Tessie (cache) and Teslemetry (restore) a fresh timestamp does not prove fresh
  data. There is no `data_time_entity` to use (compare Kia's *Last Updated At*).

## Safety: motion fails closed

The gear sensor turns a missing value into `p` (`SHIFT_STATES.get(str(x), "p")` in teslemetry, `x.lower() if isinstance(x, str) else "p"`
in tessie) and Teslemetry's speed turns a missing value into `0` (`value or 0`). Together with cached/restored values, a
"P" or a speed of 0 is not evidence that the car is parked. The new profile key `untrusted_values` makes those readings
*unavailable*:

* `shift_state`: `P` is ignored; `D`, `R`, `N` still count as moving.
* `speed_mph`: `0` is ignored; anything above 1 mph still counts as moving, even if stale.
* Parked is proven by `plugged_in` (the cable entity) or by a TeslaMate state entity
  (`ha_entity_overrides: {"moving_state_entity": "sensor.teslamate_<car>_state"}`), each of which must be fresh.

The `tesla_fleet` profile is **not** changed: it was run on a real car and its integration polls the Fleet API directly. (The
same `"p"`/`0` defaulting exists in its source; see docs/DECISIONS.md.)

## Setup

1. Install and set up the integration in HA (Teslemetry or Tessie).
2. Enable the disabled entities (Shift state and Speed for motion only if you also use a TeslaMate state; Odometer; the four tire
   pressures and warnings; Battery range for Teslemetry): Settings → Devices & services → *Teslemetry* / *Tessie* → your car →
   entities → show disabled entities → Enabled.
3. In Pit Crew: `/car discover`. It looks at the entity names and recommends `teslemetry_ha` or `tessie_ha` (it uses a few
   entities only one integration creates: `cover.*_vent_windows`, `switch.*_defrost_mode`, `select.*_seat_heater_left`). Set
   `ha_profile` and `ha_slug` as shown.
4. Stay in practice mode until `/car` matches the car's app.

## Mapping

Fields (`⚠ off` = ships disabled in Home Assistant; shift state and speed are `optional`):

| Pit Crew field | `tesla_fleet` | `teslemetry` | `tessie` |
|---|---|---|---|
| `battery_pct` | `sensor.<slug>_battery_level` | `sensor.<slug>_battery_level` | `sensor.<slug>_battery_level` |
| `range_mi` | `sensor.<slug>_battery_range` | `sensor.<slug>_battery_range` ⚠ off | `sensor.<slug>_battery_range` |
| `charging_state` | `sensor.<slug>_charging` | `sensor.<slug>_charging` | `sensor.<slug>_charging` |
| `plugged_in` | `binary_sensor.<slug>_charge_cable` | `binary_sensor.<slug>_charge_cable` | `binary_sensor.<slug>_charge_cable` |
| `charge_power_kw` | `sensor.<slug>_charger_power` | `sensor.<slug>_charger_power` | `sensor.<slug>_charger_power` |
| `charge_limit_pct` | `number.<slug>_charge_limit` | `number.<slug>_charge_limit` | `number.<slug>_charge_limit` |
| `charge_eta_min` | `sensor.<slug>_time_to_full_charge` | `sensor.<slug>_time_to_full_charge` | `sensor.<slug>_time_to_full_charge` |
| `charge_current_a` | `sensor.<slug>_charger_current` | `sensor.<slug>_charger_current` | `sensor.<slug>_charger_current` |
| `charge_current_max_a` | `number.<slug>_charge_current` | `number.<slug>_charge_current` | `number.<slug>_charge_current` |
| `charger_voltage_v` | `sensor.<slug>_charger_voltage` | `sensor.<slug>_charger_voltage` | `sensor.<slug>_charger_voltage` |
| `charge_port_open` | `cover.<slug>_charge_port_door` | `cover.<slug>_charge_port_door` | `cover.<slug>_charge_port_door` |
| `locked` | `lock.<slug>_lock` | `lock.<slug>_lock` | `lock.<slug>_lock` |
| `doors_open` | 4× `binary_sensor.<slug>_{front,rear}_{driver,passenger}_door` | 4× `binary_sensor.<slug>_{front,rear}_{driver,passenger}_door` | 4× `binary_sensor.<slug>_{front,rear}_{driver,passenger}_door` |
| `windows_open` | 4× `binary_sensor.<slug>_{front,rear}_{driver,passenger}_window` | 4× `binary_sensor.<slug>_{front,rear}_{driver,passenger}_window` | 4× `binary_sensor.<slug>_{front,rear}_{driver,passenger}_window` |
| `trunk_open` | `cover.<slug>_trunk` | `cover.<slug>_trunk` | `cover.<slug>_trunk` |
| `frunk_open` | `cover.<slug>_frunk` | `cover.<slug>_frunk` | `cover.<slug>_frunk` |
| `sentry_on` | `switch.<slug>_sentry_mode` | `switch.<slug>_sentry_mode` | `switch.<slug>_sentry_mode` |
| `user_present` | `binary_sensor.<slug>_user_present` | `binary_sensor.<slug>_user_present` | `binary_sensor.<slug>_user_present` |
| `climate_on` | `climate.<slug>_climate` | `climate.<slug>_climate` | `climate.<slug>_climate` |
| `cabin_temp_f` | `sensor.<slug>_inside_temperature` | `sensor.<slug>_inside_temperature` | `sensor.<slug>_inside_temperature` |
| `outside_temp_f` | `sensor.<slug>_outside_temperature` | `sensor.<slug>_outside_temperature` | `sensor.<slug>_outside_temperature` |
| `target_temp_f` | `climate.<slug>_climate` | `climate.<slug>_climate` | `climate.<slug>_climate` |
| `defrost_on` | `switch.<slug>_defrost` | `switch.<slug>_defrost` | `switch.<slug>_defrost_mode` |
| `location_zone` | `device_tracker.<slug>_location` | `device_tracker.<slug>_location` | `device_tracker.<slug>_location` |
| `shift_state` | `sensor.<slug>_shift_state` ⚠ off | `sensor.<slug>_shift_state` ⚠ off | `sensor.<slug>_shift_state` |
| `speed_mph` | `sensor.<slug>_speed` ⚠ off | `sensor.<slug>_speed` ⚠ off | `sensor.<slug>_speed` |
| `asleep` | `binary_sensor.<slug>_status` | `binary_sensor.<slug>_status` | `binary_sensor.<slug>_status` |
| `software_update_available` | `update.<slug>_update` | `update.<slug>_update` | `update.<slug>_update` |
| `software_version` | `update.<slug>_update` | `update.<slug>_update` | `update.<slug>_update` |
| `odometer_mi` | `sensor.<slug>_odometer` ⚠ off | `sensor.<slug>_odometer` ⚠ off | `sensor.<slug>_odometer` |
| `tire_psi_fl` | `sensor.<slug>_tire_pressure_front_left` ⚠ off | `sensor.<slug>_tire_pressure_front_left` ⚠ off | `sensor.<slug>_tire_pressure_front_left` |
| `tire_psi_fr` | `sensor.<slug>_tire_pressure_front_right` ⚠ off | `sensor.<slug>_tire_pressure_front_right` ⚠ off | `sensor.<slug>_tire_pressure_front_right` |
| `tire_psi_rl` | `sensor.<slug>_tire_pressure_rear_left` ⚠ off | `sensor.<slug>_tire_pressure_rear_left` ⚠ off | `sensor.<slug>_tire_pressure_rear_left` |
| `tire_psi_rr` | `sensor.<slug>_tire_pressure_rear_right` ⚠ off | `sensor.<slug>_tire_pressure_rear_right` ⚠ off | `sensor.<slug>_tire_pressure_rear_right` |
| `tire_warn_fl` | `binary_sensor.<slug>_tire_pressure_warning_front_left` ⚠ off | `binary_sensor.<slug>_tire_pressure_warning_front_left` ⚠ off | `binary_sensor.<slug>_tire_pressure_warning_front_left` |
| `tire_warn_fr` | `binary_sensor.<slug>_tire_pressure_warning_front_right` ⚠ off | `binary_sensor.<slug>_tire_pressure_warning_front_right` ⚠ off | `binary_sensor.<slug>_tire_pressure_warning_front_right` |
| `tire_warn_rl` | `binary_sensor.<slug>_tire_pressure_warning_rear_left` ⚠ off | `binary_sensor.<slug>_tire_pressure_warning_rear_left` ⚠ off | `binary_sensor.<slug>_tire_pressure_warning_rear_left` |
| `tire_warn_rr` | `binary_sensor.<slug>_tire_pressure_warning_rear_right` ⚠ off | `binary_sensor.<slug>_tire_pressure_warning_rear_right` ⚠ off | `binary_sensor.<slug>_tire_pressure_warning_rear_right` |

| Command | `tesla_fleet` | `teslemetry` | `tessie` |
|---|---|---|---|
| `climate_on` | `climate.set_hvac_mode` → `climate.<slug>_climate`<br>`climate.set_temperature` → `climate.<slug>_climate` | `climate.set_hvac_mode` → `climate.<slug>_climate`<br>`climate.set_temperature` → `climate.<slug>_climate` | `climate.set_hvac_mode` → `climate.<slug>_climate`<br>`climate.set_temperature` → `climate.<slug>_climate` |
| `climate_off` | `climate.set_hvac_mode` → `climate.<slug>_climate` | `climate.set_hvac_mode` → `climate.<slug>_climate` | `climate.set_hvac_mode` → `climate.<slug>_climate` |
| `set_temp` | `climate.set_temperature` → `climate.<slug>_climate` | `climate.set_temperature` → `climate.<slug>_climate` | `climate.set_temperature` → `climate.<slug>_climate` |
| `defrost` **≠** | `switch.turn_on/switch.turn_off` → `switch.<slug>_defrost` | `switch.turn_on/switch.turn_off` → `switch.<slug>_defrost` | `switch.turn_on/switch.turn_off` → `switch.<slug>_defrost_mode` |
| `seat_heat` | `select.select_option` → seat selects | `select.select_option` → seat selects | `select.select_option` → seat selects |
| `charge_limit` | `number.set_value` → `number.<slug>_charge_limit` | `number.set_value` → `number.<slug>_charge_limit` | `number.set_value` → `number.<slug>_charge_limit` |
| `charge_start` | `switch.turn_on` → `switch.<slug>_charge` | `switch.turn_on` → `switch.<slug>_charge` | `switch.turn_on` → `switch.<slug>_charge` |
| `charge_stop` | `switch.turn_off` → `switch.<slug>_charge` | `switch.turn_off` → `switch.<slug>_charge` | `switch.turn_off` → `switch.<slug>_charge` |
| `charge_amps` | `number.set_value` → `number.<slug>_charge_current` | `number.set_value` → `number.<slug>_charge_current` | `number.set_value` → `number.<slug>_charge_current` |
| `lock` | `lock.lock` → `lock.<slug>_lock` | `lock.lock` → `lock.<slug>_lock` | `lock.lock` → `lock.<slug>_lock` |
| `unlock` | `lock.unlock` → `lock.<slug>_lock` | `lock.unlock` → `lock.<slug>_lock` | `lock.unlock` → `lock.<slug>_lock` |
| `open_trunk` | `cover.open_cover` → `cover.<slug>_trunk` | `cover.open_cover` → `cover.<slug>_trunk` | `cover.open_cover` → `cover.<slug>_trunk` |
| `open_frunk` | `cover.open_cover` → `cover.<slug>_frunk` | `cover.open_cover` → `cover.<slug>_frunk` | `cover.open_cover` → `cover.<slug>_frunk` |
| `vent_windows` **≠** | `cover.open_cover` → `cover.<slug>_windows` | `cover.open_cover` → `cover.<slug>_vent_windows` | `cover.open_cover` → `cover.<slug>_vent_windows` |
| `close_windows` **≠** | `cover.close_cover` → `cover.<slug>_windows` | `cover.close_cover` → `cover.<slug>_vent_windows` | `cover.close_cover` → `cover.<slug>_vent_windows` |
| `sentry_on` | `switch.turn_on` → `switch.<slug>_sentry_mode` | `switch.turn_on` → `switch.<slug>_sentry_mode` | `switch.turn_on` → `switch.<slug>_sentry_mode` |
| `sentry_off` | `switch.turn_off` → `switch.<slug>_sentry_mode` | `switch.turn_off` → `switch.<slug>_sentry_mode` | `switch.turn_off` → `switch.<slug>_sentry_mode` |
| `honk` | `button.press` → `button.<slug>_honk_horn` | `button.press` → `button.<slug>_honk_horn` | `button.press` → `button.<slug>_honk_horn` |
| `flash` | `button.press` → `button.<slug>_flash_lights` | `button.press` → `button.<slug>_flash_lights` | `button.press` → `button.<slug>_flash_lights` |


All other commands are identical in the three integrations. Services are plain core services (`lock.lock`, `cover.open_cover`,
`switch.turn_on`, `button.press`, `number.set_value`, `select.select_option`, `climate.set_hvac_mode`,
`climate.set_temperature`). Teslemetry additionally needs the right Teslemetry scope on the token (missing scope raises
`missing_scope` in HA); the frunk cover only implements `open_cover` in `teslemetry` and `tessie`.

## What was not determined

* Anything about real-car behaviour: values, timing, how HA renders units on your locale, or what Teslemetry does for your
  firmware (`streaming_firmware` thresholds decide stream vs poll per sensor).
* Whether a given Teslemetry subscription polls or streams (`vehicle.poll`); the profile works with either, but freshness
  rules differ.
* Release-tag differences: the HA checkout has no tags.
