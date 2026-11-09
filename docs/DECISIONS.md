# Design decisions

Notable choices made while building, newest last. Format: decision — why.

## Architecture
- **Plugin id/name is `pit-crew`**, defined once in `constants.py`, `plugin.yaml` (`name`), `dashboard/manifest.json`
  and the Desktop `id` in `desktop/plugin.js`. A rename is a search-replace of those four places.
- **Pure standard library at runtime** (no `python_dependencies`): HTTP via `urllib`, state in `sqlite3`, entity profiles as
  JSON (YAML accepted when PyYAML is importable). Keeps install trivial and avoids dependency-policy review.
- **Disk-backed shared state.** The agent process and the Desktop `plugin_api.py` backend may load the package separately, so
  everything that must agree (audit log, alerts, armed-until, simulator car state) lives in the plugin's SQLite DB under
  Hermes' per-plugin data dir, not in module globals.
- **Simulator is the default adapter** and is always labeled "Simulated car". It keeps its own clock (fast-forwardable) and
  persists its state, so every feature (including the watcher rules and readiness) can be tried without owning a car.
- **Internal units are US** (mi, °F, mph, kWh, Wh/mi); adapters convert on the way in using the unit attributes HA reports.
- **Location is a zone, never coordinates.** `VehicleState` has no latitude/longitude field at all, so they cannot leak.

## Safety
- **Dry-run by default.** `armed: false` means commands are validated, policy-checked, audited and *not sent*. `/car arm`
  arms for a limited time (`arm_default_hours`, default 1h) and auto-disarms; `armed: true` in settings is a standing arm.
- **Approval uses Hermes' real pipeline.** For commands that need a human, a `pre_tool_call` hook returns
  `{"action": "approve"}` for `car_command`. The `rule_key` embeds a one-time nonce so choosing "always" can never create
  a standing allowance.
- **Positive confirmation of approval.** Hermes' gate can be bypassed by user config (YOLO, `approvals.mode: off`, cron
  auto-approve). So the tool does not trust "the hook ran": it requires a `post_approval_response` for the same nonce with a
  human "once/session/always" choice (`approval_confirmation: strict`, the default). `hermes_gate` relaxes this to trusting
  Hermes' gate alone. Cron / unattended contexts are blocked outright for approval-class commands (fail closed).
- **`lock`, `close_windows`, `sentry_on` need no approval** (safe direction) but still go through policy, rate limit, audit
  and read-back. Everything else in `security`/`attention` needs a human.
- ~~Unknown motion allowed by default~~ — **superseded in pass 4** (fail closed; see the Pass 4 section).
- **Asleep cars are not woken for comfort commands** unless `wake_ok=true` is passed (the model must ask the user first).
- **Read-back is honest:** confirmed / not_confirmed / unverifiable, never "done" without evidence.

## Delivery of proactive alerts
- Alerts are always stored (Desktop feed, `/car alerts`, `car_alerts`) and surfaced to the model on the next turn through a
  `pre_llm_call` context injection.
- Push to the user's chat uses `ctx.inject_message(session_key=...)` to a chat the user binds with `/car notify here`
  (needs `plugins.entries.pit-crew.allow_gateway_injection: true`, documented). `post_message` only works inside a live
  turn and cron delivery is job-scoped, so neither can serve a free-running watcher.

## Desktop page
- **Own confirmation dialog instead of `ConfirmDialog`.** The SDK's `ConfirmDialog` focuses *Confirm* and makes Enter/Space
  confirm. For car commands that is the wrong default, so the page builds on the SDK's `Dialog*` primitives: Cancel is
  focused, Enter never confirms, and the dialog shows the live safety decision (mode, risk class, every policy check).
- **Layout via inline styles + theme variables.** Tailwind classes written in a plugin file are not guaranteed to exist in the
  app's compiled CSS, so layout uses inline `style` objects with `var(--ui-*)` values (still no hard-coded colors).
- **The page only talks to its own backend** (`ctx.rest`, 12 s polling, plus a `plugin.pit-crew.state.changed` nudge). The
  backend reuses the already-loaded plugin package when present and otherwise loads its own copy; durable state is shared
  through the plugin SQLite DB, so it works under `plugins.isolation: host` too.
- **Connection settings stay in Settings → Plugins** (the manifest `config_schema` form writes config and secrets through
  Hermes' supported route). The page edits only what is safe at runtime: arm/disarm and per-rule toggles/thresholds.
- **Preflight uses a forced fresh read** (no cached state) for every safety decision; found by a test that primed the cache.

## Pass 2 — owner decisions (addendum)
- **Renamed to Pit Crew (`pit-crew`).** Package `pit_crew`, env vars `PIT_CREW_*`, settings path
  `plugins.entries.pit-crew`, event names `plugin.pit-crew.*`, data dir `plugin-data/pit-crew`, catalog `catalog/pit-crew.yaml`.
  The slash command stays `/car` and tools stay `car_*` (the owner allowed it; they read better than `/pitcrew`). The git
  remote is `trevornk/pit-crew`. Existing `hermes-car` data dirs are not migrated (nothing was ever released).
- **Pit Crew keeps its own history** (`history_store.py`; tables `snapshots`, `drives`, `charges`). `history` now defaults to
  `own`. Every fresh state read feeds a recorder that classifies the interval since the previous snapshot as driving, charging
  or idle and merges intervals into episodes. Evidence for driving, in order: odometer advance, reported motion/speed, range
  falling faster than 15 mi/h (idle drain and pre-conditioning are an order of magnitude slower), zone change with a battery
  drop. Distance: odometer > speed×time > range drop (the source is stored in `dist_src`, so zone-only cars like HA
  `tesla_fleet` get honest estimates). Energy comes from battery % × usable kWh; a trip with <2 % drop gets no Wh/mi rather than
  a rounding-noise one. Charge energy added is the % gain; energy drawn is the integral of reported charge power; cost needs the
  optional `electricity_rate_usd_kwh`. Gaps >6 h are never bridged. Only zone names are stored — no coordinates.
- **TeslaMate is optional** — either read directly (`history: teslamate`) or imported once into the own store
  (`/car import teslamate [days]`, idempotent by external key, never duplicating something Pit Crew recorded itself).
- **Simulated car keeps a synthetic past**: `CombinedHistory` shows the simulator's 60 days only for the period before the own
  recorder's first record, so the demo has data on day one and real recordings take over from there.
- **Direct Tesla adapter (`adapters/tesla_direct.py`).** One adapter `tesla`, two modes (`teslemetry`, `tessie`; the `fleet` mode was removed in pass 3 — see below) through
  `tesla-fleet-api`, declared as `python_dependencies: ["tesla-fleet-api>=1.5,<2"]` and imported lazily inside `LibraryClient`
  (the module imports nothing from the package at load time; a test asserts it). Floor 1.5: the oldest release I could confirm
  has every call used (Fleet `vehicle`/`vehicle_data`/commands, Teslemetry, Tessie's own command set, `TeslaFleetOAuth`),
  supports Python 3.10+ at 1.5.0 (newer releases need 3.13, and Hermes supports 3.11–3.14 so pip picks per interpreter); ceiling
  `<2` per the dependency policy. Library knowledge lives in one thin class so a library change touches one place and tests
  mock that class (or inject a fake module).
- **The library is asyncio-only and its errors derive from `BaseException`** (`TeslaFleetError`). A private event-loop thread
  (`AsyncRunner`) bridges to the synchronous adapter API, and `LibraryClient._guard` converts library errors into `AdapterError`
  with text chosen by exception class name — never the library's message, which could contain a token.
- **Never wake to poll** = (1) cheap vehicle-state call first, data call only if `online`; (2) an asleep car returns the last data
  flagged `asleep` with untouched timestamps, so it ages into "stale"; (3) our own minimum poll interval (`tesla_min_poll_s`, 120 s),
  tightened to 4 s for 45 s after a command so read-back can see the change; (4) HTTP 429 → 5 min back-off serving cache; (5)
  `wake_up` exists in exactly one place and runs only after `allow_wake_once()`, which the safety engine calls only when the
  command carried `wake_ok`. A test found and fixed a bug where the min-poll cache hid the asleep flag from the policy.
- ~~Fleet direct read-only until a signing key exists~~ and ~~Catalog rule 11 tension~~ — **superseded by pass 3 (below).**
- **Zones for the direct adapter** come from optional home/extra-zone coordinates in settings; coordinates are read from
  `drive_state` only when zones are configured (which also makes the car show its location-sharing icon), converted to a zone name
  immediately and discarded. No zones configured = location unknown, and location-dependent rules/checks refuse rather than guess.
- **Sleeping-car limitation (accepted):** an unlock/trunk/frunk on an asleep car is refused because its data is stale (`wake_ok`
  does not waive the freshness check, since the decision would be made on old data). Ask for a status after the car wakes.

## Pass 3 — catalog rule 11 compliance (parent decision, supersedes the pass-2 rule-11 note)
- **All plugin-held Tesla OAuth was deleted.** Hermes' manifest `oauth:` route (`_pkce_config`) cannot set the token-exchange
  `audience` Tesla needs or pin the redirect port Tesla's registered redirect URI requires, and signed commands through
  `tesla-fleet-api` need the raw access token. Rule 11 says not to work around a missing route, so native Fleet-direct is not
  shipped. Removed: the `fleet` mode, `TeslaFleetOAuth` use, refresh-token storage and rotation, `TokenStore`/`token_store_path`
  (the file that held the rotated token), reading a private key from a path, the `tesla_region`/`tesla_client_id`/
  `tesla_private_key_path` settings, the client-secret/refresh-token secrets and their env constants, the OAuth-specific error
  texts — and their tests (replaced by tests that assert none of it exists).
- **Tesla Fleet API (own developer app) is supported through Home Assistant's official Tesla Fleet integration**; HA holds and
  refreshes the tokens, and the `tesla_fleet` profile is tested on the owner's car. A native Fleet-direct mode is planned pending a
  Hermes OAuth enhancement (audience + fixed redirect port).
- **One credential route.** Every secret (HA token, TeslaMate token, Teslemetry/Tessie token) is a declared `config_schema` secret
  read via `agent.secret_scope.get_secret(NAME)` in `service.hermes_secret_getter`; adapters only ever call `ctx.secret(CONST)`.
  A scope error (e.g. unscoped read under multiplexing) is logged as a warning instead of silently looking like "no token". Static
  tests forbid `os.environ`/`getenv`/dotenv/keychain/token-file code anywhere in the package.
- **Desktop confirmation hardened (rule 12 review point).** `/preflight` now issues a single-use, command-bound, 2-minute
  `confirm_token`; `/command` for approval-class commands refuses a bare `confirmed: true`. It is still a human click in the plugin's
  own UI rather than Hermes' prompt — flagged for the reviewer in `docs/CATALOG_COMPLIANCE.md` with a fallback (preview-only).
- **Full admission-rule audit** is in `docs/CATALOG_COMPLIANCE.md`; the statically checkable parts run as tests. Added a CI workflow
  that resolves test tooling with `uv --exclude-newer "3 days"` (rule 10 recommendation). `requires_env` stays empty on purpose.

## Pass 4 — motion must be proven (live finding on the owner's car)
- **Fail closed.** `allow_unknown_motion` now defaults to `false` (settings, manifest form, docs, tests). Cause: HA's `tesla_fleet`
  ships `sensor.<slug>_shift_state` and `sensor.<slug>_speed` disabled, so motion was unknown whenever the car was unplugged and the
  old default (allow) made unlock/trunk/frunk/vent fail open on the most safety-relevant check.
- **Freshness is part of "parked".** `VehicleState.motion(now, max_age_s)` ignores indicators older than `max_state_age_s` for the
  "parked" conclusion (a stale zero speed or a stale "plugged in" proves nothing → `None` → refused). A stale *moving* reading still
  blocks (the conservative side). Any indicator saying moving wins over any saying parked.
- **Motion sources in the HA adapter**: speed (mph; km/h converted), gear, and an optional state entity via `moving_state_entity`
  (profile key or `ha_entity_overrides`; `driving` ⇒ moving, other states ⇒ not moving, `unavailable/unknown` ⇒ no vote). The
  `tesla_fleet` profile ships `sensor.teslamate_{slug}_state` as that optional source and a `motion_hint` with the exact HA
  clicks. The combined `moving` field carries the freshest timestamp of the sources that voted "parked".
- **Actionable everywhere**: the refusal text (adapter-specific hint + the opt-out sentence), `status().warnings`, the `/car` card,
  `/car discover` (profile scoring emits a warning when the motion entities are absent and no driving entity exists), and a
  *Needs attention* card on the Desktop page. Adapters without any motion fields get a generic warning.

## Pass 6 — background credentials under profile multiplexing
- `hermes_secret_getter` raises `SecretScopeUnavailable` (an `AdapterUnavailable`) when Hermes raises a class named
  `UnscopedSecretError`; "not set" still returns `None` and keeps the old missing-token message. Rule 11 unchanged: no fallback.
- The degraded state lives in the plugin DB (`background_credential_state`) so the Desktop backend process sees it. Only background
  polls set/clear it; the turn-time fallback (`poll_once(from_turn=True)`) deliberately does neither, so a chat success cannot
  hide the fact that the watcher itself is still blind.
- Fallback = `pre_llm_call` → `Watcher.schedule_turn_check()` → daemon thread via `contextvars.copy_context().run`. Single flight
  and one start per `poll_interval_s`; it calls only `poll_once` (read state, evaluate rules, deliver alerts), a source test
  asserts the watcher module has no command path. If a turn built the adapter, the watcher also works afterwards (the adapter caches
  its token) and clears the state itself.

## Pass 7 — prefer the host's supervised task
- No more `get_running_loop()` probing: `spawn_task` is attempted unconditionally; RuntimeError (older Hermes) or any failure/absence
  → own daemon thread (the coroutine is closed so nothing is left un-awaited). The stop signal is a `threading.Event` polled by the
  coroutine, so `on_unload` works regardless of which thread/loop runs it. Pass 6's degraded mode and turn-time fallback are unchanged.

## Pass 8 (pre-demo fixes)
- **Approval-class commands are never executed from the plugin's own pages.** The page previews the policy checks and *Ask Hermes*
  hands the request to chat; `/command` refuses them while armed. Why: catalog rule 12 — a click in the plugin's UI is not Hermes'
  approval. In dry-run the page may still log a "preview" (nothing is sent). The old `broker.issue_ui/take_ui` token path is left in
  `safety/approval.py` but unused by the API.
- **Composer hand-off API.** Desktop: `host.composer.insertText(null, text, {mode:'block'})` (documented in the Desktop SDK as the
  supported door; fail-closed per address). If no composer answers (this page replaced the chat view) it navigates to `/` (new chat)
  and retries with `'new'`; last resort copies the text via `ctx.os.writeClipboard` and toasts. Web dashboard: the dashboard SDK has
  no composer API, so the most supported equivalent is showing the exact sentence with a Copy button (`navigator.clipboard`).
- **R1 semantics.** While the car is online (status entity says so) an available reading is "confirmed as of" the newest stamp
  among the car's mapped entities; `FieldValue.changed_at` keeps the real last-change time. Offline/asleep cars keep per-entity
  stamps. Trade-off: a single online car with a dead individual entity that still reports a value is trusted; HA marks truly dead
  entities `unavailable`, which already maps to "not available". Two motion tests that modelled "sensor stopped reporting" now model
  it as the car being offline.
- **R11 is setting-based.** HA exposes no reliable charge-schedule entity, so readiness takes an optional `charge_schedule_start`
  (HH:MM). With it, a plugged-in idle car is assumed to start charging then and `charge_start` isn't recommended.
- **R10:** idle voltage (<= 50 V) is never used; last voltage seen while charging is remembered in the plugin DB; with neither, the
  rate is "unknown" (0 kW) rather than a guessed 240 V. A car that never reports voltage keeps the labeled 240 V assumption.
- **R8/R9:** range-only "drives" need the drop to exceed 15 mi/h over the time the range value had *held its previous value*, plus a
  zone corroboration (away, or a zone change); range-derived trips store no Wh/mi and are excluded from efficiency statistics.
- **P1-8 `sys.modules` fallback kept** (not provably unneeded: an in-process mount that hasn't run `register()` still needs it),
  documented in `docs/PR_BODY.md` as registering only the plugin's own private package name.
- **Remote Desktop.** Hermes already has a supported delivery path (Install from Git with the Desktop target, or a `hermes://plugin/install`
  link, then Rescan), so no SDK request was needed; documented in the README and `/car desktop-install`. `docs/SDK_REQUEST.md` not written.
- **Not done on purpose:** codicon `car` for the nav (not in the codicon set), the `door_ajar` simulator event away from home,
  replacing `tesla-fleet-api` (explicitly out of scope).
- **Rule 11 and time zones:** the DST-safe default zone reads `/etc/localtime`'s symlink, never `os.environ`.

## Pass 9
- **Watcher ownership.** The lease heartbeat (60 s, TTL 180 s) is independent of the poll interval, so ownership no longer drifts.
  Push is decoupled from polling: any process (including a forced poll from the dashboard) only *stores* alerts; the lease holder
  pushes every still-`new` alert after atomically claiming it (`new → pushing`, reverted to `new` on failure, stale claims retaken
  after 5 min). A holder whose push is refused (no live gateway in that process) releases the lease and steps aside for 5 min so
  the gateway's watcher takes over. Alerts are never lost: they stay `new` and the next-turn context carries them. On an unpatched
  Hermes with only a dashboard or only a gateway, behaviour is unchanged (the 5-minute yield only matters when someone else can poll).
- **No `/car home here` / "Set home to where the car is".** It would mean persisting the car's coordinates, which the privacy
  design promises never to store. Instead the README and the settings help say to copy the coordinates from a map app, and the
  page/`/car` explain that unlock and the away-from-home alert need them.
- **`teslemetry` / `tessie` accepted as Connection names** (they map to the direct adapter in that mode) instead of making people
  type `tesla` plus a second setting; the manifest `choices` lists them. A third-party adapter name still works from config.yaml
  (the Desktop form only offers the listed choices).
- **Safety beat UX once:** the audit suggested one-click everything. Risky commands (unlock/trunk/frunk/vent/Sentry off/honk/flash)
  still never send from a button: "Go live & ask Hermes" switches mode and drafts the request; Hermes' prompt decides.
- **Settings page.** No custom `registerSettingsPage` wizard: the schema form is the documented surface, so it got grouping by label
  prefix ("Home Assistant · URL"), ordering (Connection first, Advanced last), `choices`, and plain help; the Connect card deep-links
  to it (`/settings?tab=plugins&agent=pit-crew`). A guided wizard with a *Test connection* button is a post-hackathon idea.
- **Rate-limit reservation.** Live commands insert a `sending` audit row inside the same `BEGIN IMMEDIATE` transaction as the
  rate/duplicate check, then finish the row; a crash mid-send leaves a `sending` row that still counts toward the limit.
- **Timeouts are `unknown_outcome`,** not `failed`; the engine reads the car back anyway and reports `confirmed` if the change shows.
- **Privacy.** Fixtures were rescrubbed at HEAD only (history is the owner's separate step). The owner's name stays in LICENSE and
  plugin.yaml.


## Pre-submission pass (0.2.0)

* **Tire pressure is part of the normalized state, in psi.** Six fields (four tires + the car's recommended front/rear pressure).
  Units are converted on the way in (`bar`, `kPa`, `psi`); an **unknown pressure unit makes the reading unavailable** instead of
  guessing, because a bar value read as psi looks like a flat tire.
* **Low-tire threshold, in order:** the rule's `min_psi` if set > 0; else 90% of the pressure the car recommends for that axle
  (BMW and the Tesla API report it); else the adapter's model default (`state.hints["tire_min_psi"]`: 38 psi for a Tesla = 90% of
  a Model 3/Y's 42 psi placard, which Home Assistant does not expose); else 30 psi, which no passenger-car placard is below. So a
  35 psi Kia is not permanently "low", and a Tesla is flagged at ~38.
* **Fast air loss** compares the newest reading to the highest in the last `window_min` (default 30 min) and fires on a drop of
  `drop_psi` (default 3). Readings taken while the car is moving are not tracked (hot tires); stale readings are skipped by the
  engine (R16) so a quiet sensor neither alerts nor resolves an alert. The rule needs *any* tire, not all four (`requires_any`).
* **Disabled-by-default HA entities are data, not folklore:** profile fields carry `disabled_by_default` + `enable_as`, and every
  surface prints the same enable-it instructions. A disabled entity has no state in `/api/states`, so "absent" means "enable it".
* **BMW profile kept, marked deprecated.** HA core removed `bmw_connected_drive` (March 2026). The profile is still correct for the
  last source and is ranked below current profiles by discovery, with the removal explained. The BMW CarData community
  integration is mentioned but not mapped (no source verification was done for it).
* **Kia/Hyundai profile follows kia_uvo v4 entity names** (from `strings.json`); the older ids stay as `alt_entities`. Honk/flash use
  the two hazard buttons, which the integration ships disabled, and say so.
* **Approval tickets:** only the newest matching ticket counts, and a "yes" is valid for 2 minutes. Falling through to an older
  confirmed ticket let a call whose own prompt never happened ride on an earlier approval.
* **The simulator is on the service clock** (and a simulated drive takes time at 35 mph) so tests are deterministic and history
  sees believable speeds.
* **CI runs the real thing.** Hermes v0.23.0 from its lockfile on Python 3.14 (its own requirement; its `uv.lock` is the reviewed
  dependency set, installed with `uv sync --frozen`), Node 22 with exact pins, and a no-skips gate. The quarantine (`--exclude-newer
  "3 days"`) still applies to the test tooling we resolve ourselves.


## Review pass 2 (0.2.0)

* **Odometer rate is judged over the time the reading held its old value** (`ots`, like `rts` for range). Home Assistant refreshes
  the odometer every ~10 min while we snapshot every 2, so a jump of 10 mi in one interval is normal; the 150 mph glitch filter now
  divides by the time since the odometer last changed. A real glitch (600 mi in a fresh interval) is still refused.
* **Fast air loss has a 60 min cool-down after the car last moved**, kept by the engine (`last_moving_ts`, independent of whether
  any tire rule ran while driving), and discards samples taken before the drive. Chosen over comparing with a pre-drive baseline
  because it needs no extra state and tires are back to ambient within that hour. Cost: a leak that starts in that hour is caught by
  the low-pressure rule and the car's own TPMS warning instead. Cars that report no motion get no cool-down (nothing to key it on).
* **Per-tire freshness.** For `requires_any` rules the engine skips only when every input is stale; each rule ignores its stale
  inputs, and an open alert is only kept (not resolved) when it names a tire that is stale right now.
* **Rule minimums** (`RULE_MINIMUMS`): `drop_psi` 0.5, `window_min` 1, `trip_mi` 1, `within_hours` 1, `below_pct` 1..100,
  `min_psi` 0 (auto) or 5..120. `set_rule` refuses lower values; `rule_config` raises a lower stored value to the floor.
* **Kia/Hyundai data time.** Profile fields may carry `data_time_entity` (and `data_time_required`). The field's freshness becomes
  the older of HA's stamp and that timestamp sensor. `shift_state` is required to have it (fails closed to unavailable); lock,
  location and plug fall back to HA's stamp when the sensor is missing, because dropping them would break far more than motion.
* **The car's own tire warning** is `tire_warn_{fl,fr,rl,rr}` (bool). Home Assistant: `binary_sensor.<slug>_tire_pressure_warning_
  <position>` (tesla_fleet key `vehicle_state_tpms_soft_warning_*`; HA core exposes only the soft warning; disabled by default per the
  owner's brief, which could not be re-checked against `binary_sensor.py` since only `sensor.py` and `strings.json` were at hand).
  Teslemetry/Tessie: soft OR hard warning. Rule `tire_car_warning` (high severity, 5 min hold, fresh readings only).
  Simulator: the warning is on below 70% of the placard.
* **Go live defaults to 1 hour** (`arm_default_hours`); existing explicit settings are kept. The limits (15 min - 72 h) did not move.
* **Version**: `constants.VERSION` is read from `plugin.yaml` (one place); `/car help` and the unknown-subcommand reply print it.
* **CI**: actions pinned to commit SHAs (tag in a comment, resolved with `gh api` reads); the validate retry only repeats when the
  output shows a network/5xx/timeout-class error.

## Teslemetry and Tessie through Home Assistant
* **Separate profiles, not `tesla_fleet` as-is.** From HA core `origin/dev` (4368abee2a92): the windows cover is `Vent windows`
  (`cover.<slug>_vent_windows`) in both integrations vs `Windows` in tesla_fleet; Tessie names defrost `Defrost mode` and the front seat
  heaters `Seat heater left/right`; Teslemetry ships Battery range disabled. The rest of the names match. No release tag exists in the
  checkout, so only `dev` was compared. Details and evidence: docs/HA_TESLA_INTEGRATIONS.md.
* **`untrusted_values` on shift state and speed (new profile key).** Both integrations turn a missing gear into `p` and Teslemetry turns
  a missing speed into `0`; Tessie serves its cache (`use_cache=True`), Teslemetry streamed sensors restore their last value after a
  restart, and neither exposes when the *car* last reported, so HA's timestamps cannot rescue it. A "P"/0 reading is therefore
  unavailable and parked has to come from a fresh plug or a TeslaMate state entity; D/R/N or speed above 1 still block. Cost: unlock,
  trunk, frunk and vent are refused for an unplugged car without TeslaMate. Chosen over accepting "P", because a wrong "parked" can
  unlock a moving car.
* **`tesla_fleet` left as-is** although its source has the same `"p"`/`0` defaulting. It is polled straight from the Fleet API, it was run
  on a real car, and changing its motion rules is the owner's call; flagged in the result report as a follow-up.
* **`discovery_signature`.** The HA states API carries no integration domain, and the three Tesla integrations share most names. A few
  entities only one creates (present) or that the others would have (absent) give a small tie-break (up to ±0.1 on the coverage score),
  so a tesla_fleet car keeps the tested profile and cannot be steered by a handful of optional entities.
* **Teslemetry rear seat heaters** are listed as `disabled_command_entities` (they ship disabled); tesla_fleet's identical situation is not
  added to the tested profile.
