# Demo — the 85-second shot list

Screen: Hermes Desktop with the **Pit Crew** page (sidebar entry "Pit Crew") beside the chat, captions burned in.
For recording, set **Safety · Live mode** to `always` (or run `/car live` on the default `on_demand`, which is timed). Use the simulator for everything except the one real-car shot. Hide zone names and the HA URL
(there is no VIN anywhere). One surface per beat. Pre-check that the push really arrives in your chat app (Telegram, Matrix, Discord…) with `/car notify here`, then `/car notify test`, on the exact surface you record.

| t | Shot | Caption / what it proves |
|---|---|---|
| 0–6 s | Title card **"Pit Crew — a car agent that watches, plans, and acts within your Hermes approval settings."** + 2 s of the real car in the driveway | the pitch |
| 6–16 s | Type `/car` → instant card. Ask *"Will I be ready for my 8:30 dentist appointment tomorrow?"* → the agent reads the calendar, calls `car_readiness`: "Not ready: the limit is 50%, raise to 70%?" | usefulness · more than a skill |
| 16–24 s | "Yes" → `charge_limit` (comfort, no prompt) → "the car confirmed it". The readiness card turns green | reliability (read-back) |
| 24–40 s | **Proactive.** Cut to the phone (your chat app). The watcher pushes *"Your car is unlocked and away from home — want me to lock it?"* with nobody typing. Reply "yes" → "Locked — confirmed". Caption: *"background watcher; nobody asked."* (Simulator: page → **Leave it unlocked**, **Drive 20 mi away**, **Fast-forward 30 min**; caption "simulated car".) | creativity · more than a skill |
| 40–58 s | **Safety.** *"Unlock the car."* → Hermes' real approval prompt with the policy lines → approve → confirmed. Then *"open the trunk"* while the sim is "away" → refused, with the reason. Then a scheduled run trying unlock → "not sent: no human". Quick cuts. Show the page's **Ask Hermes** button doing the same hand-off | reliability (the heaviest weight) |
| 58–66 s | The **History of commands** on the page (dry-run vs live, approval, read-back). Caption *"practice mode by default · you choose timed or always-on live · every command audited & read back."* | polish · reliability |
| 66–74 s | **Where the data comes from.** `car_status` → *field sources*: Home Assistant + TeslaMate, each field read from the newest source (real car with location masked; optional, skip if you only use one source). Caption *"each reading comes from the freshest source."* | reliability · transparency |
| 74–80 s | "No car?" Page → **Try it: simulator controls** → *Drive 20 mi away* → *Fast-forward 30 min* → an alert with a **Lock it** button appears. Caption *"Home Assistant (any brand) · Tesla · TeslaMate · simulator."* | usefulness for judges |
| 80–85 s | End card: `hermes plugins install trevornk/pit-crew` + repo URL | — |

Rules of thumb
* This sequence (`/car sim drive 25 away` → `park_home` → `plug_in` → `unlock`) is the dependable way to show the
  approval prompt on the simulator; right after `drive … away` an unlock is (correctly) refused.
* On the real car, unlock needs a fresh motion source (enable HA's *Shift state* + *Speed*, or be plugged in). If in doubt show
  unlock on the simulator and use the real car only for lock and charge-limit.
* The web dashboard (`hermes dashboard` → **Pit Crew** tab) shows the same page; use it if the recording surface is a browser.
