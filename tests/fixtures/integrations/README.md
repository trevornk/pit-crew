# Integration fixtures for the brand profiles

Offline Home Assistant states used to test `adapters/profiles/bmw_connected_drive.json` and
`kia_hyundai_connect.json`. Nothing here was recorded from a real car and nothing is personal data.

## bmw_connected_drive/

* `ha_states_*.json` – the entity states Home Assistant core's own test suite asserts for its three fixture
  vehicles (`i4 eDrive40`, `iX xDrive50`, `i3 (+ REX)`), read from
  `tests/components/bmw_connected_drive/snapshots/*.ambr` at core commit `f055c6c7fda3` (the last commit that still
  contained the integration; it was removed in core PR #165075 on 2026-03-08). Differences from the snapshots:
  VINs/unique ids/registry data dropped; `unit_of_measurement` filled in from the entity descriptions
  (the snapshots leave it `None`): km, bar, %; the `device_tracker.<slug>` state (the snapshots contain none) is
  added following `device_tracker.py` (state = zone name); timestamps are fixed.
* `entity_catalog.json` – entity names (`strings.json`) and which entities ship disabled
  (`entity_registry_enabled_default=False` in `sensor.py`).

## kia_uvo/

* `entity_catalog.json` – entity names from `custom_components/kia_uvo/strings.json` at tag `v4.0.0`, plus the two
  hazard-light buttons that the integration disables by default (`button.py`, `enabled_fn=False`).
* `ha_states_us.json`, `ha_states_eu.json` – hand-built states for a plug-in EV in US units (mi, °F, psi) and
  European units (km, °C, bar). The kia_uvo project ships no state recordings or snapshots, so these are
  synthesized: every entity id is derived from the catalogue exactly as Home Assistant derives it
  (`<domain>.<device name slug>_<entity name slug>`), values are plausible, units follow the integration's
  dynamic-unit behaviour.

## teslemetry/, tessie/

* `entity_catalog.json` – entity names from Home Assistant core's `strings.json` for each integration and which entities ship
  disabled (`entity_registry_enabled_default=False` in the platform files), read from core `origin/dev` at `4368abee2a92`
  (2026-11-09). Both integrations set `translation_key` to the data key, so a name's slug is the entity id suffix.
* `ha_states_us.json` – synthetic states for a plugged-in, charging, parked car in US units (mi, °F, psi), with the optional
  entities enabled. Built from the shapes in the source (enum values, units, `p` gear / 0 speed defaults, timestamp sensor for time
  to full charge); not recorded from a car or a real Home Assistant.

Licences: Home Assistant core is Apache-2.0; kia_uvo is MIT. Only entity ids, names and example values are used.
