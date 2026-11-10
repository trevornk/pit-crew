# Catalog PR description (paste this into the `hermes-agent` PR that adds `plugin-catalog/pit-crew.yaml`)

> Owner checklist before opening the PR: make `trevornk/pit-crew` public; run `hermes plugins validate . --install-deps` in a
> throwaway Hermes; the screenshots are in `docs/screenshots/`; replace `TODO-SHA` in the entry and fill `sha`.

## Add Pit Crew (`pit-crew`) — proactive, safety-gated car agent

Live car state, a "will I be ready for tomorrow?" planner, a background watcher with twelve toggleable rules (including low tire pressure, fast air loss and the car's own tire warning), and commands
that only run through safety checks enforced in code. Works with Home Assistant (any brand with an HA integration; the
`tesla_fleet` mapping is built and tested against sanitized recordings of a real car; commands have not been run on a real car yet), direct Teslemetry/Tessie, read-only TeslaMate, or a built-in **simulated
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
* **Risk** — when live *and* approved this can unlock and open a real car. Commands are in practice mode (nothing sent) by default; going live is explicit,
  time-boxed (72 h cap); unlock/trunk/frunk/window-vent/Sentry-off/honk/flash need the human's recorded *yes* in **Hermes' own
  approval prompt** and fail closed when unattended; every command is policy-checked (not moving, fresh state, trusted zone,
  rate limit, bounded values), read back, and audited.
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
* **Known limits** (also in the entry's `known_issues`): read-only status has been verified on a real Model Y through Home
  Assistant, but no command has been run on a real car yet; the BMW and Kia/Hyundai HA profiles are verified against the
  integrations' source and fixtures, not run on a real car (BMW's integration was removed from HA core in March 2026; the Kia
  profile targets the kia_uvo HACS custom integration); the direct Tesla adapter is tested against mocks only; the live approval
  round-trip has been exercised on the CLI and in CI through Hermes' real approval pipeline, not yet on every gateway surface.
* 652 tests (CI installs Hermes Agent v0.23.0 and runs them all, none skipped), `hermes plugins validate .` clean, no binaries,
  scanner `safe`.
