# Writing an adapter (any car, any data source)

Pit Crew has two layers:

* **Brain** (shared, platform-independent): the normalized `VehicleState`, the command vocabulary with risk classes, the
  readiness engine, the watch rules and the safety engine. You never touch it.
* **Adapters**: one small module per car platform. It says what the car can *read* and *do*, fetches a `VehicleState`, and
  sends commands. The brain never offers anything the adapter didn't declare.

Built in: `simulator`, `homeassistant` (+ entity-mapping profiles), `tesla` (direct: Teslemetry / Tessie via
`tesla-fleet-api`), `teslamate` (read-only; live view or optional history import).

| Adapter | Source | Commands | Notes |
|---|---|---|---|
| `simulator` | built in | simulated | default; seeded events; fast-forward |
| `homeassistant` | HA REST + JSON profile | via HA services | any brand with an HA integration |
| `tesla` | `tesla-fleet-api` (lazy import) | via the library | modes `teslemetry`, `tessie` (static token via the secret scope); never wakes the car to poll; no plugin-held OAuth |
| `teslamate` | TeslaMateApi (GET only) | none | optional; history import: `/car import teslamate` |

### History is not the adapter's job

Pit Crew records trips and charging sessions itself from whatever `fetch_state` returns (`history_store.py`). To get good
history from your adapter, report `odometer_mi` (exact distance), `moving`/`speed_mph`, `charging_state`, `charge_power_kw`,
`battery_pct`, `range_mi`, `outside_temp_f` and `location_zone` when you have them — with none of those it falls back to
battery-range changes. A `HistoryProvider` is only for sources that already hold history (like TeslaMate) and can be copied in
with `import_history()`.

## The contract

```python
class CarAdapter:
    kind = "mycar"            # the value of the `adapter` setting
    label = "My car"          # shown in the UI / status
    simulated = False

    def capabilities(self) -> Capabilities: ...            # which state fields + commands this car supports
    def fetch_state(self, now: float) -> VehicleState: ...  # cheap; MUST NOT wake a sleeping car
    def send_command(self, command: str, args: dict) -> CommandResult: ...   # only called after policy/approval/armed checks
    # optional: def allow_wake_once(self): ...   # called right before send_command when the human allowed waking the car
```

Rules the safety engine relies on:

1. **`fetch_state` is cheap and never wakes the car.** The watcher calls it every couple of minutes. If your platform bills
   or rate-limits data calls (Tesla Fleet API does), cache inside the adapter, check the car's online state first, and
   return the last data marked `asleep` for a sleeping car — see `adapters/tesla_direct.py`.
2. **Units are US**: miles, °F, mph, kW, kWh. Convert on the way in.
3. **Per-field freshness.** `FieldValue(value, updated_at_epoch_seconds)`. If your platform has no per-field timestamps, use
   the time the platform last heard from the car (or "now" if it is live). A field the car doesn't report is
   `FieldValue(None, None, available=False)` — never a made-up default.
4. **Report motion** (`speed_mph`, `shift_state` or `moving`) with a real timestamp: unlock/trunk/frunk/vent are refused unless a *fresh*
   reading proves the car is parked. Optionally implement `motion_hint() -> str` and `motion_warnings() -> list[str]` for setup help.
5. **Location is a zone**, never coordinates: `home`, `away`, or a named zone, in `location_zone`. There is no coordinate field.
6. **`send_command` must not retry** in a way that could double-fire. Raise `AdapterError` with a message that is safe to show
   (no tokens, no URLs with secrets). Return `CommandResult(accepted=True)` once the platform accepted it — the engine does
   the read-back by calling `fetch_state` again.
7. **Declare honestly.** Commands you list in `capabilities()` are offered to the model and the UI.

### State fields

`battery_pct, range_mi, charging_state (charging|starting|stopped|complete|disconnected|no_power|unknown), plugged_in,
charge_power_kw, charge_limit_pct, charge_eta_min, charge_current_a, charge_current_max_a, charger_voltage_v,
charge_port_open, locked, doors_open (list of names), windows_open (list), trunk_open, frunk_open, sentry_on, user_present,
climate_on, cabin_temp_f, outside_temp_f, target_temp_f, defrost_on, location_zone, shift_state, speed_mph, moving, asleep,
software_update_available, software_version, odometer_mi, tire_psi_fl / tire_psi_fr / tire_psi_rl / tire_psi_rr (psi),
tire_rec_front_psi / tire_rec_rear_psi (psi, the car's recommended cold pressure)` — provide what you have. Pressures must
be psi; a pressure whose unit you don't know is better reported as unavailable than guessed (a bar value read as psi looks like
a flat tire).

### Commands and risk classes

| Risk | Commands | Human approval |
|---|---|---|
| `comfort` | `climate_on`, `climate_off`, `set_temp`, `defrost`, `seat_heat`, `charge_limit`, `charge_start`, `charge_stop`, `charge_amps` | no |
| `security` (safe direction) | `lock`, `close_windows`, `sentry_on` | no |
| `security` (risky) | `unlock`, `open_trunk`, `open_frunk`, `vent_windows`, `sentry_off` | **yes** |
| `attention` | `honk`, `flash` | **yes** |

The vocabulary and argument ranges live in `brain/commands.py`. Read-back verification is derived from the state fields
a command affects (e.g. `lock` is confirmed when `locked` becomes `True`), so reporting those fields makes your
commands verifiable; if you don't, the engine honestly reports "sent, can't confirm".

## A minimal adapter

```python
# my_car_adapter.py  (a complete, working read-only adapter)
import time


def register(registry):
    """Entry-point hook: receives the plugin's adapter-registry module."""

    class MyCar(registry.CarAdapter):
        kind = "mycar"
        label = "My car (example)"

        def capabilities(self):
            return registry.Capabilities(
                state_fields=frozenset({"battery_pct", "range_mi", "locked", "location_zone"}),
                commands=frozenset({"lock"}))

        def fetch_state(self, now):
            soc = 63.0                      # ... call your platform's API here ...
            fv = lambda value: registry.FieldValue(value, now)
            return registry.VehicleState(
                name="My car", source=self.kind, captured_at=now,
                fields={"battery_pct": fv(soc), "range_mi": fv(soc * 2.8),
                        "locked": fv(True), "location_zone": fv("home")})

        def send_command(self, command, args):
            if command != "lock":
                raise registry.CommandNotSupported(command)
            # ... tell your platform to lock ...
            return registry.CommandResult(accepted=True, detail="lock requested")

    registry.register_adapter("mycar", lambda ctx: MyCar())
```

Ship it as a normal Python package and advertise the hook:

```toml
# pyproject.toml of your package
[project.entry-points."pit_crew.adapters"]
mycar = "my_car_adapter:register"
```

Install it into Hermes' environment (`pip install` / `uv pip install` into the same Python as Hermes), set the plugin's
*Car adapter* setting to `mycar`, restart. A third-party adapter that fails to import or register is logged and skipped;
it can never break the plugin.

`ctx` passed to your factory (`BuildContext`) has `settings` (the plugin settings), `secret(name)` (reads a secret through
Hermes' secret scope — the only credential route; never read `os.environ` or credential files), `store` (the plugin DB) and `clock`.

A **history provider** is the same idea: subclass `HistoryProvider` (`drives`, `charges`, `battery_health`) and call
`registry.register_history("name", factory)`; select it with the *History source* setting.

## Home Assistant profiles (no code)

For any brand that has a Home Assistant integration, you often don't need code at all — a profile maps entities to fields
and services to commands. `adapters/profiles/tesla_fleet.json` is the complete reference. A custom profile can live anywhere:
set *HA entity profile* to its path (JSON, or YAML if PyYAML is installed).

```jsonc
{
  "name": "my_brand", "label": "My brand via HA", "tested": false,
  "fields": {
    "battery_pct":   {"entity": "sensor.{slug}_battery", "kind": "number"},
    "range_mi":      {"entity": "sensor.{slug}_range", "kind": "number", "dimension": "distance"},   // km -> mi automatically
    "locked":        {"entity": "lock.{slug}_door", "kind": "lock_locked"},
    "doors_open":    {"kind": "open_list", "entities": {"front_driver": "binary_sensor.{slug}_front_door"}},
    "location_zone": {"entity": "device_tracker.{slug}", "kind": "zone"}
  },
  "commands": {
    "lock":   {"steps": [{"service": "lock.lock",   "entity": "lock.{slug}_door"}]},
    "set_temp": {"steps": [{"service": "climate.set_temperature", "entity": "climate.{slug}",
                            "data": {"temperature": {"arg": "temp_f", "unit_of": "temperature"}}}]}
  }
}
```

Top-level profile keys also include `moving_state_entity` (a state entity whose value `driving` means moving, e.g. TeslaMate's
`sensor.teslamate_{slug}_state`) and `motion_hint` (text shown when no motion source exists).

Field kinds: `number`, `attr`, `on_off` (`invert`), `enum` (`map`), `timestamp_minutes`, `cover_open`, `lock_locked`,
`open_list`, `attr_open_list` (openings reported as attributes of one entity, e.g. BMW's Lids: `{"entity": …, "attrs":
{"front_driver": "leftFront"}}`), `attr_open` (one such attribute as a boolean), `climate_on`, `zone`, `shift`. Units are
converted from the entity's reported unit via `dimension` (`distance`, `temperature`, `speed`, `power`, `energy`, `pressure`;
an unknown pressure unit makes the field unavailable).

Other per-field keys: `optional` (not counted against discovery coverage), `alt_entities` (older entity ids of the same thing,
tried after `entity`), and `disabled_by_default` with `enable_as` (the entity's name in Home Assistant) for entities the
integration ships disabled: discovery, `/car` and `car_status` then tell the user exactly which ones to enable.
Top-level `disabled_command_entities` lists disabled controls a command needs, `integration_title` names the integration in
those instructions, `verified_against` records what the profile was checked against, and `deprecated` /
`deprecated_note` mark a profile for an integration that no longer works (it is ranked below current ones). `/car discover` scores the bundled profiles against your HA and
proposes a generic mapping when none fits.

Label a profile you haven't verified on a real car as `"tested": false` — the UI and status say so. If you checked it against
the integration's source and fixtures, say that in `verified_against`; the status then reads "verified against the integration
source and fixtures; not run on a real car" instead of "untested". The bundled BMW and Kia/Hyundai profiles are checked this way
by `tests/test_brand_profiles.py` against `tests/fixtures/integrations/` (see its README for provenance).

## Testing your adapter

Reuse the scriptable stub in `tests/stubs.py` as a model, and the fake Home Assistant / TeslaMate servers in
`tests/fakes.py`. The sanitized recordings in `tests/fixtures/live-sanitized/` show real entity and API shapes.
