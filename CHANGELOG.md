# Changelog

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
