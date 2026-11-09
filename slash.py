"""The ``/car`` slash command: instant status card and controls, no model turn."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from .adapters.base import AdapterError, SecretScopeUnavailable
from .brain import tires
from .brain.models import describe_state
from .brain.timeparse import fmt_clock
from .constants import DISPLAY_NAME, VERSION
from .service import get_service

logger = logging.getLogger(__name__)


def _num(text: str) -> bool:
    """An ASCII number like ``72`` or ``70.5`` (``str.isdigit`` also accepts '²' and other digits float() rejects)."""
    return text.isascii() and text.replace(".", "", 1).isdigit()


def _count(text: str, default: int, cap: int = 1000) -> int:
    return min(int(text), cap) if text.isascii() and text.isdigit() else default

from .watcher.rules import RULE_LABELS, RULES_BY_ID  # noqa: E402

HELP = """\
Everyday
  /car                    status card
  /car ready [when] [mi]  will I be ready?  e.g. /car ready tomorrow 7:30 AM 40
  /car lock               lock the car          /car warm [°F]   warm it up (default 70°F)
  /car alerts [ack [id]]  open alerts (ack with no id acknowledges all)
  ...or just ask in plain language: "lock my car", "warm it up", "will I make it tomorrow?"
Safety
  /car live [hours]       go live: Hermes can send commands (asks first; stops by itself, 1 h default)
  /car practice           back to practice mode: commands are checked and logged, nothing is sent
  /car log [n]            recent command attempts
Setup
  /car discover           find your car in Home Assistant
  /car notify here|off|status|test   push alerts to THIS chat (test = send a test alert)
  /car import teslamate [days]  backfill trips/charges from TeslaMate (optional)
  /car rules              alert rules and thresholds
  /car desktop-install    get the Pit Crew page into a Desktop app on another machine
Simulated car only: /car sim  (prints its own list)
(`/car arm` and `/car disarm` still work: they are the same as live and practice.)"""


DESKTOP_INSTALL = """\
Pit Crew page in Hermes Desktop
- Desktop on the SAME machine as Hermes: nothing to do. `hermes plugins enable pit-crew`, restart, open "Pit Crew" in the sidebar.
- Desktop on ANOTHER machine (connected to this Hermes remotely): the app loads its page from ITS OWN ~/.hermes/desktop-plugins/pit-crew/,
  so install the Desktop half there. In that Desktop app: Capabilities -> Plugins -> Install from Git -> `trevornk/pit-crew`
  with the Desktop target checked (or open hermes://plugin/install?repo=trevornk/pit-crew). Then toggle it on, and use Rescan after updates.
- After updating the plugin: `hermes plugins update pit-crew` (reloads the dashboard and the gateway), then in Desktop run
  "Reload desktop plugins" (Cmd+K). `install --force` over an enabled plugin does not reload on Hermes 0.23: restart `hermes dashboard`
  and the gateway. Press Enter, not Tab, on `/car`.
- No Desktop? `hermes dashboard` has a Pit Crew tab served by this Hermes, and `/car` works in any chat.
More: README -> "Update and restart" and "Desktop on another machine"."""


def _status_card(svc: Any) -> str:
    try:
        state = svc.get_state(force=True)
    except SecretScopeUnavailable as exc:
        return f"Car: {exc}"
    except AdapterError as exc:
        info = svc.connection_info({"ok": False, "error": str(exc)})
        return (f"Pit Crew isn't connected to your car yet.\n{exc}\n{info['hint']}\n"
                "Want to try it first? Set Connection to “simulator” (Settings → Plugins → Pit Crew).")
    now = svc.now()
    v = state.value
    tag = "  (demo)" if state.simulated and "simulated" not in state.name.lower() else ""
    lines: List[str] = [f"**{state.name}**{tag}"]
    bat = []
    if state.has("battery_pct"):
        bat.append(f"Battery {v('battery_pct'):.0f}%" + (f" ({v('range_mi'):.0f} mi)" if state.has("range_mi") else ""))
    cs = v("charging_state")
    if cs == "charging":
        extra = f"Charging {v('charge_power_kw'):.1f} kW" if state.has("charge_power_kw") else "Charging"
        if state.has("charge_limit_pct"):
            extra += f" → {v('charge_limit_pct'):.0f}%"
        eta = v("charge_eta_min")
        if isinstance(eta, (int, float)) and eta > 0:
            extra += f" (done ~{fmt_clock(svc.local_dt(now + eta * 60))})"
        bat.append(extra)
    elif cs == "complete":
        bat.append("Charge complete")
    elif cs == "stopped":
        bat.append("Plugged in, NOT charging")
    elif cs == "disconnected":
        bat.append("Not plugged in")
    if bat:
        lines.append(" · ".join(bat))
    sec = []
    if state.has("locked"):
        sec.append("Locked" if v("locked") else "UNLOCKED")
    if state.has("location_zone"):
        sec.append(_where(v("location_zone")))
    if state.has("cabin_temp_f"):
        t = f"cabin {v('cabin_temp_f'):.0f}°F"
        if state.has("outside_temp_f"):
            t += f", outside {v('outside_temp_f'):.0f}°F"
        sec.append(t)
    if sec:
        lines.append(" · ".join(sec))
    opens = []
    for key, name in (("doors_open", "doors"), ("windows_open", "windows")):
        val = v(key)
        if isinstance(val, list) and val:
            opens.append(f"{name} open: {', '.join(val)}")
    if v("trunk_open"):
        opens.append("trunk open")
    if v("frunk_open"):
        opens.append("frunk open")
    extras = [("; ".join(opens) if opens else "all closed")]
    tire_line = tires.summary_line(state, svc.watcher.engine.rule_config(RULES_BY_ID["tire_pressure_low"]).get("min_psi"))
    if tire_line:
        lines.append(tire_line)
    if state.has("sentry_on"):
        extras.append("Sentry on" if v("sentry_on") else "Sentry off")
    if v("software_update_available"):
        extras.append("software update waiting")
    if v("asleep"):
        extras.append("asleep")
    lines.append(" · ".join(extras))
    info = svc.armed_info()
    if info["armed"]:
        until = f" until {fmt_clock(svc.local_dt(info['until']))}" if info["until"] else " (standing setting)"
        lines.append(f"Mode: LIVE{until} — Hermes can send commands (unlock, trunk and similar still need your approval)")
    else:
        lines.append("Mode: Practice — commands are checked and logged, nothing is sent. (`/car live` to go live)")
    for w in svc.warnings(state):
        lines.append("⚠ " + w)
    open_alerts = svc.alerts(("new", "surfaced"))
    lines.append(f"Alerts: {len(open_alerts)} open" if open_alerts else "Alerts: none")
    age = state.freshness_age_s(now)
    if age is not None:
        lines.append("Updated just now" if age < 60 else f"Updated {int(age // 60)} min ago" if age < 5400
                     else f"Updated {age / 3600:.0f} h ago")
    return "\n".join(lines)


def _when(iso: str) -> str:
    from datetime import datetime
    try:
        d = datetime.fromisoformat(iso)
        return d.strftime("%b %-d %-I:%M %p")
    except (ValueError, TypeError):
        return str(iso)[5:16].replace("T", " ")


def _where(zone: Any) -> str:
    z = str(zone or "").strip()
    if not z or z.lower() in ("away", "not_home", "not home"):
        return "away from home"
    if z.lower() in ("unknown", "unavailable"):
        return "location unknown"
    return f"at {z}"


_WHO = {"tool": "Hermes", "slash": "/car", "desktop": "Pit Crew page", "web": "Pit Crew page", "cron": "Scheduled job"}


def _outcome(r: Dict[str, Any]) -> str:
    st = r["status"]
    if st == "confirmed":
        return "sent, car confirmed"
    if st == "sent_unconfirmed":
        return "sent, not confirmed yet"
    if st == "dry_run":
        return "practice run, nothing sent"
    if st == "blocked":
        why = (r.get("detail") or "").strip().rstrip(".")
        return "refused" + (f" ({why[:70]})" if why else "")
    if st == "unknown_outcome":
        return "unclear whether it was sent"
    return st.replace("_", " ")


def _audit_text(svc: Any, n: int) -> str:
    rows = svc.audit_log(n)
    if not rows:
        return "The audit log is empty."
    out = []
    for r in rows:
        if r["command"] in ("arm", "disarm"):
            out.append(f"{_when(r['when'])} · {'went live' if r['command'] == 'arm' else 'back to practice mode'} ({_WHO.get(r['surface'], r['surface'])})")
            continue
        args = r["args"] if isinstance(r["args"], dict) else {}
        shown = ",".join(f"{k}={v}" for k, v in args.items() if k not in ("override_zone", "wake_ok"))
        appr = {"confirmed": " · you approved", "denied": " · you declined"}.get(r["approval"], "")
        out.append(f"{_when(r['when'])} · {_WHO.get(r['surface'], r['surface'])} · {r['command']}{'(' + shown + ')' if shown else ''}"
                   f" · {_outcome(r)}{appr}")
    return "Recent command attempts (newest first):\n" + "\n".join(out)


def _alerts_text(svc: Any, rest: List[str]) -> str:
    if rest and rest[0] == "ack":
        target = rest[1] if len(rest) > 1 else "all"
        if target == "all":
            ids = [int(a["id"]) for a in svc.alerts(("new", "surfaced"))]
            if not ids:
                return "No open alerts to acknowledge."
        elif target.isascii() and target.isdigit() and len(target) < 15:
            ids = [int(target)]
        else:
            return "Alert ids are numbers: see `/car alerts`. (`/car alerts ack` alone acknowledges all of them.)"
        done = [i for i in ids if svc.store.alert_set_state(i, "acked")]
        if target != "all" and not done:
            return f"There is no alert #{target}. See `/car alerts`."
        return f"Acknowledged {len(done)} alert{'s' if len(done) != 1 else ''}."
    rows = svc.alerts(("new", "surfaced", "acked"))
    if not rows:
        return "No open car alerts."
    for r in rows:
        if r["state"] == "new":
            svc.store.alert_set_state(int(r["id"]), "surfaced")
    order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    rows.sort(key=lambda r: (r["state"] == "acked", order.get(r["severity"], 9), -int(r["id"])))
    out = []
    for r in rows:
        out.append(f"#{r['id']} [{r['severity']}] {_clean_title(r['title'])}: {r['body']}" +
                   ("  (acknowledged)" if r["state"] == "acked" else f"\n   → {_user_suggestion(r['suggestion'])}"))
    return "\n".join(out)


def _clean_title(t: str) -> str:
    return t.replace("[Simulated] ", "")


def _user_suggestion(text: str) -> str:
    """Alert suggestions are phrased for the agent ("Want me to lock it? (lock needs no approval)"); show people a verb."""
    t = (text or "").strip()
    low = t.lower()
    if "lock it" in low or "lock needs no approval" in low:
        return "Say *lock it* (or `/car lock`)."
    return t


def _arm(svc: Any, rest: List[str]) -> str:
    confirmed = "confirm" in rest
    nums = [r for r in rest if _num(r)]
    want = float(nums[0]) if nums else None
    hours = svc.clamp_hours(want)
    note = ""
    if want is not None and abs(hours - want) > 1e-9:
        note = f" (limits are 15 minutes to 72 hours, so {want:g} became {hours:g})"
    simulated = getattr(svc.adapter, "simulated", False)
    if not confirmed:
        what = ("This is the SIMULATED car, so nothing real can happen." if simulated else
                f"Hermes will be able to send real commands to your {svc.adapter.label}. Unlock, trunk and similar still ask for your approval each time.")
        question = f"Go live for {hours:g} {'hour' if hours == 1 else 'hours'}{note}? {what}"
        answer = None
        ctx = svc.ctx
        if ctx is not None:
            try:
                answer = ctx.ask_choice(question, [{"value": "yes", "label": f"Go live for {hours:g} h"},
                                                  {"value": "no", "label": "Cancel (stay in practice mode)"}])
            except Exception as exc:
                logger.info("ask_choice unavailable: %s", exc)
        if answer is None:
            return (f"{question}\nI can't show a confirmation here. To go live, type: `/car live {hours:g} confirm`")
        if answer != "yes":
            return "Cancelled: staying in practice mode (nothing is sent)."
    info = svc.arm(hours, by="slash")
    return f"Live for {hours:g} h{note}, until {fmt_clock(svc.local_dt(info['until']))}. `/car practice` stops it sooner."


def _quick(svc: Any, command: str, args: Dict[str, Any], what: str) -> str:
    """/car lock, /car warm: the safe-direction commands, through the same safety engine as everything else."""
    res = svc.run_command(command, args, surface="slash", session="slash", reason="typed /car " + what, approval=None,
                          unattended=False)
    return res.get("message") or str(res.get("status"))


def _ready(svc: Any, rest: List[str]) -> str:
    miles = None
    words = list(rest)
    if words:
        last = words[-1].lower().rstrip(".")
        for suffix in ("miles", "mi"):
            if last.endswith(suffix) and _num(last[: -len(suffix)]):
                miles, words = float(last[: -len(suffix)]), words[:-1]
                break
        else:
            if len(words) > 1 and _num(last) and not words[-2].lower().endswith(("am", "pm", ":")):
                if ":" not in words[-2] and not words[-2].isdigit():
                    miles, words = float(last), words[:-1]
    res = svc.readiness(" ".join(words) or None, miles)
    if not res.get("ok"):
        return f"Can't work that out: {res.get('error', 'unknown problem')}" + (f" ({res['hint']})" if res.get("hint") else "")
    mark = {"ready": "✓ Ready", "tight": "⚠ Tight", "not_ready": "✗ Not ready"}.get(res["verdict"], res["verdict"])
    lines = [f"{mark}: {res['summary']}",
             f"{res['battery_now_pct']:.0f}% now → {res['projected_at_departure_pct']:.0f}% at departure; the trip needs about {res['needed_pct']:.0f}%."]
    for r in res.get("recommendations") or []:
        lines.append(f"→ {r['action'].replace('_', ' ').capitalize()}: {r['why']}")
    return "\n".join(lines)


def _sim(svc: Any, rest: List[str]) -> str:
    adapter = svc.adapter
    if not getattr(adapter, "simulated", False) or not hasattr(adapter, "control"):
        return "`/car sim` only works with the simulated car (adapter=simulator)."
    if not rest:
        return ("Usage: /car sim advance <min> | drive <mi> [zone] | park_home | plug_in | unplug | leave_unlocked | open_door | close_doors | event <kind> | cold <°F> | normal_weather | set_soc <pct> | leak [tire] | low_tire [tire] [psi] | inflate | sleep | reset\n"
                "Tip: after `drive`, use `park_home` (and `plug_in`) before trying `unlock`.")
    action, *params = rest
    kw: Dict[str, Any] = {}
    try:
        if action == "advance":
            kw["minutes"] = float(params[0]) if params else 30
        elif action == "drive":
            kw["miles"] = float(params[0]) if params else 10
            if len(params) > 1:
                kw["to"] = params[1]
        elif action == "event":
            kw["kind"] = params[0] if params else "door_ajar"
        elif action == "set_soc":
            kw["percent"] = float(params[0])
        elif action == "cold":
            kw["temp_f"] = float(params[0]) if params else 18
        elif action in ("leak", "low_tire"):
            if params:
                kw["tire"] = params[0]
            if len(params) > 1:
                kw["psi_per_hour" if action == "leak" else "psi"] = float(params[1])
        res = adapter.control(action, **kw)
    except (ValueError, IndexError):
        return "Bad arguments for /car sim. Try `/car sim advance 90`."
    except AdapterError as exc:
        return f"Simulator: {exc}"
    svc.get_state(force=True)
    return f"Simulator: {res['note']}."


def _notify(svc: Any, rest: List[str], session_key: str, platform: str) -> str:
    d = svc.watcher.deliverer
    sub = rest[0] if rest else "status"
    if sub == "here":
        d.bind(session_key, platform)
        if svc.ctx is None:
            return "Bound this chat, but the plugin context is unavailable so pushes may not work."
        return ("Proactive car alerts will be sent to this chat.\n"
                "If nothing arrives, Hermes may be blocking plugin messages: set "
                "`plugins.entries.pit-crew.allow_gateway_injection: true` in config.yaml "
                "(see README → Proactive alerts).")
    if sub == "off":
        d.unbind()
        return "Alerts will no longer be pushed to a chat (they still appear in `/car alerts` and are mentioned on your next turn)."
    if sub == "test":
        res = d.send_test()
        if res["ok"]:
            note = "" if res.get("push_enabled") else "\n(Pushed alerts are switched off in settings, so real alerts would not be sent until you turn `alert_push` on.)"
            return "Test alert sent: delivery to the bound chat works. Look for “Pit Crew test alert: delivery works”." + note
        return "Test alert FAILED: " + str(res["hint"])
    st = d.status()
    t = d.target()
    if not st["bound"]:
        return "No chat is bound for pushed alerts. Run `/car notify here` in the chat you want."
    last = st["last_push"]
    tail = f" Last push: {'ok' if last.get('ok') else 'refused'}." if last else ""
    return f"Pushing alerts to a {t.get('platform') or 'local'} chat." + tail + (f"\n{last['hint']}" if last and last.get("hint") else "")


def _discover(svc: Any) -> str:
    res = svc.discover_ha()
    if not res.get("ok"):
        if "Background alerts need" in str(res.get("error")):
            return f"Couldn't look at Home Assistant: {res.get('error')}"
        return (f"Couldn't look at Home Assistant: {res.get('error')}\n"
                "Set “Home Assistant · URL” and “Home Assistant · Access token” in Settings → Plugins → Pit Crew, "
                "then run `/car discover` again.")
    rec = res.get("recommended")
    lines = [f"Looked at {res['entities_seen']} Home Assistant entities."]
    if rec:
        lines.append(f"Best match: car name “{rec['ha_slug']}”, brand mapping “{rec['ha_profile']}” ({rec['note']}).")
        ctx = svc.ctx
        answer = None
        if ctx is not None:
            try:
                answer = ctx.ask_choice(f"Use “{rec['ha_slug']}” ({rec['ha_profile']})?",
                                        [{"value": "yes", "label": f"Use {rec['ha_slug']}"}, {"value": "no", "label": "Not now"}])
            except Exception as exc:
                logger.info("ask_choice unavailable: %s", exc)
        lines.append("In Settings → Plugins → Pit Crew set “Home Assistant · Car name” to "
                     f"{rec['ha_slug']} and “Home Assistant · Car brand mapping” to {rec['ha_profile']}, "
                     "and “Connection · Which car” to homeassistant. Changes apply immediately; then run `/car`."
                     + (" (I can't change settings for you from here.)" if answer == "yes" else ""))
    else:
        lines.append("No bundled profile matched well. A generic guess:")
        lines.append(json.dumps(res["generic_proposal"]["entity_overrides"], indent=2))
    for w in res.get("warnings", []):
        lines.append("WARNING: " + w)
    for p in res["proposals"][:3]:
        lines.append(f"  - {p['profile']} / {p['slug']}: {int(p['coverage'] * 100)}% of fields found"
                     f"{'' if p['tested'] else ' (not run on a real car)'}"
                     f"{' (integration removed)' if p.get('deprecated') else ''}")
    return "\n".join(lines)


def handle(raw_args: str = "", *, session_id: str = "", session_key: str = "", platform: str = "", **kwargs: Any) -> str:
    """Entry point for ``/car …``. Always returns text; never raises."""
    try:
        svc = get_service()
        parts = (raw_args or "").strip().split()
        sub = parts[0].lower() if parts else "status"
        rest = parts[1:]
        if sub in ("status", "s"):
            return _status_card(svc)
        if sub in ("live", "arm", "golive", "go-live"):
            return _arm(svc, rest)
        if sub in ("practice", "disarm", "stop"):
            svc.disarm(by="slash")
            return "Back to practice mode. Nothing will be sent to the car."
        if sub == "lock":
            return _quick(svc, "lock", {}, "lock")
        if sub in ("warm", "warmup", "warm-up"):
            temp = rest[0] if rest and _num(rest[0]) else None
            return _quick(svc, "climate_on", {"temp_f": float(temp)} if temp else {"temp_f": 70}, "warm")
        if sub in ("ready", "readiness"):
            return _ready(svc, rest)
        if sub == "alerts":
            return _alerts_text(svc, rest)
        if sub == "log":
            return _audit_text(svc, _count(rest[0], 10, 200) if rest else 10)
        if sub == "rules":
            lines = []
            for r in svc.watcher.engine.describe_rules():
                cfg = ", ".join(f"{RULE_LABELS.get(k, k.replace('_', ' '))} {v}" for k, v in r["settings"].items())
                lines.append(f"{'✓' if r['enabled'] else '–'} {r['title']}" + (f" ({cfg})" if cfg else ""))
            return "Alert rules (change them on the Pit Crew page, or ask me):\n" + "\n".join(lines)
        if sub == "notify":
            return _notify(svc, rest, session_key, platform)
        if sub == "import":
            if not rest or rest[0] != "teslamate":
                return "Usage: /car import teslamate [days]"
            res = svc.import_history("teslamate", _count(rest[1], 365, 3650) if len(rest) > 1 else 365)
            if not res["ok"]:
                return f"Import failed: {res['error']}"
            return (f"Imported {res['drives']} trips and {res['charges']} charging sessions from TeslaMate "
                    f"({res['already_imported']} already imported, {res['skipped_overlap']} skipped because Pit Crew recorded them itself).")
        if sub == "discover":
            return _discover(svc)
        if sub == "sim":
            return _sim(svc, rest)
        if sub in ("desktop-install", "desktop"):
            return DESKTOP_INSTALL
        if sub in ("help", "?", "-h", "--help"):
            return f"{DISPLAY_NAME} {VERSION}\n{HELP}"
        return f"Unknown subcommand '{sub[:40]}' ({DISPLAY_NAME} {VERSION}).\n{HELP}"
    except Exception as exc:
        logger.exception("/car failed")
        return f"/car hit an internal error ({type(exc).__name__}). Details are in the Hermes log."
