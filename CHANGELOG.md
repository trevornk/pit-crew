# Changelog

## 0.2.0 (unreleased)
- **Approval-class commands now follow your Hermes approval setting.** Unlock, trunk, frunk, vent, Sentry-off, honk and flash no longer
  second-guess Hermes: with approvals on (manual, or smart: Hermes asks a person for plugin approvals) Hermes asks you; with
  `approvals.mode: off` or yolo, or an already-allowed key, Hermes doesn't ask and neither does Pit Crew. The result says "Sent without
  asking" and `/car log` shows "approved by Hermes (no prompt)". The old `approval_confirmation` setting is replaced by
  *Advanced · Always ask a person for these* (`require_human_for`, empty by default; an explicit `strict` migrates to the five old
  strict commands). Rule keys are now stable (`pit-crew:unlock`) unless the command is in that list. Cron stays blocked.
  Also: README/`/car desktop-install` no longer claim `hermes plugins update` reloads running processes; the Tool Search notes are
  corrected (Hermes unwraps the bridge before hooks; the gate's handling is defence in depth); the hook reuses a recent car read.
- **Host-contract fixes.** Alert-rule edits (page, `car_rules`) now write the plugin's `rules` setting instead of a hidden database copy
  (old edits migrate once). `/car discover` applies its suggestion when you say yes. The status-bar chip is a normal item you can hide from the
  status bar's right-click menu. The Desktop page refreshes at once when you change a setting; "Ask Hermes" opens a new chat with Hermes'
  own action and retries a few times. Alerts are no longer consumed by a subagent's turn. `/car notify test` says "Hermes accepted the test
  message" (not "delivery works") and Pit Crew refuses to bind a chat without a session key. The web dashboard stops polling while hidden.
- **Fixes from the 2026-11-09 real-car test (Home Assistant `tesla_fleet`).**
  - *Stale data shown as fresh.* An awake car no longer lifts every reading to "now". Each reading keeps its own `last_reported`
    time; only slow-changing ones (lock, doors, windows, trunk/frunk, Sentry, port, plug, limits, versions) are confirmed by the
    car's live readings (battery, range, temperatures, charging...), and only when those are themselves under 15 min old. Charging
    state/power/current, motion and location are never promoted, so `car_status` shows a 36-minute-old "charging" as stale with its age.
  - *Read-back.* Confirmation needs a value the car's integration wrote after the command; charging commands wait up to 90 s for the
    Fleet API; `charge_amps` is confirmed by the car's actual charger current while charging (HA's own echo proves nothing) and
    is otherwise unverifiable; a command whose target state already holds (fresh data) reports `confirmed` with `already` set
    ("already closed") instead of "state still shows the old value".
  - *Alerts.* A push failure now records why. Showing an alert in another chat's next turn no longer cancels the push to the
    bound chat: push state (`pushed_ts`, attempts, next try) is separate, retried with backoff for 6 hours, then given up and
    reported in status. `charging_stopped_unexpectedly` is not raised after Pit Crew's own `charge_stop`/`charge_limit`.
    The alerts table gains push columns (migrated automatically).
  - *Defrost* is documented and reported as maximum heat and fan.
- **Approvals now work with Hermes Tool Search.** When Hermes defers plugin tools, the model calls `car_command` through the bridge
  tool `tool_call`, and Hermes fires `pre_tool_call` only for `tool_call`, so the approval prompt never appeared and every
  approval-class command (unlock, trunk, frunk, vent windows, Sentry off, honk, flash) failed closed with "Not sent: no confirmed
  human approval". The gate now looks inside the bridge call (list, JSON-string or single form) and applies the same checks. Works on
  unpatched Hermes. A `tool_call` batch containing an approval-class `car_command` is refused ("send approval-needed car commands on
  their own, one per tool_call"); comfort-only batches pass; unreadable bridge arguments that name `car_command` block.
  Tested against the real Hermes hook dispatch and bridge re-dispatch.
- **`/car notify test`** pushes a clearly labelled test message ("Pit Crew test alert: delivery works") through the same path as real
  alerts to the chat bound with `/car notify here`, and says whether it worked. It creates no alert and changes no rule or dedupe state.
- **Tire pressure.** Per-tire pressure (psi) and the car's recommended pressure are part of the normalized state, mapped for Home
  Assistant `tesla_fleet` (the four *Tire pressure* sensors), Teslemetry/Tessie (`tpms_pressure_*`, with per-tire freshness),
  BMW and Kia/Hyundai, and the simulator (temperature-aware, with leak / low-tire / pump-up controls). Shown in `car_status`,
  `/car`, the Desktop page and the web dashboard. Three new alerts (twelve in all): *the car reports a tire warning* (its own TPMS warning: Home Assistant `tesla_fleet` *Tire pressure warning* sensors, Teslemetry/Tessie `tpms_*_warning_*`), *a tire is low on air* (your psi, else 90% of the
  car's recommended pressure, else 38 psi for a Tesla / 30 psi for anything else) and *a tire is losing air fast* (a drop of N psi
  within a window). Stale, unavailable, implausible or non-finite readings never alert.
- **Home Assistant `teslemetry` and `tessie` integrations** get their own profiles (`teslemetry_ha`, `tessie_ha`), verified against
  Home Assistant core's source (not run on a real car; `tested: false`). `/car discover` picks the right one from a few entities each
  integration alone creates. New profile keys `untrusted_values` (a "P" gear / 0 speed that those integrations fill in for missing
  data never proves the car is parked) and `discovery_signature`. See docs/HA_TESLA_INTEGRATIONS.md for the mapping and verdicts.
- **Review fixes.** Trips no longer lose miles when Home Assistant refreshes the odometer less often than Pit Crew polls; a tire
  cooling after a drive is no longer reported as a leak (60 min cool-down, documented); one sleeping tire sensor no longer silences the
  rules for the other three; rule settings have sane minimums (a 0 psi drop can't fire on any two readings); on the Kia/Hyundai profile
  a "P" gear no longer proves "parked" from hours-old cloud data (its data time comes from *Last Updated At*); CI actions are pinned to
  commit SHAs and `hermes plugins validate` is retried only on network errors.
- **Go live now lasts 1 hour by default** (was 8; the 15 min - 72 h limits are unchanged). `/car help` and an unknown subcommand name the
  running Pit Crew version. README: update-and-restart steps for Desktop, the dashboard and the gateway.
- **Disabled-by-default Home Assistant entities** (Odometer, Shift state, Speed, tire pressures, Kia's hazard-light buttons) are now
  explained: `/car discover`, `/car`, `car_status` and the rule list say exactly which to enable. The `tesla_fleet` odometer mapping
  was confirmed against Home Assistant core (`sensor.<slug>_odometer`, miles, disabled by default).
- **Brand profiles** verified against the integrations' source and fixtures (not run on a real car): Kia/Hyundai now targets the
  `kia_uvo` v4 HACS custom integration (full field/command coverage, older entity ids kept as aliases); BMW was corrected (the
  charge-limit entity never existed) and is marked deprecated because Home Assistant removed the integration in March 2026. New
  profile features: attribute-based openings, alternate entity ids, disabled controls, deprecation notes.
- **CI** now runs everything: the Node/jsdom page tests (pinned react/react-dom/jsdom + lockfile in `tests/node`) and the plugin-load
  and approval-pipeline tests against Hermes Agent v0.23.0 installed from its own `uv.lock`, fails if any test is skipped, runs
  `hermes plugins validate`, and uses current Node 24 actions. Page tests skip with a clear reason when Node is absent locally.
- **Bug-audit fixes** (each with a regression test): NaN/inf sensor values (a NaN speed counted as "parked"), gateway timeouts on
  commands, an approval for an earlier call that never ran vouching for a later one, secrets/zone names reaching approval prompts and
  tool results, impossible trips reported as "tight", unlocked/Sentry alerts while driving, delivery races resurrecting resolved
  alerts, the watcher not restarting after a reload, runaway charge-energy and odometer-glitch history, the Desktop dialog stuck after
  one command, the web page showing stale data as live, double-sent commands, and unvalidated page API bodies. "in 2 hours" now means
  two hours from now.
- **Flaky test fixed** (`test_simulated_car_records_new_activity…`): it depended on the time of day; the simulator now follows the
  service clock, and a trip that starts by unplugging is recorded.

## 0.1.0 (unreleased)
- **Pass 9 (pre-publication polish).**
  Security: the watcher lease is renewed on a fixed heartbeat (no more drifting to the dashboard process); alerts are stored by
  any process and pushed only by the lease holder via an atomic claim (reverted on failure), and a process that cannot deliver
  yields the lease; a crashed poll can no longer end the watcher; only Hermes' approval counts as approval (dead UI path
  deleted); rate limit and duplicate check are atomic across processes; DST-correct readiness hours; past/Feb-29 departures are
  clean errors; NaN rule values rejected; URL userinfo never echoed; alert text sanitised; model's reason labelled unverified;
  timeouts report *unclear whether sent*; DB files 0600; adapters are closed when replaced; warnings for plain-HTTP/unverified TLS.
  UX: one vocabulary (Practice mode / Live), Go live in the header and in the command dialog, quick actions (Lock · Warm up ·
  Charge · Ask Hermes), a Connect-your-car card, grouped and labelled settings with choices (`teslemetry`/`tessie` accepted as
  connection names), `/car ready|lock|warm|live|practice`, readable audit/alerts/policy text (a passing check no longer says
  "outside"), web page parity (alert fixes confirm first, inline Go live). Privacy: fixtures and tests scrubbed. README rewritten.
- Skeleton: manifest v2, normalized `VehicleState`, command vocabulary with risk classes, adapter contract + registry,
  simulated car, `car_status` tool.
- Milestone 2: Home Assistant adapter with entity-mapping profiles (tested `tesla_fleet`, untested BMW and Kia/Hyundai
  starters) and auto-discovery, read-only TeslaMate history/live adapter, `car_history`, `car_readiness`
  (weather-aware readiness engine), departure-time parsing; tests against sanitized live fixtures and fake HTTP servers.
- Milestone 3: safety. Dry-run by default with timed arming, policy engine (moving / stale / untrusted zone / asleep /
  bounds), human approval through Hermes' approval pipeline with positive confirmation and one-time rule keys, fail-closed
  unattended handling, rate limit + de-duplication, read-back verification, SQLite audit log, VIN/token redaction, and the
  `car_command` tool.
- Milestone 4: proactive watcher (supervised task / thread, lease-elected, backs off when the car sleeps, never wakes it),
  nine toggleable rules with hold times, de-duplication, cooldowns and auto-resolve, alert delivery (stored, next-turn
  context, push to a bound chat via `inject_message`), `car_alerts`, `car_rules`, the `/car` slash command,
  three automation blueprints and the bundled `car-agent` skill.
- Milestone 5: Desktop page (hero card, tiles with freshness, readiness widget, alerts feed, safety-confirmed command
  buttons, audit table, settings/rules, simulator controls), status-bar chip, sidebar entry and the `dashboard/plugin_api.py`
  backend; verified by rendering in Node against real simulator JSON.
- Milestone 6: README, `docs/SAFETY.md`, `docs/ADAPTERS.md` (the doc example is executed by a test), `docs/DEMO.md`,
  `docs/STATUS.md`, catalog entry draft, full settings form in the manifest, end-to-end tests through Hermes' real
  approval pipeline, simulated-car alert labelling, misconfigured-adapter handling.

## Pass 2 (owner addendum)
- Renamed to **Pit Crew** (`pit-crew`).
- **Own history store**: trips, charging sessions and efficiency recorded from any adapter; TeslaMate becomes an optional
  import (`/car import teslamate`).
- **Direct Tesla adapter** (Teslemetry / Tessie / Fleet API via `tesla-fleet-api>=1.5,<2`, lazy import): never wakes a sleeping car
  to poll, rate-limit aware, signed commands only with a registered key.
- Docs: "Choose your connection" table, connection setup, safety/privacy updates.

## Pass 3 (catalog rule 11 compliance)
- Removed plugin-held Tesla OAuth entirely (Fleet-direct mode, refresh-token storage/rotation, token store, key-file reading).
  Teslemetry and Tessie (static token via the secret scope) remain; Tesla Fleet API works through Home Assistant's official
  integration; native Fleet-direct is planned pending a Hermes OAuth enhancement.
- Verified every credential is read only through `agent.secret_scope.get_secret`; scope errors are now logged.
- Desktop commands need a single-use, command-bound confirmation token.
- `docs/CATALOG_COMPLIANCE.md` (all 18 admission rules) + automated checks; CI with release-age quarantine.

## Pass 4 (fail-closed motion; live finding)
- `allow_unknown_motion` now defaults to **false**: unlock/trunk/frunk/vent/honk/flash are refused unless a **fresh** speed, gear,
  driving-state or plugged-in reading proves the car is parked. Stale readings count as unknown.
- HA `tesla_fleet` profile: maps `sensor.<slug>_shift_state` / `sensor.<slug>_speed` (mph) and an optional TeslaMate state entity
  (`moving_state_entity`); actionable warnings in refusals, `car_status`, `/car`, `/car discover` and the Desktop page.

## Pass 5
- TeslaMate state as a motion source is now an allowlist: only `asleep`/`online`/`charging`/`suspended`/`updating` prove parked,
  `driving` ⇒ moving, everything else (`offline`, `start`, unknown) is no vote. SAFETY notes the timestamp-on-change caveat.

## Pass 6
- Background watcher without a Hermes secret scope (`UnscopedSecretError`, matched by class name) is now its own state
  (`credential_unavailable_in_background`), not "token missing": one warning per process, plain-language fix text in `car_status`,
  `/car`, `/car discover` and the Desktop card, a read-only rate-limited single-flight turn-time alert check in a copied context,
  and automatic recovery. No environment/.env fallback.

## Pass 7
- `start_watcher` always tries the host's `ctx.spawn_task` first (works without a running loop on newer Hermes, bound to the
  plugin's profile secret scope) and only falls back to its own daemon thread if it is absent or raises. Stop is a thread-safe event.

## Pass 8 — pre-demo fix pass
- **Approvals (rule 12).** The Desktop and web pages never approve: approval-class commands become a preview plus *Ask Hermes*
  (`host.composer.insertText`; copy helper on the web page). `POST /command` refuses them while armed (`ask_hermes`); `/preflight`
  no longer issues tokens for them. Alerts carry a routed one-click fix (`action`).
- **Web dashboard page.** `dashboard/dist/index.js` + manifest `entry`/`tab`: a read-mostly Pit Crew page; no more 404 on every load.
- **Freshness.** HA readings are confirmed by the newest stamp among the car's entities while it is online (R1, with the real
  fixture); the trusted-zone check needs a fresh location (R2); watcher rules skip stale readings (R16); alert copy states the time
  it was seen so it stays true later. Desktop: humane ages, "last known" instead of dimming, "car last reported".
- **Readiness & history.** Time parser (dates, ordinals, `7.30am`, `tonight 8`), idle-charger voltage, scheduled charging
  (`charge_schedule_start`), no fake trips from sleep drain, no circular Wh/mi for range-derived drives.
- **Hardening.** No redirects with credentials, `HTTPException` handled, serialized live commands, transactional watcher lease,
  TeslaMate units + gear allowlist, partial door lists read as unknown, DST-safe default zone, `battery_kwh` guard.
- **Docs/catalog.** README quickstart fixed (`park_home`, `plug_in`, restart), "Desktop on another machine", `/car desktop-install`,
  one-line catalog description + `docs/PR_BODY.md`, 85 s `docs/DEMO.md`, refreshed STATUS/SAFETY/COMPLIANCE; `fleet` removed from `tesla_mode`.
