## New catalog entry: pit-crew 0.2.0

* **Repo:** https://github.com/trevornk/pit-crew
* **Pinned sha:** `<PINNED_SHA>`
* **requires_hermes:** `>=0.23.0`
* **Tier / category / platforms / maintainer:** community / automation / all (`platforms: []`) / trevornk
* **Image and screenshots:** pinned to `<PINNED_SHA>` under `docs/screenshots/` (banner.png, pit-crew-page.png, live-confirmed.png, alert.png)
* **Demo video:** <VIDEO_URL>

### Add Pit Crew (`pit-crew`) — proactive, safety-gated car agent

Live car state, a "will I be ready for tomorrow?" planner, a background watcher with twelve toggleable rules (including low tire pressure, fast air loss and the car's own tire warning), and commands
that only run through safety checks enforced in code. Works with Home Assistant (any brand with an HA integration; the
`tesla_fleet` mapping is built and tested against sanitized recordings of a real car, and its commands have been run on that car), direct Teslemetry/Tessie, read-only TeslaMate, or a built-in **simulated
car** so a reviewer can try everything with no car. Ships a Desktop page, a web-dashboard page, a `/car` command, three cron
blueprints and a bundled skill. Try it: `hermes plugins install trevornk/pit-crew`, enable, restart, then `/car` and the
README quickstart.

### Disclosure (catalog rule 13)

* **Network calls** — only to services the user configures: their Home Assistant (REST `/api/states`, `/api/services`), their
  TeslaMateApi (**GET endpoints only**; its command endpoints are never called), and, for the direct Tesla adapter, Teslemetry
  or Tessie through the `tesla-fleet-api` package. Redirects are never followed, so credentials cannot be forwarded elsewhere.
  **No telemetry**, nothing else.
* **Credentials** — the HA, TeslaMateApi and Teslemetry/Tessie tokens are declared `config_schema` secrets read only through
  Hermes' secret scope (`agent.secret_scope.get_secret`). **No stored credential**: the plugin holds no OAuth tokens, does no
  refresh/rotation and reads no credential files, `.env`, keychains or `os.environ`.
* **Reads outside its own data** — only a custom Home Assistant profile file if the user points a setting at one (JSON/YAML
  mapping, not credentials).
* **Background process** — one supervised watcher (host `spawn_task`, or its own daemon thread as a fallback) polls the car
  every 2 min (15 min while it sleeps) and **never wakes the car**. It only reads; it has no command path.
* **Storage** — a SQLite file in the plugin's data dir: audit log, alerts, recorded trips/charging sessions (zone names,
  distance, energy, temperature; **no coordinates, no VIN**), simulator state.
* **Risk** — when live this can unlock and open a real car. By default (`live_mode: on_demand`) commands are practice only
  (nothing sent) until the user goes live, which lasts 1 h by default (15 min - 72 h with `/car live N`) and then switches back;
  `live_mode: always` sends without going live first (`/car stop` pauses). The agent can never go live itself.
  Unlock/trunk/frunk/window-vent/Sentry-off/honk/flash go through **Hermes' own approval gate** and follow the user's Hermes
  approval setting (asked when approvals are on; not asked when off/yolo); the Advanced `require_human_for` setting forces a
  recorded human *yes* for listed commands even then and refuses when no human answer is available. Unattended runs fail closed.
  Every command is policy-checked (not moving, fresh state, trusted zone, rate limit, bounded values), read back, and audited.
* **Optional alert push** — alerts are sent to a Hermes delivery target (`alert_target` / `/car notify`, same as a cron job's `deliver`)
  through Hermes' own cross-platform send: a plain message, no agent turn. With no target, alerts are stored and mentioned on the next turn.
* **Dependency** — one bounded package, `tesla-fleet-api>=1.5,<2` (Apache-2.0), imported lazily and used only by the direct
  Tesla adapter; HA / TeslaMate / simulator users never import it.

### Notes for reviewers

* **Rule 12 (approvals).** The plugin's own Desktop and web-dashboard pages cannot approve anything. For approval-class
  commands they show a preview of the policy checks and an *Ask Hermes* button that drafts the request in the chat composer
  (`host.composer.insertText`); the plugin API `/command` refuses those commands while live (`status: ask_hermes`) and a
  click can never stand in for Hermes' prompt. Tests: `tests/test_dashboard_api.py`.
* **Rule 9 (`sys.modules`).** `dashboard/plugin_api.py` contains a single `sys.modules[...] = ...` in the *fallback* path that
  loads this plugin's **own** package under the private name `pit_crew_desktop_core` when the host has not already loaded it
  (so its relative imports resolve). It never registers, replaces or patches a Hermes core module. Under
  `plugins.isolation: host` the already-loaded package is found first and the fallback is not used; it is kept for hosts that
  mount the API without registering the plugin. `hermes plugins validate` reports "no core override" with no warnings.
* **Rule 10.** CI (`.github/workflows/ci.yml`) resolves *test tooling* with a release-age quarantine; the plugin's own
  dependency is the single bounded `tesla-fleet-api>=1.5,<2`, checked against Hermes' pins with `pip install --dry-run -c`.
* **Known limits** (also in the entry's `known_issues`): on the owner's real Model Y through Home Assistant (2026-11-09) twelve
  no-approval commands and all seven approval-class commands (unlock, trunk, frunk, vent, Sentry off, honk, flash) were sent and
  the car did each; the approval ones were sent with the owner at the car and Hermes approvals off, so Hermes did not prompt (as
  designed; the audit records this). On 2026-11-10 the trunk was sent again with `live_mode: always`. The Desktop page and its
  status-bar toggle (Desktop connected to a remote Hermes), the web-dashboard tab and an alert push to a phone were used live.
  Not yet exercised live: Hermes' approval prompt with approvals on, on a real gateway surface (chat-app buttons); it is covered
  in CI against Hermes v0.23.0's real approval pipeline with a scripted human. The BMW and Kia/Hyundai HA profiles are verified
  against the integrations' source and fixtures, not run on a real car (BMW's integration was removed from HA core in March
  2026; the Kia profile targets the kia_uvo HACS custom integration); the direct Tesla adapter (Teslemetry/Tessie) is tested
  against mocks only.
* 920 tests (CI runs Python 3.11 and 3.12, installs Hermes Agent v0.23.0 and runs them all, none skipped), `hermes plugins
  validate .` clean, no binaries, scanner `safe`.
