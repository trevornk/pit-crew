# Safety model

Pit Crew can move real metal and unlock a real door, so safety is enforced **in code**, not in prompts.
Everything below is tested (`tests/test_safety_engine.py`, `tests/test_approval_gate.py`).

## The short version

1. **Practice mode by default.** Out of the box, commands are checked and logged but the car is **never contacted**. The
   reply says "PRACTICE RUN … the car was NOT contacted".
2. **You go live on purpose.** **Go live** (the Pit Crew page) or `/car live` (asks you to confirm) lets Hermes send
   commands for a limited time (default 1 h, between 15 minutes and 72 hours) and then it switches itself back.
   `/car practice` (or **Stop**) works any time. A standing "Always live" setting (Advanced, `armed: true`) exists for people
   who want it; it is not recommended. (`/car arm` and `/car disarm` are aliases.)
3. **Risky commands need a human.** Unlock, trunk, frunk, window venting, Sentry-off, honk and flash go through Hermes'
   own approval prompt — the same one used for dangerous shell commands — on whatever surface you're using
   (Desktop, CLI, Telegram…). Locking, closing windows and turning Sentry on move the car toward *safer* and need no approval.
4. **No human, no risky command.** In a scheduled / unattended run the answer is always "not sent".
5. **Every command is policy-checked:** not while moving, not on stale data, not for a sleeping car (no waking it for
   convenience), unlock/trunk/frunk only at a trusted zone, bounded temperatures / charge limits / amps, rate-limited and
   de-duplicated.
6. **We check the car actually did it.** After sending, the plugin re-reads the car until it shows the change, and tells
   you exactly one of: *confirmed at 8:02 PM*, *sent but not confirmed*, or *sent; this car can't verify it*.
7. **Everything is audited** (SQLite): who/what surface, risk class, practice vs live, policy, approval result, outcome and
   the read-back. `/car log` and the Desktop page show it.

## Command classes

| Class | Commands | Approval |
|---|---|---|
| `read` | status, history, readiness | none |
| `comfort` | climate on/off, set temp, defrost, seat heat, charge limit, start/stop charge, charge amps | none (bounded, rate-limited; may run unattended) |
| `security` – safe direction | lock, close windows, Sentry on | none |
| `security` – risky | unlock, open trunk, open frunk, vent windows, Sentry off | **human** |
| `attention` | honk, flash | **human** |

## How approval really works

A model-initiated risky command takes this path:

```
model calls car_command(unlock)
  └─ pre_tool_call hook (ours) ── policy refuses? ─► blocked, never asks you
        │ ok
        └─ returns {"action":"approve", rule_key:"pit-crew:unlock"}
  └─ Hermes' approval gate applies YOUR Hermes approval setting (prompt a person, or not)
  └─ post_approval_response hook (ours) records a person's actual answer, when there was one
  └─ car_command runs only if the hook approved this exact call and Hermes did not deny it; the ticket is single-use
```

**Pit Crew follows your Hermes approval setting; it does not second-guess it.** What Hermes does with the `approve` directive
(`tools/approval.py` `_run_approval_gate`, Hermes v0.23.0):

| Your Hermes | What happens | Audit says |
|---|---|---|
| `approvals.mode: off`, or yolo | Hermes approves without asking | approved by Hermes (no prompt) |
| `manual` | Hermes asks a person | you approved / declined |
| `smart` | Hermes **asks a person** too. Hermes does not route plugin approvals to its guardian (`_human_decision` is called without `smart=`; only shell-command checks use it) | you approved / declined |
| key already allowed (session / always) | Hermes approves without asking | approved by Hermes (no prompt) |
| scheduled (cron) run | **Pit Crew blocks it**, even with `approvals.cron_mode: approve` (stricter than Hermes on purpose) | no one to ask |
| any other run with nobody to ask | Hermes blocks it (fail closed) | n/a |

The result then says plainly: "Sent without asking: your Hermes approvals didn't require a prompt (approvals off, yolo, or
already allowed this session)." If you want a person every time regardless, put the command under *Advanced · Always ask a
person for these* (`require_human_for`, config only). Those commands use a one-time rule key (so "always" can never skip them) and
the tool refuses unless a person's *yes* was recorded; with approvals off or yolo they are refused with a message that says why.

Design points:

* **Works with Hermes Tool Search.** When Hermes defers plugin tools, the model calls `car_command` through the bridge tool
  `tool_call`. Hermes v0.23.0 unwraps that call **before** `pre_tool_call` runs (`agent/tool_executor.py`
  `_unwrap_tool_search_call`), so the hook normally sees `car_command` itself and decides as for a direct call; Hermes itself also
  refuses multi-entry local batches. The gate's own handling of the raw `tool_call` name is defence in depth for the case where
  that unwrap fails: it looks inside the bridge call and, if it cannot read it but it names `car_command`, blocks. A `tool_call` batch
  with an approval-class `car_command` is blocked ("send approval-needed car commands on their own, one per tool_call").
* **Stable rule key.** Normal commands use `pit-crew:<command>`, so Hermes' once / session / always mean what Hermes says (choosing
  "session" or "always" really skips later prompts for that command, and nothing dead accumulates in your config). Only commands you
  put under `require_human_for` carry a one-time nonce.
* **Tickets are bound** to the exact command + arguments (and the task), expire after 10 minutes, and are consumed on use.
* **The model cannot forge approval:** there is no argument that grants it; unknown arguments are rejected.
* **Fail closed:** if the gate itself errors, or the run is unattended, an approval-class command is not sent.
* **The plugin's own pages never approve.** The Desktop page and the web-dashboard page show the policy checks for an
  approval-class command and an *Ask Hermes* button that drafts the request in the chat (Desktop: `host.composer`; web: a
  copy-the-sentence helper). The backend's `/command` refuses those commands while live (`status: ask_hermes`); in practice mode it
  only logs a preview. Lock, close windows, Sentry on, climate and charging still run from the page behind its confirm dialog
  (Cancel is the default) and are audited as surface `desktop`.

## Policy checks (run in practice mode too)

| Check | Applies to | Refuses when |
|---|---|---|
| bounds | temps, charge limit, amps | outside 59–82 °F, 50–100 %, 1–charger max A |
| parked | unlock, trunk, frunk, vent, honk, flash | the car is moving — **or its parked-ness cannot be proven** (fail closed; see below) |
| fresh state | unlock, trunk, frunk, vent, Sentry off | data older than `max_state_age_s` (default 10 min) |
| trusted zone | unlock, trunk, frunk | not at a `trusted_zones` place (default `home`) — unless the user insists (`override_zone`), which is shown prominently in the approval prompt |
| asleep | every command | the car is asleep (waking drains battery) — unless the user agreed (`wake_ok`) |
| plugged in | start charging | the car is not plugged in |
| rate limit | live commands | more than N per window (default 10 per 10 min) |
| duplicate | live commands | the identical command was sent within 60 s |

### Parked must be *proven*, with fresh data

"Parked" needs at least one **fresh** (within `max_state_age_s`, default 10 min) indicator that says so: a speed near zero, a
gear of P, a `driving`-state entity that says anything but `driving`, or the car being plugged in. Any indicator that says
"moving" refuses the command. A stale reading proves nothing, and **if nothing proves it, the command is refused** —
`allow_unknown_motion` defaults to **false** (a live test found that the default used to fail open for a Home Assistant
`tesla_fleet` car, because HA ships its *Shift state* and *Speed* entities disabled).

The refusal says exactly how to fix it, for example: *"cannot confirm the car is parked … In Home Assistant → Settings → Devices &
services → Tesla Fleet → (your car) → disabled entities, enable 'Shift state' and 'Speed'."* The same warning appears in `/car`,
`car_status`, `/car discover` and the Desktop page's *Needs attention* card. Another motion source can be mapped with the
profile/override key `moving_state_entity` (for instance TeslaMate's MQTT entity `sensor.teslamate_<car>_state`: `driving` ⇒
moving; only the explicit states `asleep`, `online`, `charging`, `suspended` and `updating` count as parked, and only while fresh —
`offline` (the car may be driving through a dead zone), `start` and any unknown value are *no vote*). **Caveat:** TeslaMate's state entity only refreshes its timestamp when the state *changes*, so a long-parked car's reading
becomes "stale" and stops proving anything. For reliable unlock/trunk away from home (where being plugged in is no help), enable
Home Assistant's *Shift state* and *Speed* entities — they refresh on every poll. Setting `allow_unknown_motion: true` re-enables the old behaviour
(allowed with a warning) for people who knowingly accept it.

**Cloud-cached cars (Kia/Hyundai via `kia_uvo`).** That integration polls a cached cloud copy and only wakes the car about once a
day, and Home Assistant stamps each write as "fresh". So a gear of P read from it can be hours old and still look new. The profile
therefore takes the data time of the gear, plug, lock and location from the integration's *Last Updated At* sensor, and the gear
counts as a parked indicator **only if that sensor is readable** (otherwise the gear is treated as unavailable). Older than
`max_state_age_s` proves nothing, as for any stale reading. Plugged in with fresh data still proves parked.

## Privacy

* **No raw coordinates anywhere.** The state model has no latitude/longitude field; location is only a *zone*
  (`home` / `away` / a named zone). Tool output, logs, alerts and the audit log therefore cannot contain coordinates.
* VINs and Home Assistant tokens are masked in transcripts/logs (patterns registered with Hermes' redactor) and scrubbed
  from any remote error text before it is stored or shown.
* Secrets (HA, TeslaMate and Teslemetry/Tessie tokens) are declared `config_schema` secrets read **only** through Hermes' secret
  scope (`agent.secret_scope.get_secret`), never stored in config or logs. The plugin holds no OAuth tokens: nothing is
  refreshed, rotated or written, and no credential files are read. (Tesla's own Fleet API is supported through Home
  Assistant's Tesla Fleet integration, which holds those tokens itself.)
* **Direct Tesla modes never wake the car to poll.** Polling asks the cheap vehicle-state endpoint; only an online car gets a data
  call, an asleep car's last data is shown as stale, calls are rate-limited (`tesla_min_poll_s`, back-off on HTTP 429). Waking
  happens only for a command the human explicitly allowed (`wake_ok`), once. Latitude/longitude, when zones are configured, are
  converted to a zone name inside the adapter and discarded.
* Page-initiated commands are audited as surface `desktop`. (Pass 8: the old single-use `/preflight` tokens are no longer a way to
  approve anything — approval-class commands are refused from the page.)
* **Freshness is per reading (pass 8).** Home Assistant timestamps an entity with when its value last *changed*, so while the car
  is online only the slow-changing readings (lock, doors, windows, trunk, Sentry...) are confirmed by the car's live readings (never charging, motion or location); every other reading keeps its own `last_reported` age, and an asleep/offline car keeps
  per-entity stamps. A trusted-zone command also needs the *location* reading itself to be fresh, and watcher rules skip readings
  older than `stale_after_s` instead of raising alerts from them.
* Pit Crew's own history stores zone names, distances, energy and temperatures — never coordinates.

## Pass 9 additions

* **Go live from the command dialog is still a confirmation.** The dialog's primary button ("Go live for 1 h & send")
  turns on live mode and sends one safe-direction command; Cancel keeps focus and Enter never confirms. Approval-class commands
  are **not** sent by it: "Go live & ask Hermes" only switches the mode and drafts the request, and Hermes' approval prompt
  still decides. Where a UX suggestion and safety conflicted, safety won: there is no one-click unlock.
* **The engine only accepts Hermes' approval as approval.** Evidence of any other kind (the old, removed "UI click" path) is
  refused for approval-class commands.
* **The model's stated reason is labelled "unverified"** in the approval prompt, flattened to one line and shortened.
* **Atomic rate limit and duplicate check** across processes (the page's backend and the gateway are separate processes on
  one database): the check and the audit row are one transaction.
* **Honest outcomes:** a timeout or a half-finished multi-step command is reported as *unclear whether it was sent* (it is
  still counted against the rate limit) instead of a plain failure.
* **Session / always are real.** Choose "once" if you want to be asked again; "session" or "always" on an unlock prompt allows
  unlock without asking later (Hermes' own rule), unless you listed unlock under *Always ask a person for these*.
* **`require_human_for`** (Advanced, empty by default) is config-only: no tool can change it. An old explicit
  `approval_confirmation: strict` migrates to unlock, open_trunk, open_frunk, vent_windows and sentry_off; an unset or
  `hermes_gate` value means empty.
* **Don't expose the Hermes dashboard to the internet.** Its plugin routes can switch live mode and send the
  non-approval commands (lock, close windows, climate, charging). They sit behind the dashboard session token, but binding the
  dashboard to `0.0.0.0` on an untrusted network widens who can try. Unlock, trunk and the other approval-class commands cannot
  be sent from it in any case.
* **Database files are owner-only (0600).** Plain `http://` to Home Assistant, and turning off certificate checks, now produce
  a visible warning, because the access token travels over that link.

## What this does *not* protect against

* Someone who can already edit your Hermes config or Home Assistant can do what they like — the plugin guards the agent,
  not the host.
* A compromised Home Assistant token can control the car without going through this plugin. Use a dedicated HA user.
* The simulator and the practice-mode default exist precisely so you can try everything with zero risk first.

## Troubleshooting: "Background alerts need a Hermes version where plugin background tasks can read plugin secrets"

With `gateway.multiplex_profiles: true`, a plugin's background task (the alert watcher) runs outside any chat turn and has no
profile secret scope, so Hermes refuses `get_secret()` there (`UnscopedSecretError`). **Your token is fine** — Pit Crew tells this
apart from "secret not set" and never falls back to the environment or `.env` files (catalog rule 11). While in this state
(`credential_unavailable_in_background`): the watcher logs one warning per process and pauses; the message above appears in
`car_status`, `/car`, `/car discover` and the Desktop *Needs attention* card; and during chats the `pre_llm_call` hook starts a
read-only alert check (at most one at a time, at most once per poll interval, on a worker thread carrying a copy of the turn's
context so the secret scope travels). That check never blocks the turn and can never send a vehicle command. When a Hermes that
lets background tasks read plugin secrets is installed, the next successful background poll clears the state automatically.
