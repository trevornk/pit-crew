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
- **Dry-run by default.** A fresh install (`live_mode: on_demand`) validates, policy-checks and audits commands and does *not send*
  them. `/car live` goes live for a limited time (`live_default_hours`, default 1 h) and switches back by itself. (Superseded in
  part by the live-mode decision below: `live_mode: always` replaces the old standing `armed: true`.)
- **Approval uses Hermes' real pipeline, and follows the user's Hermes approval setting** (owner decision, 2026-11-09). A
  `pre_tool_call` hook returns `{"action": "approve", "rule_key": "pit-crew:<cmd>"}` for approval-class `car_command`s. If the handler
  runs, Hermes authorized that exact call under the user's own policy (manual, smart, off, yolo, an allowlist entry); denial,
  timeout and gate errors block before it. The earlier "strict" default refused whatever Hermes had auto-approved, which overrode the
  user's own `approvals.mode: off`. Replaced by `require_human_for` (empty by default): listed commands get a nonce rule key and need a
  recorded human *yes*; with approvals off/yolo they are refused with a message. Audit says `confirmed` (a person answered) or
  `hermes_auto` (Hermes approved without asking). Migration: explicit `approval_confirmation: strict` -> the five old strict
  commands; unset or `hermes_gate` -> empty. `smart` mode: Hermes asks a person for plugin approvals (no guardian involved).
  Cron / unattended contexts stay blocked outright for approval-class commands (Pit Crew's floor, stricter than Hermes `cron_mode`).
  Upstream gap (U2): a plugin cannot ask Hermes for once-only consent or learn how a call was approved, hence the ticket layer.
- **`lock`, `close_windows`, `sentry_on` need no approval** (safe direction) but still go through policy, rate limit, audit
  and read-back. Everything else in `security`/`attention` needs a human.
- ~~Unknown motion allowed by default~~ — **superseded in pass 4** (fail closed; see the Pass 4 section).
- **Asleep cars are not woken for comfort commands** unless `wake_ok=true` is passed (the model must ask the user first).
- **Read-back is honest:** confirmed / not_confirmed / unverifiable, never "done" without evidence.

## Delivery of proactive alerts
- Alerts are always stored (Desktop feed, `/car alerts`, `car_alerts`) and surfaced to the model on the next turn through a
  `pre_llm_call` context injection.
- Push goes through Hermes' cross-platform send (`tools.send_message_tool.send_message_tool({"action":"send","target":...,
  "message":...})`, the function behind cron `deliver=`, `hermes send` (hermes_cli/send_cmd.py:233-240) and the kanban notifier) to a
  *delivery target* string (`telegram`, `discord:#car`, `matrix:!room:server`, `signal:+1...`, bare platform = home channel).
  **Why not `ctx.inject_message` (the earlier design):** it needs a live messaging gateway in the *calling* process
  (hermes_cli/plugins_injection.py:52-53, "no live gateway is available" in a Desktop/CLI process), a gateway session-store entry for the key
  (gateway/run_plugin_injection.py:195, silent "not routed" when the room, which cron posts to daily, had none), and it starts an
  agent turn in that chat for every alert. A plain platform message needs none of that. The inject path was removed, not kept as a fallback:
  where `send_message` is unavailable it could not work either (same host), and a half-working second path hid the failures.
  **Why not `ctx.dispatch_tool("send_message", ...)`** (plugins.md:112, the brief's first idea): `send_message` is intentionally *not* a registered tool
  (tools/send_message_tool.py:25-27, toolsets.py:186 "no agent-callable send_message tool"; a real registry lookup returns `None`), so it would answer "Unknown
  tool". Importing the function is how Hermes' own non-agent callers use it. It is a private-ish module path, so the import is guarded and a missing
  tool is reported ("cannot do a cross-platform send from here"), never crashed on.
  **Target from `/car notify here`:** read from Hermes' session context variables (`gateway.session_context.get_session_env`: platform, chat id, thread id,
  bound for plugin commands by `GatewayRunner._session_env_scope`, gateway/run.py:3998), not parsed from the session key: Hermes' own `_parse_session_key`
  (gateway/run.py:2946) splits on `:` and cannot hold a Matrix room id (`!room:server`). Matrix gets no thread suffix for the same reason.
  **Precedence:** the `alert_target` setting wins when non-empty, else the target stored by `/car notify`; one rule, shown in `/car notify status`.
  **Scope:** the send runs in the caller's context. `Watcher.run` polls and delivers in one `asyncio.to_thread` call, which copies the context, so the
  platform token is read through `agent.secret_scope`/`gateway.config._getenv` in exactly the scope the HA token was read in. A process without a scope
  fails with Hermes' own error, reported, and the watcher lease moves to a process that can send (`kind` scope/unavailable); a wrong target or a platform
  that is down fails everywhere, so those do not move the lease. A Desktop/CLI-only user has no platform: alerts stay stored, on the page and in
  the next-turn context.

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
- No more `get_running_loop()` probing: `spawn_task` is attempted unconditionally; on Hermes v0.23.0 it needs a running loop, so at `register()` it
  usually raises RuntimeError (some newer/patched builds run it on a Hermes-owned loop bound to the profile's secret scope; that is not v0.23.0). Any RuntimeError or any failure/absence
  → own daemon thread (the coroutine is closed so nothing is left un-awaited). The stop signal is a `threading.Event` polled by the
  coroutine, so `on_unload` works regardless of which thread/loop runs it. Pass 6's degraded mode and turn-time fallback are unchanged.

## Real-car test fixes (2026-11-09)
- **R1 revised: awake does not mean fresh.** `sensor.<slug>_charging` read "charging" 36 min after the car stopped, because HA's
  tesla_fleet had not re-written it, while Pass 8's rule lifted every field to the newest stamp. Now each reading uses its own
  `last_reported` (fallback `last_updated`). The Pass 8 reason, a shut door looking days old, is kept for a short list of slow-changing
  fields (`CONFIRMABLE_FIELDS` in `adapters/homeassistant.py`): they are confirmed by the newest stamp among the *other*
  (live) fields, only while the car is online and only if that stamp is under 15 min old. Charging/motion/location are never
  promoted. The real fixture shows doors and the lock re-written together with range and temperature, which is the evidence the
  integration polls them together; a door's `last_changed` on its own is not. Safety policy is unchanged and now sees honest ages
  for charging and motion; a lock/door reading can still be borrowed from a live poll, as in Pass 8.
- **Read-back evidence = written after the command.** `confirmed` requires the verified fields' raw stamps (not borrowed ones) to be no
  older than the send time (5 s skew allowance). A value that already matched but predates the command is "not proof". Chosen over
  trusting a match because HA served a stale "charging" for 36 min. Trade-off: an integration that leaves an unchanged value's
  stamp alone ends as `sent_unconfirmed` unless the pre-state was fresh (below).
- **"Already" instead of "failed".** If the target already held in fresh pre-command data, the command is still sent (cheap, and the
  car may disagree with HA) and the result is `confirmed` with `readback.already` ("already closed"); the message says nothing
  needed to change. Same status vocabulary, no new status, so UI/pages needed no change. Stale pre-state never qualifies.
- **Charging read-back waits up to 90 s (`verify_window_s`), polling every 5 s**, because the Fleet API lags 30-60 s. HA's
  `switch.<slug>_charge` and `number.<slug>_charge_current` are command-optimistic (the integration writes the requested value
  itself), so neither is ever evidence. Not done: asking HA to `update_entity` the sensors. It is a different service call outside the
  profile vocabulary, forces a Fleet API request, and I could not verify offline that it never wakes the car.
- **`charge_amps`** is confirmed only while the car is charging and its *actual* charger current is within 1 A of the request;
  idle, it stays unverifiable. (`charge_current_max_a` in the profile is the number entity's `max` attribute, not the setpoint.)
- **Alert push state is its own thing.** `state` (new/surfaced/acked/resolved) is what the human has seen; `pushed_ts`, `push_attempts`,
  `next_push_ts`, `push_claim_ts` track delivery to the bound chat. Surfacing never cancels a push; acked/resolved alerts are not
  pushed; retries back off 30 s doubling to 30 min; after 6 h the alert is no longer pushed and status says so. The old `pushing`
  state is gone (rows migrate back to `new`).
- **Why the failed push happened (Hermes core, see RESULT5.md):** the watcher lease was held by a Desktop/TUI process, where Hermes
  has no messaging-gateway injector, so `inject_message` logs "no live gateway is available" and returns False. Pit Crew already yielded
  the lease, but the same process's next-turn hook had marked the alert surfaced, which used to end the push. Pit Crew now captures
  Hermes' logged refusal (public logger only) as the reason.
- **Rules that Pit Crew's own commands can trigger.** Only `charging_stopped_unexpectedly` is suppressed (after our `charge_stop`, or a
  `charge_limit` at or below the charge, within 30 min, from the audit log). `plugged_not_charging` already needs 30 min of
  stillness; opening alerts after `open_trunk`/`vent_windows` still fire deliberately: a trunk left open is useful to hear about.
- **Defrost** is Tesla max defrost (heat and fan at maximum): in the tool description, command summary, result message when switched on,
  README and skill.

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

## Approval bridge (Hermes Tool Search)
* **Unwrap `tool_call` in the gate: defence in depth.** An earlier note here claimed Hermes fires `pre_tool_call` only for the bridge
  name. That is wrong for v0.23.0: `agent/tool_executor.py` `_parse_tool_call` -> `_unwrap_tool_search_call` peels the bridge before the
  hook, and Hermes rejects local multi-entry batches itself (`tools/tool_search.py`). The live "no confirmed human approval" seen with
  Tool Search was the strict-mode/approvals-off conflict, not the bridge. The gate still understands the raw `tool_call` name for the
  failure path of that unwrap (an exception there leaves the bridge name), at no cost.
  The ticket is issued under the hook's `task_id`, which the re-dispatch passes unchanged, so `broker.take` finds it; nothing about the
  broker changed.
* **Batches with an approval-class command are refused, not split.** One prompt per hook verdict is all Hermes supports, and a prompt
  that covers several commands would let one yes authorise more than the person saw. (Hermes itself already rejects multi-entry
  batches of local tools; the rule protects against versions that do not.) Comfort-only batches and other tools pass as before.
* **Unreadable bridge arguments fail closed only when they name `car_command`** (or the envelope is unreadable and mentions it);
  unrelated unreadable calls are not ours to block. Dry-run (disarmed) bridge calls need no prompt, same as direct calls.
* **seat_heat read-back skipped.** tesla_fleet exposes `select.<slug>_seat_heater_*` (options off/low/medium/high) and the sanitized
  fixture has the two front ones, but confirming needs five new state fields, a new profile field kind (option text to level), simulator
  and display changes, and the rear seats are absent from the fixture. Not clean; seat_heat stays `sent_unconfirmed`.
* **`/car notify test`** uses the same `send_message` call as real pushes (a plain labelled message, no framing for a model). It writes no
  alert row and does not update `notify_last`, so it cannot change status, rules or dedupe. It reports Hermes' own answer: `send_message_tool` returns
  `{"success": true}` once the platform API accepted the message, else `{"error": ...}` (target not resolvable, platform not configured, no home channel,
  platform error), shown with the reason. That is delivery to the platform, not proof a person read it. (Earlier versions used `inject_message`, whose True
  meant only "a host accepted it"; see "Delivery of proactive alerts".)

## Host-contract audit (2026-11-09)
* **`dashboard/plugin_api.py` `core()`** finds the loaded plugin package in `sys.modules`, else execs its own copy. Not a documented Hermes pattern; it works because durable state lives in the shared DB. A second module copy (isolation `host`, forced reload) has `svc.ctx = None`; the page path never relies on it.
* **Alert rules: one source of truth** (audit #16). Edits from the Pit Crew page, `car_rules` and `/car rules` now write the plugin's own
  `rules` setting (in-process `ctx.set_config("rules.<id>.<key>", v)`; the dashboard backend, which has no ctx, uses
  `hermes_cli.plugins_state.save_plugin_setting`, the writer Hermes' own settings form and `set_config` share on v0.23.0; that module is
  undocumented, so it is imported guarded and a failure is reported, never swallowed). The DB key `rule_overrides` is moved into the
  setting once and deleted; if the move fails (managed install) the old overrides keep applying until it succeeds. The
  Settings form, the page and the tool therefore agree, and the `plugin.yaml` label is true.
* **`/car discover`**: on "yes" it applies `ha_slug`, `ha_profile` and `adapter` with `ctx.set_config` (present on v0.23.0); if that fails or
  the user says no it prints the manual steps as before.
* **Approval hook state read** (audit #6): `pre_tool_call` reuses a car read at most `max_state_age_s` old (policy still judges per-field
  freshness) so a slow Home Assistant/Tesla call can't hit Hermes' 30 s hook timeout (which fails closed and mutes the hook for 60 s);
  the tool's own preflight is always a forced live read before anything is sent. The adapter's own timeout is not changed.
* **Two guarded private imports remain, on purpose** (`hermes plugins validate` lists them as warnings under catalog rule 9, and passes):
  `tools.approval.is_approval_bypass_active` (only to give a clearer early refusal for `require_human_for` commands; the tool-side check is the
  real enforcement, so an ImportError changes nothing but the message) and `hermes_cli.plugins_state.save_plugin_setting` (only for rule
  edits made on the page, whose backend has no `ctx`; in-process edits use the documented `ctx.set_config`). If a release removes either,
  the first degrades to the tool-side refusal and the second reports "couldn't save" instead of silently dropping the edit.

## Second real-car session (2026-11-09, approval commands, `tesla_fleet`)
Ground truth from the owner at the car vs audit rows 22-35: unlock reported "already unlocked" although the doors had just been locked by
hand (the car did unlock); open_trunk/open_frunk "confirmed" ~1 s after sending, and `car_status` still said open after the owner closed
both by hand; vent/close windows physically worked but read `not_confirmed`.
* **Why (Home Assistant core `dev` @ c459f2ef54b3868be07bc1a6738bf0c6b836060d).** tesla_fleet commands are *optimistic*: `lock.py` L54-69
  `await handle_vehicle_command(self.api.door_unlock()); self._attr_is_locked = False; self.async_write_ha_state()`; the same in
  `cover.py` (windows L80-97, charge port L124-137, frunk L160-165, trunk L190-205) and `switch.py` L160-175. The car is only re-read by
  the coordinator, every `VEHICLE_INTERVAL_SECONDS = 600` (`coordinator.py` L33); a refresh calls `_handle_coordinator_update` on every
  entity (`entity.py` L90-94), re-writing all of them in one pass. tesla_fleet has no streaming, so a door locked by hand is invisible for up to
  10 minutes. teslemetry (`lock.py` L71-86, `cover.py` L123-142) and tessie (`entity.py` `set()` L93-97, used by lock/cover) do the same.
  The window binary sensors are not written by the command, which is why vent/close stayed unconfirmed.
* **Design: an integration's own write is "accepted", never "confirmed".** Profiles say `optimistic_commands: true` (tesla_fleet, teslemetry_ha,
  tessie_ha). The adapter takes the newest stamp among the mapped entities no command writes (battery, range, temperatures, door/window
  sensors...) as "the car was last re-read"; a command-target entity (lock, trunk/frunk cover, switches...) stamped later than that
  (+2 s for the same refresh pass) is `FieldValue.assumed`. Read-back never confirms from an assumed field: it keeps re-reading (free
  `GET /api/states`) for the usual window and then reports `readback: accepted` / status `sent_unconfirmed` ("the car's service accepted
  it; the car has not reported back"). A coordinator refresh that shows the change confirms; one that shows the old value is
  `not_confirmed`. Assumed fields are listed in `car_status` `freshness.assumed_after_command` and do not count towards `newest_update` (so
  an optimistic write cannot make the data look fresh for the policy either). The earlier rule stays: values written before the command
  are never proof.
  * Chosen over requesting `homeassistant.update_entity`: that forces a paid Fleet API `vehicle_data` call per command, is a service outside
    the profile vocabulary, and on a car that took the command 1 s ago would still race Tesla's own state update. Chosen over a new
    top-level status: `sent_unconfirmed` already means "don't rely on it"; `readback: accepted` carries the difference, so pages need
    no change. Errs to under-claiming: a teslemetry *streamed* update of the lock entity also looks assumed until the next poll.
  * Tessie's coordinator re-reads Tessie's cache every 10 s; a refresh there proves Tessie's copy, not the car (documented in the profile).
* **"Already" needs the car's report from under a minute ago** (`ALREADY_MAX_AGE_S = 60`, not an assumed value). The brief's alternative,
  "newer than the last command for that field", was rejected: in the real timeline there was no earlier lock command, so it would have
  repeated the false "already unlocked" (hand changes are not commands). An older matching reading is mentioned ("the last reading
  (5 min old) said unlocked, but it may be out of date, so the command was sent anyway") and proves nothing. If the car did report the
  state < 60 s before, HA re-writing the same value after the command does not undo the "already".
* **Practice-run approval wording** comes from the same inputs as the live path (`safety/gate.py approval_mode_sentence`: Hermes' bypass
  state via the guarded `is_approval_bypass_active`, and `require_human_for`). Off/yolo: "Hermes approvals are off, so it would be sent
  without asking"; on: "Hermes would ask you first"; listed: "you'd be asked every time" (or "refused until you turn approvals on" when
  approvals are off). Outside a Hermes session the bypass state reads as "on".
* **Timing is measured, not guessed.** Live audit rows end with `timing: service answered in N ms, read-back took M ms` (wall time
  around `send_command` and around the read-back); results carry `timing_ms`. Commands with no observable result (flash, honk) return
  without any read-back wait. The ~20 s the owner saw for flash is mostly outside Pit Crew (agent turn, Tesla cloud to car): see RESULT10.
* **Live length is a setting** `live_default_hours` (whole hours 1-24, default 1; bad values fall back to 1): used by the page's Go live
  and by `/car live` without a number. An explicit `/car live N` keeps 15 min-72 h. No "forever" for a timed window (worker 11: for no time
  limit the owner picks `live_mode: always`, below). The old `arm_default_hours` key is read when the new one is not set. In
  `on_demand` a setting never arms anything by itself.

## Always available control mode, owner-adjustable protections (worker 11, owner decision 2026-11-09)
* **`live_mode`: `on_demand` (default) | `always`.** The owner found running `/car live` before every use a dealbreaker. `always` sends
  for real without arming; `/car stop` / Stop is a *pause* (`arm_state.paused`) that lasts until `/car live` / Go live, which resumes
  with no expiry (a number given to `/car live` does not create a limit in this mode, and the reply says so). `on_demand` is exactly the
  old behaviour. A stop in `on_demand` leaves no pause behind, so switching to `always` later starts live; a pause made in `always` is
  remembered across a switch away and back. The setting is read on every check (`armed_info`), so a change in the settings form applies
  at once; the pages re-read on `settings.changed`. The old standing `armed: true` (an Advanced checkbox) becomes `live_mode: always`
  when `live_mode` is unset, and `armed` is no longer a form field.
* **Status words are one set everywhere:** "Live: always on", "Paused (practice) — /car live to resume", "Live until 4:39 PM",
  "Practice mode · nothing is sent" (`CarService.live_label`; the pages build the same words from `armed.state`).
* **Owner-adjustable protections** are settings with plain labels and a conservative default: `zone_rule` (`trusted_zones` |
  `anywhere`; the per-call `override_zone` stays for one-off use), `wake_for_commands` (`ask` | `auto`; `auto` lets the policy pass an
  asleep car for a requested command and calls the direct adapter's `allow_wake_once`; status reads still never wake), and the numeric
  ones are clamped to bounds instead of being switch-offable: `max_state_age_s` 60-3600, `rate_limit_commands` 1-60,
  `rate_limit_window_s` 60-86400, `dedupe_window_s` 5-600. Unknown choice values fall back to the conservative default.
* **Every refusal names the setting that would allow it** (label + key), and the ones nothing can lift say "no setting changes that".
  The setting hint comes before the agent-directed "If the user…" sentence, so the pages' shortened refusal still shows it.
* **Fixed invariants (not settings):**
  * Never unlock/open while moving or not provably parked — a door or trunk opening on the road endangers people; no convenience outweighs it.
  * Approval-class commands never from unattended/cron/background runs — nobody is there to answer, and a schedule must not open a car.
  * The agent can never arm itself or change these settings via a tool — a confused or prompt-injected model must not lower its own guard.
  * Truthful results (sent vs accepted vs confirmed) — not a protection, a promise; never configurable.
* **A fresh "asleep" reading is parked evidence** (a Tesla cannot sleep in gear): the mall case. Only a reading within
  `max_state_age_s`; "just woken" (asleep now false) is *not* evidence, because a car that just woke may be about to drive and its
  gear reading is stale. Any reading of D/R/N or speed > 1 mph, fresh or stale, still refuses first. Known limit: HA tesla_fleet's
  `binary_sensor.<car>_status` is off for both asleep and offline; an offline car cannot take the command, and one that comes back
  and drives reports gear/speed again on the next poll.
* **No waiting for a report that cannot arrive** (`report_latency_s` per HA profile: tesla_fleet 600 — coordinator polls every 600 s,
  no streaming; teslemetry_ha 5 — streaming; tessie_ha 10 — coordinator re-reads Tessie's cache every 10 s; the simulator 0; unknown
  = None). When the latency is more than twice the longest read-back wait for that command (`verify_timeout_s`, or a charging
  command's 90 s window), the engine reads ONCE right after sending and answers: `confirmed` if that read already carries the car's
  report, otherwise `readback: accepted` / `sent_unconfirmed` with "Tesla accepted it. The car reports back within ~10 min; check
  car_status later". Other profiles keep the full wait (and the long charging window where it can help). Rejected: shortening
  `verify_timeout_s` globally (it would cut the wait where a report can arrive in time).

## Fresher car state (worker 12, owner request 2026-11-09)
The owner found a 10-minute-old picture of the car confusing. Two changes; neither touches what counts as proof.
* **One re-read after a command (reverses the RESULT10 rejection of `homeassistant.update_entity`).** The owner's agent judged one
  vehicle-data request per user command (~$0.002 at Tesla's 500 requests per $1) worth a truthful confirmation. Home Assistant core
  `dev` @ c459f2ef54b3868be07bc1a6738bf0c6b836060d: `homeassistant.update_entity` (components/homeassistant/services.py L197-203) ->
  `entity.async_update_ha_state(True)` (helpers/entity.py L1018-1039; an exception is logged and swallowed, so a 200 proves nothing)
  -> `CoordinatorEntity.async_update` -> `coordinator.async_request_refresh()` (helpers/update_coordinator.py L729-740, L319-324;
  debouncer `immediate=True`, cooldown 10 s, awaited, so HA answers once the refresh finished, or at once when a refresh ran < 10 s
  before). tesla_fleet's vehicle coordinator (coordinator.py L174-205) first calls the free `vehicle()` and returns its old data when the
  car is not online, so **a refresh never wakes the car**; only an online car costs a `vehicle_data` request. Every entity is then
  re-written (`always_update` defaults True, update_coordinator.py L607-613; entity.py L90-94), including when the car was asleep.
  * Design: profile `refresh_after_command: {entity: sensor.{slug}_battery_level, settle_s: 5}` (tesla_fleet only; teslemetry/tessie
    report within seconds). Setting `refresh_after_command` (default true). After a live command with an observable result on a slow
    profile (the existing single-read path), the engine waits the settle delay (Tesla needs a moment), POSTs `update_entity` once, then
    reads once. Settle + request are bounded by `REFRESH_BUDGET_S = 10`. Not for status reads (only the engine's send path calls it),
    not for flash/honk, not when the car reported the requested state < 60 s before ("already").
  * Verdict: `confirmed` only when the car's report written after the command shows the change, with the existing rules (no assumed
    value) plus: the re-read must show the car awake (an asleep car's refresh re-writes old data). A re-read after the command that
    shows the old value is `not_confirmed`; anything else (request failed, HA did not re-read, car asleep, data unavailable) is
    `accepted` with the reason. `readback.refreshed` marks this path; audit detail adds settle + request time.
  * Found while testing: `update_entity` re-writes the requested entity itself even when the coordinator skips the refresh
    (cooldown). That stamp would have looked like a car re-read and turned an optimistic write into "confirmed". The refresh entity is
    therefore never re-read evidence (`_not_evidence`), its own stamp is capped to the last real re-read, and it does not feed the
    "confirmed while online" promotion.
* **TeslaMate as an optional fresher source next to Home Assistant.** When `teslamate_url` is set with the HA adapter, every state read
  also GETs TeslaMateApi `/api/v1/cars/:id/status` and merges, per field (locked, doors, windows, trunk, frunk, Sentry, shift/speed/moving,
  place, charging state/power, battery/range), whichever source reported more recently. TeslaMateApi (src/v1_TeslaMateAPICarsStatus.go @
  3e158df077165237130072c82d07878ab448f4f8) exposes **no per-field or per-poll time**, only `state` and `state_since`. So: asleep /
  offline / suspended -> values as of `state_since`; awake -> 60 s old (`TM_AWAKE_AGE_S`, an assumption: the API does not say when
  TeslaMate last polled); a value that differs from Pit Crew's previous look -> as of that look (it was published after it). An HA value
  that is only an optimistic write competes with HA's last real re-read time. Motion fails closed: if either source says D/R/N or speed
  > 1, that reading is kept whatever its age. Read-back: right after a command is accepted the engine calls `adapter.after_send`, which
  takes a TeslaMate look; a later change is therefore stamped after the send, and a TeslaMate-sourced reading must be stamped strictly
  after the send (no 5 s skew allowance) to count. TeslaMate never commands (the client only has GET). `car_status` shows
  `freshness.field_sources` and `freshness.source_note`. TeslaMate down -> HA only, said in `source_note`. Without a TeslaMate URL
  nothing changes. HA's TeslaMate MQTT entities are not used (the owner's HA has only four).
