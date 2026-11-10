# Safety model

Pit Crew can move real metal and unlock a real door, so safety is enforced **in code**, not in prompts.
Everything below is tested (`tests/test_safety_engine.py`, `tests/test_approval_gate.py`).

## The short version

1. **Practice mode by default.** Out of the box (`live_mode: on_demand`), commands are checked and logged but the car is
   **never contacted**. The reply says "PRACTICE RUN … the car was NOT contacted".
2. **You decide how it goes live** (**Safety · Live mode**, `live_mode`):
   * `on_demand` (default): **Go live** (the Pit Crew page) or `/car live` (asks you to confirm) lets Hermes send commands
     for a limited time (default `live_default_hours` = 1 h; `/car live N` 15 minutes to 72 hours) and then it switches itself back.
   * `always`: commands are sent for real without going live first. `/car stop` (or **Stop**) pauses it into practice mode
     until you run `/car live` (or press **Go live**) again, which resumes it with **no expiry**. Status, chip and page say
     which applies: *Live: always on* / *Paused (practice) — /car live to resume* / *Live until 4:39 PM*.
   * `/car stop` (= `/car practice`) works any time in both modes. (`/car arm` and `/car disarm` are aliases.) The old
     standing `armed: true` setting is read as `live_mode: always` when `live_mode` is not set.
   * Nothing else differs between the modes: every check below applies the same. **The agent can never go live itself:**
     no tool arms, and no tool changes these settings; only the setting, your slash command or your click.
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
| fresh state | unlock, trunk, frunk, vent, Sentry off | data older than `max_state_age_s` (default 10 min; 1 min to 1 h, cannot be switched off) |
| trusted zone | unlock, trunk, frunk | `zone_rule: trusted_zones` (default) and not at a `trusted_zones` place (default `home`) — unless the user insists once (`override_zone`), which is shown prominently in the approval prompt. `zone_rule: anywhere` drops this check |
| asleep | every command | the car is asleep (waking drains battery) — unless the user agreed (`wake_ok`), or `wake_for_commands: auto` (then a command the user asked for may wake it; status reads never do) |
| plugged in | start charging | the car is not plugged in |
| rate limit | live commands | more than `rate_limit_commands` per `rate_limit_window_s` (default 10 per 10 min; 1-60 per 1 min-1 day) |
| duplicate | live commands | the identical command was sent within `dedupe_window_s` (default 60 s; 5-600 s) |

Every refusal names the setting that would allow it, by its label in the settings form and its key, e.g. *"Refused: the car is
at 'away', away from your trusted zones (home). To allow unlock/trunk/frunk anywhere, set “Safety · Unlock/trunk/frunk zone”
(zone_rule) to 'anywhere' in Pit Crew settings."* Refusals no setting can lift say so.

### Owner-adjustable protections vs fixed invariants

| Adjustable (setting; default) | Risk when loosened |
|---|---|
| `live_mode` (`on_demand`) | `always`: anything that gets Hermes to call `car_command` sends at once; safe-direction and comfort commands need no approval. Keep Hermes approvals on (or use `require_human_for`) if other people can talk to your Hermes. |
| `zone_rule` (`trusted_zones`) | `anywhere`: unlock/trunk/frunk work wherever the car is parked. Someone near the car when it opens could take things. |
| `wake_for_commands` (`ask`) | `auto`: a requested command wakes the car (battery) without a second question. |
| `max_state_age_s` (600 s, 60-3600) | Acting on an older picture of the car. |
| `rate_limit_*`, `dedupe_window_s` | More commands in a burst; a repeated message is more likely to send twice with a short window. |
| `require_human_for` (empty) | Tightens only. |
| `allow_unknown_motion` (off) | Acting when parked-ness cannot be told. Never overrides a reading that says the car is moving. |
| Hermes' approval mode | Hermes' own setting. |

| Fixed (not a setting) | Why |
|---|---|
| Never unlock/open while moving or not provably parked | A door or trunk opening on the road hurts people; no convenience is worth it. |
| Approval-class commands never from unattended/cron/background runs | No person is there to answer; a schedule must not open a car. |
| The agent cannot arm itself or change these settings | A prompt-injected or confused model must not be able to lower its own guard. |
| Truthful results (sent vs accepted vs confirmed) | Not a protection but a promise: you decide what to do next from it. |

### The walk back to the car (mall case)

With `live_mode: always`, `zone_rule: anywhere` and `wake_for_commands: auto`, "open the trunk" as you walk out of a shop to
the parked, sleeping car is sent (after Hermes' approval step, per your Hermes setting). Parked is proven because the car
reports it is **asleep right now** (a fresh asleep reading): a Tesla cannot sleep in gear. A stale asleep reading proves
nothing, a car that is awake needs fresh gear/speed as before, and any reading of D/R/N or a speed above 1 mph — even an old
one — still refuses. Caveat: Home Assistant's Tesla Fleet *status* sensor reports "offline" the same as asleep; an offline car
cannot receive the command anyway, and a car that comes back online and moves reports gear/speed again.

### Parked must be *proven*, with fresh data

"Parked" needs at least one **fresh** (within `max_state_age_s`, default 10 min) indicator that says so: a speed near zero, a
gear of P, a `driving`-state entity that says anything but `driving`, the car being plugged in, or the car reporting that it
is asleep. Any indicator that says
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
* **Pushed alerts leave this machine for the platform you chose.** With a delivery target set, each alert's title, body and suggestion
  (a zone name like `home`/`away`, a rule, a percentage; scrubbed of VINs/tokens, never coordinates) is sent as a plain message through Hermes
  to Telegram/Discord/Matrix/etc. and is then subject to that platform's retention and who is in the chat. Pit Crew sends nothing else, no
  credentials, and no car command; a pushed alert never runs anything (acting still needs your yes in a Hermes chat, and Hermes' approvals).
  Choose a private chat or room. Leave the target empty to keep alerts local.
* VINs and Home Assistant tokens are masked in transcripts/logs (patterns registered with Hermes' redactor) and scrubbed
  from any remote error text before it is stored or shown.
* Secrets (HA, TeslaMate and Teslemetry/Tessie tokens) are declared `config_schema` secrets read **only** through Hermes' secret
  scope (`agent.secret_scope.get_secret`), never stored in config or logs. The plugin holds no OAuth tokens: nothing is
  refreshed, rotated or written, and no credential files are read. (Tesla's own Fleet API is supported through Home
  Assistant's Tesla Fleet integration, which holds those tokens itself.)
* **Direct Tesla modes never wake the car to poll.** Polling asks the cheap vehicle-state endpoint; only an online car gets a data
  call, an asleep car's last data is shown as stale, calls are rate-limited (`tesla_min_poll_s`, back-off on HTTP 429). Waking
  happens only for a command the human explicitly allowed (`wake_ok`), once, or — with `wake_for_commands: auto` — for a
  command the user asked for; never for polling. Latitude/longitude, when zones are configured, are
  converted to a zone name inside the adapter and discarded.
* **The re-read after a command never wakes the car either** (Home Assistant Tesla Fleet, `refresh_after_command`): Home Assistant's
  refresh asks Tesla whether the car is online (free) and only then fetches its data. It runs once per live command with an observable
  result, never for status reads or the watcher, and a re-read that finds the car asleep confirms nothing.
* **TeslaMate next to the command connection (Home Assistant, Teslemetry or Tessie) is read-only** (GET only: `/api/v1/cars/:id/status`, and `/api/v1/cars` once for the VIN check next to Teslemetry/Tessie; a different car is ignored). It can make a reading fresher, never a command
  possible: a merged reading still goes through every policy check, motion from either source that says moving wins, and a TeslaMate
  reading confirms a command only when it is stamped after the send.
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
