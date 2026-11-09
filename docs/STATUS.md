# Status

_Last updated: pre-submission gap-closing pass (version 0.2.0). Hackathon submission deadline: Wed Nov 11 2026 4 PM US/Central._

## What is verified now
* **Live, read-only:** the Home Assistant `tesla_fleet` connection has read the status of the owner's **real Model Y** (state,
  charging, lock, climate, place). The sanitized recordings of it are the test fixtures. Its Odometer showed "unavailable" because
  Home Assistant ships that entity disabled; the mapping itself matches HA core's source.
* **Not live-tested yet (the owner will do this separately): commands.** Lock/climate/charge from `/car live`, the unlock approval
  round-trip on a real surface (Telegram/Discord/Desktop), chat pushes, and both pages inside the real apps.
* **In CI on every push** (`.github/workflows/ci.yml`): all 593 tests with none skipped: Python 3.12 test tools resolved with a
  3-day release-age quarantine, Node 22 with pinned `react`/`react-dom`/`jsdom` (`tests/node`, `npm ci`), and **Hermes Agent v0.23.0
  installed from its own `uv.lock` (Python 3.14)** so the real `PluginManager` load and the real approval pipeline run; then
  `hermes plugins validate .`. A job step fails the build if any test was skipped. Locally the Node and Hermes tests skip with a
  clear reason when those are absent (point `HERMES_NODE_MODULES` / `HERMES_PY` at them).
* **Source-verified, not run on a car:** `kia_hyundai_connect` (kia_uvo v4 HACS integration) and `bmw_connected_drive` (HA's last
  version of an integration that was removed in March 2026), against entity names, units and test data; see
  `tests/fixtures/integrations/README.md`.
* **New in 0.2.0:** tire pressure end to end (fields, adapters, simulator, `/car`, both pages, two alerts) and Home Assistant
  "enable this disabled entity" guidance; a full bug audit (list in `CHANGELOG.md`, each fix has a regression test).

## Known limits (not fixed, by design or cost)
* Cross-process races: two processes sharing the simulator or the recorder's last-snapshot row can overwrite each other (the
  simulator is demo-only; the recorder tolerates a lost snapshot).
* `alerts` and `audit` rows are never pruned (small).
* A rule's cooldown restarts when its condition clears (a flapping door can alert once per episode, by design).
* A model can pass `wake_ok` / `override_zone` itself on comfort/charging commands (the schema says "only after the user agreed");
  approval-class commands still need the human prompt. The pages cannot set either flag.
* The Desktop/web tiles label a reading "confirmed" when it carries no timestamp (no adapter does this today).
* Fast-air-loss can, rarely, read a hot tire cooling right after a long highway drive as a leak; readings taken while moving are ignored.

## Pass 9 (security + privacy + UX + README) — done
* **Security (all 12 findings):** watcher lease heartbeat and decoupled atomic push (gateway-owned delivery), crash-proof poll loop,
  Hermes-only approval evidence, atomic cross-process rate limit, DST/time-parse fixes, NaN/userinfo/bad-body hardening, alert text
  sanitising, honest `unknown_outcome`, 0600 DB, adapter cleanup. Tests: `tests/test_pass9_security.py`.
* **Privacy (HEAD only):** fixtures and test paths scrubbed, `.gitignore` expanded; **the git history still needs the owner's
  squash/recreate step** (see `PASS9-REPORT.md`).
* **UX:** Practice/Live vocabulary everywhere, Go live in header and dialog, quick actions, Connect-your-car card, grouped
  settings, `/car ready|lock|warm|live|practice`, readable policy/audit/alerts, web page parity. Tests: `tests/test_pass9_ux.py`.
* **README** rewritten as a landing page with an honest verified/not-verified section. Screenshots are still the owner's to add.
* Still true: nothing has run against a live Home Assistant / Tesla / Teslemetry / Tessie / car, and neither page has been opened in
  the real app (Node SSR and jsdom only).


## Pass 8 (pre-demo fix pass, from the judge's review) — done
* **Reliability:** R1 HA freshness (readings confirmed while the car is online), R2 zone freshness, R7 time parser, R8/R9 no fake
  trips / no circular efficiency, R10 voltage-aware charging rate, R11 scheduled charging (`charge_schedule_start`), R12 TeslaMate
  units, R13 DST-safe zone, R14 partial door lists = unknown, R15 TeslaMate gear allowlist, R16 stale readings don't alert, R17
  no credentialed redirects, R18 `battery_kwh` guard, R5 serialized live commands, R6 transactional lease. Regression tests in
  `tests/test_pass8_freshness.py` (R1 uses the real live-sanitized fixture) and next to each module.
* **Catalog:** the plugin's own pages can't approve anything (rule 12): approval-class commands are preview + *Ask Hermes*;
  `/command` refuses them while armed. One-line catalog description, disclosure moved to `docs/PR_BODY.md`, `image:`/`screenshots:`
  placeholders pinned to `TODO-SHA`; `fleet` removed from `tesla_mode`.
* **UI:** a real web-dashboard page (no more 404), Desktop page polish (humane ages, "last known", command applicability reasons,
  alert fix buttons, error states), README quickstart that runs as written, "Desktop on another machine" steps, 85 s demo list.
* **Owner steps left:** see `PASS8-REPORT.md` (screenshots, SHA, public repo, `validate --install-deps`, live checks).

## Verification (pass 8)
* `pytest tests` → see the report for the final count (all passing); `hermes plugins validate .` passes.
* The web page runs in jsdom, the Desktop page in Node SSR, both against real simulator JSON; neither was opened in the real app.

## Pass 4 (fail-closed motion) — done
* `allow_unknown_motion` default **false** everywhere; refusal text says exactly how to fix it; motion must be **fresh**
  (`VehicleState.motion(now, max_age_s)`). tesla_fleet profile maps Shift state + Speed and optional `moving_state_entity`
  (TeslaMate state); warnings in discover, `/car`, `car_status`, Desktop *Needs attention*. Tests: `tests/test_motion_failclosed.py`.
* To use unlock/trunk on the owner's car: enable HA's *Shift state* and *Speed* entities (or rely on being plugged in / TeslaMate state).

## Pass 3 (catalog rule 11 compliance) — done
* **Plugin-held Tesla OAuth removed** (no Fleet-direct mode, no refresh-token store/rotation, no key-file reads); Teslemetry + Tessie
  only (static token). Docs: Fleet API row now "through Home Assistant's official Tesla Fleet integration (tested on the owner's
  car)"; native Fleet-direct planned pending a Hermes OAuth enhancement (audience + fixed redirect port).
* **Secrets audit**: all secrets read only via `agent.secret_scope.get_secret` (single route, pinned by tests).
* **`docs/CATALOG_COMPLIANCE.md`**: one-line verdict for each of the 18 admission rules; static checks in
  `tests/test_catalog_compliance.py`. Other fixes from the audit: Desktop single-use confirm tokens, CI release-age quarantine
  workflow, disclosure of the custom-profile file read, scope-error logging.
* Verified: 'tesla-fleet-api>=1.5,<2' resolves under Hermes' installed pins (`pip install --dry-run -c <pins>` in a scratch venv).

### Pass 3 — open
* `validate --install-deps` (catalog CI) still not run locally (it installs into the Hermes venv).
* Rule 12 review point: Desktop confirmation is a plugin-UI click, not Hermes' prompt (fallback: preview-only for risky commands).
* The pass-2 notes below about Fleet mode / rule 11 are **superseded**.

## Pass 2 (owner addendum) — done (Fleet-direct parts superseded by pass 3)
* **Rename** to Pit Crew / `pit-crew` everywhere (package, manifest, env vars `PIT_CREW_*`, settings path, Desktop/dashboard ids, docs,
  `catalog/pit-crew.yaml`); `origin` → `https://github.com/trevornk/pit-crew.git`. `/car` and `car_*` names kept.
* **Own history store** (`history_store.py`): interval classifier → drives/charge sessions in the plugin DB from ANY adapter;
  `history: own` is the default; `CombinedHistory` keeps the simulator's synthetic past; `/car import teslamate [days]` (and
  `POST /import`) backfills idempotently. TeslaMate is optional everywhere.
* **Direct Tesla adapter** (`adapters/tesla_direct.py`): Teslemetry / Tessie (`tesla-fleet-api>=1.5,<2`, declared in
  `python_dependencies`, lazy import), never wakes to poll, rate-limit aware. (Fleet mode: removed in pass 3.)
* Docs: connection-choice table, per-connection setup, ADAPTERS/SAFETY/DECISIONS/catalog updates.

### Pass 2 — not verified / open
* **No live Tesla/Teslemetry/Tessie account was available**: the direct adapter is tested against fakes of the library and of the
  Fleet-API JSON shape (a synthetic fixture, not a recording). The real library was inspected in a scratch dir (not installed
  anywhere that matters) to confirm method names/signatures for 1.5.0 and 1.21; behaviour against the live services is unverified.
* ~~Catalog rule 11 tension for Fleet mode~~ — resolved in pass 3 by removing it.
* `python_dependencies` pulls `bleak`/`protobuf`/`cryptography` etc. (library requirements). `hermes plugins validate --install-deps`
  (what catalog CI runs) was NOT run here because it would install them into the Hermes venv; plain `validate` passes. `hermes plugins doctor` warns "declared python_dependencies not installed" in this sandbox for the same reason — expected; the plugin still loads (lazy import) and the warning disappears after a normal `hermes plugins install`.
* History recorder heuristics are tested with scripted state sequences, not against months of real data.

## Done in pass 1 (all six milestones)

1. **Skeleton** — manifest v2, normalized `VehicleState` (per-field freshness, `available=False`), command vocabulary with
   risk classes, adapter contract + registry (+ `pit_crew.adapters` entry point), seeded simulator, `car_status`.
2. **Adapters/history** — Home Assistant REST adapter + entity-mapping profiles (tested `tesla_fleet`, untested BMW and
   Kia/Hyundai starters) + auto-discovery; read-only TeslaMateApi adapter (GET only); `car_history`; `car_readiness`.
3. **Safety** — dry-run default, timed arming, policy engine, human approval via Hermes' pipeline with positive
   confirmation, fail-closed when unattended, rate limit + de-dupe, read-back, SQLite audit log, redaction, `car_command`.
4. **Proactive** — watcher (task/thread, lease, sleep-aware, never wakes), nine rules, delivery (stored + next-turn context +
   push via `inject_message` to a chat bound with `/car notify here`), `car_alerts`, `car_rules`, `/car`, 3 blueprints, skill.
5. **Desktop** — page + chip + nav + backend (`dashboard/plugin_api.py`); renders with the simulator.
6. **Docs** — README, SAFETY, ADAPTERS, DECISIONS, DEMO, CHANGELOG, `catalog/pit-crew.yaml` draft.

## Verification actually run

* `python -m pytest tests` (venv outside the repo) → see the pass-8 count above (no network; includes the real-Hermes, Node and jsdom tests).
* Real-Hermes tests (skip if the Hermes venv is absent): the plugin loads through the real `PluginManager` in an isolated
  `HERMES_HOME`; the **real approval pipeline** is exercised end to end with a scripted human (approve once / always /
  deny / timeout / no human / YOLO).
* Desktop page rendered in Node (React server rendering, SDK stubbed) against real simulator JSON.
* `hermes plugins validate .` → passes (no `dangerous`, no warnings besides the informational isolation notes);
  `hermes plugins doctor . --ci` → OK.

## Not done / not verified (be honest)

* **Never run against the owner's live Home Assistant / TeslaMate / car** (hard boundary of this build). Mapping and
  service shapes were verified only against the sanitized recordings and fake servers.
* **Real gateway surfaces** — approval via Telegram/Discord buttons, `inject_message` push to a real chat, and
  `/car arm`'s `ask_choice` on each surface were not exercised (no live gateway in the sandbox). Unit-tested with stubs; the
  CLI approval path is tested for real. If a surface doesn't fire `post_approval_response`, strict mode will refuse risky
  commands — set `approval_confirmation: hermes_gate` as the documented escape hatch, and tell us.
* **Desktop / web dashboard in the real apps** — rendered with an SDK stub (Desktop) and jsdom (web), not inside the real apps; visual
  polish and SDK prop details (e.g. `Button` ref/autoFocus, `Dialog` focus, `host.composer` while the page replaces the chat view) need an eyeball pass. Screenshots are the owner's to supply (`docs/screenshots/README.md`).
* `plugins.isolation: host` mode for the dashboard backend is covered by the shared-DB design but not exercised.
* BMW / Kia-Hyundai profiles are verified against source and fixtures, not run on a car (labeled).

## Next steps (for the live pass)

1. Install into the live profile (`hermes plugins install` from the repo, or symlink for dev), enable, restart the gateway.
   Install from a git clone (the dev venv lives outside the repo at `~/.hermes/cache/scratch/pit-crew-venv`).
2. Configure HA: URL + token, `/car discover`, set `ha_profile=tesla_fleet` and the slug; keep disarmed; compare `/car` with
   the Tesla app; then `/car arm 1`, try `lock` → `climate_on` → (approval) `unlock` at home.
3. `/car notify here` in the home channel + `allow_gateway_injection: true`; trigger via the simulator first.
4. Take screenshots (`docs/screenshots/`), fill `catalog/pit-crew.yaml` `sha`/`image`/`screenshots`, open the catalog PR
   from the final public commit. Rename (if desired) = search-replace in `constants.py`, `plugin.yaml`,
   `dashboard/manifest.json`, `desktop/plugin.js` (+ the README/doc strings).
5. Make the repo public at submission (the catalog CI clones it).

## Known issues / ideas

* HA adapter reads all of `/api/states` per poll (fine at 2 min; large HA instances could use a template endpoint).
* Per-field freshness for TeslaMate is approximated from `state_since` when the car sleeps.
* Possible additions: tire pressure rule (TeslaMate has TPMS), departure schedule from calendar via the skill only,
  more brand profiles, YAML profile tests.
