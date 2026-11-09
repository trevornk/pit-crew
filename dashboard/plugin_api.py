"""Backend for the Desktop page, mounted by Hermes at /api/plugins/pit-crew/.

It is a thin JSON layer over the plugin's ``CarService``. It reuses the already-loaded plugin package
when there is one (so the agent and the page share the same in-memory service) and otherwise loads its
own copy; either way all durable state (audit log, alerts, armed state, simulator car) is in the shared
plugin SQLite database, so both always agree.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter

ROOT = Path(__file__).resolve().parents[1]
router = APIRouter()
_core: Any = None


def core() -> Any:
    """The plugin package module (already loaded by Hermes if possible).

    Not a documented Hermes pattern: plugin_api.py is meant to be self-contained, and importing the plugin package is a
    convenience. It works because all durable state is in the shared plugin DB. Under ``plugins.isolation: host`` or after
    a forced reload a second copy of the module singletons can exist, and there ``svc.ctx`` is None (no ctx-only APIs such as ``ask_choice``,
    ``is_unattended()`` is False). Nothing on the page path may rely on ``svc.ctx``; today nothing does."""
    global _core
    if _core is not None:
        return _core
    target = (ROOT / "__init__.py").resolve()
    for mod in list(sys.modules.values()):
        try:
            f = getattr(mod, "__file__", None)
            if f and Path(f).resolve() == target and hasattr(mod, "register"):
                _core = mod
                return mod
        except (OSError, ValueError):
            continue
    name = "pit_crew_desktop_core"
    spec = importlib.util.spec_from_file_location(name, target, submodule_search_locations=[str(ROOT)])
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the pit-crew package")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    _core = mod
    return mod


def svc() -> Any:
    return core().service.get_service()


def _err(message: str, **extra: Any) -> Dict[str, Any]:
    return {"ok": False, "error": message, **extra}


# Flags that let the AGENT, after talking to the human, wake a sleeping car or skip the trusted-zone check. A page click
# is not that conversation, so the pages cannot set them (they are removed, not honoured).
POLICY_FLAGS = ("wake_ok", "override_zone")


def _page_args(body: dict) -> Optional[Dict[str, Any]]:
    """The command arguments from a page request, or ``None`` if they are not an object."""
    args = body.get("args")
    if args is None:
        return {}
    if not isinstance(args, dict):
        return None
    return {k: v for k, v in args.items() if k not in POLICY_FLAGS}


def _num_param(value: Any, name: str) -> Optional[float]:
    import math
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(num):
        raise ValueError(f"{name} must be a finite number")
    return num


@router.get("/summary")
def summary(refresh: bool = False) -> Dict[str, Any]:
    s = svc()
    status = s.status(force=refresh)
    out: Dict[str, Any] = {"ok": status.get("ok", False), "status": status}
    info = s.armed_info()
    out["armed"] = {"armed": info["armed"], "source": info["source"],
                    "until_local": s.local_dt(info["until"]).isoformat(timespec="minutes") if info["until"] else None,
                    "setting_armed": info["setting_armed"], "mode": "live" if info["armed"] else "practice",
                    "default_hours": s.clamp_hours(None)}
    out["alerts"] = _with_actions(s.alerts(("new", "surfaced", "acked"), limit=30))
    out["adapter"] = s.describe_adapter()
    out["connection"] = s.connection_info(status)
    out["simulated"] = bool(out["adapter"].get("simulated"))
    snap = s.settings.snapshot()
    out["settings"] = {k: snap[k] for k in ("adapter", "history", "trusted_zones", "max_state_age_s", "stale_after_s",
                                           "poll_interval_s", "asleep_poll_interval_s", "watcher_enabled", "alert_push",
                                           "require_human_for", "rate_limit_commands", "rate_limit_window_s",
                                           "default_departure", "default_trip_mi", "arm_default_hours")}
    out["notify"] = s.watcher.deliverer.status()
    if "error" in out["adapter"]:
        out["status"].setdefault("error", out["adapter"]["error"])
    out["watcher"] = {"last_poll": s.watcher.last_poll, "last_error": s.watcher.last_error}
    return out


@router.get("/readiness")
def readiness(departure: Optional[str] = None, trip_miles: Optional[float] = None, round_trip: bool = False,
              expected_temp_f: Optional[float] = None) -> Dict[str, Any]:
    return svc().readiness(departure, trip_miles, round_trip=round_trip, expected_temp_f=expected_temp_f)


@router.get("/history")
def history(kind: str = "summary", days: int = 30) -> Dict[str, Any]:
    return svc().history(kind, days=days)


@router.post("/preflight")
def preflight(body: dict) -> Dict[str, Any]:
    """Show the safety decision for a command before the user confirms it (nothing is sent)."""
    s = svc()
    command = str(body.get("command", "")).strip().lower()
    args = _page_args(body)
    if args is None:
        return _err("arguments must be an object of name: value pairs")
    pre = s.engine.preflight(command, args)
    spec = pre.get("spec")
    out: Dict[str, Any] = {"ok": pre["ok"], "status": pre["status"], "message": pre["message"], "command": command}
    if spec is not None:
        out.update({"risk": spec.risk, "summary": spec.summary, "needs_human_approval": spec.requires_approval,
                    "safe_direction": spec.safe_direction})
    if pre.get("policy") is not None:
        out["policy"] = pre["policy"].to_dict()
    info = s.armed_info()
    out["armed"] = info["armed"]
    out["mode"] = "live" if info["armed"] else "dry_run"
    if spec is not None and spec.requires_approval:
        # P1-2: approval-class commands are never executed from a UI click. The page previews the safety checks and
        # hands the request to Hermes, whose real approval prompt is the only live approval.
        out["via_hermes"] = True
        out["ask_hermes"] = ask_hermes_text(spec.name, pre.get("args") or {})
    return out


ASK_HERMES = {
    "unlock": "Unlock my car.", "open_trunk": "Open the trunk.", "open_frunk": "Open the frunk.",
    "vent_windows": "Vent the windows.", "sentry_off": "Turn Sentry mode off.", "honk": "Honk the horn once.",
    "flash": "Flash the lights once.",
}

# the one-click fix an alert can offer (rule id -> command); anything approval-class goes through Hermes
ALERT_ACTIONS = {
    "left_unlocked_away": ("lock", "Lock it"), "unlocked_at_home_night": ("lock", "Lock it"),
    "opening_left_open": ("close_windows", "Close windows"), "plugged_not_charging": ("charge_start", "Start charging"),
    "sentry_off_away": ("sentry_on", "Turn Sentry on"),
}


def ask_hermes_text(command: str, args: Dict[str, Any]) -> str:
    base = ASK_HERMES.get(command) or f"Run the car command {command}."
    return base if not args else f"{base} (arguments: {', '.join(f'{k}={v}' for k, v in args.items())})"


def _with_actions(alerts: list) -> list:
    out = []
    for a in alerts:
        a = dict(a)
        act = ALERT_ACTIONS.get(a.get("rule"))
        if act:
            spec = core().brain.commands.get_spec(act[0])
            a["action"] = {"command": act[0], "label": act[1], "via_hermes": bool(spec and spec.requires_approval),
                           "ask": ask_hermes_text(act[0], {})}
        out.append(a)
    return out


@router.post("/command")
def command(body: dict) -> Dict[str, Any]:
    """Run a command from the Desktop / web page. ``confirmed`` records that the dialog was shown. Approval-class
    commands are refused here while armed (they go through Hermes); in dry-run they only produce a preview."""
    s = svc()
    cmd = str(body.get("command", "")).strip().lower()
    if body.get("confirmed") is not True:                 # the dialog sends a real true; "false"/"0"/1 are not a confirmation
        return _err("this command needs the confirmation dialog", status="needs_confirmation")
    args = _page_args(body)
    if args is None:
        return _err("arguments must be an object of name: value pairs", status="invalid")
    spec = core().brain.commands.get_spec(cmd)
    if spec is not None and spec.requires_approval and s.armed_info()["armed"]:
        # P1-2 / rule 12: Hermes' approval prompt is the only live approval; a click here cannot stand in for it.
        return _err("this command needs your approval in Hermes: ask it in chat and answer the approval prompt",
                    status="ask_hermes", ask_hermes=ask_hermes_text(cmd, args))
    res = s.run_command(cmd, args, surface="desktop", session="desktop",
                        reason=str(body.get("reason") or "from the Desktop page"), approval=None, unattended=False)
    if res.get("status") in ("confirmed", "sent_unconfirmed"):
        try:   # let the alert rules see the fix now, so the alert it solved clears right away
            s.watcher.poll_once(force=True)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).debug("post-command poll failed", exc_info=True)
    return res


@router.post("/arm")
def arm(body: dict) -> Dict[str, Any]:
    s = svc()
    if body.get("confirmed") is not True:
        return _err("arming needs the confirmation dialog", status="needs_confirmation")
    try:
        hours = _num_param(body.get("hours") or s.settings.get("arm_default_hours"), "hours")
    except ValueError as exc:
        return _err(str(exc))
    info = s.arm(hours, by="desktop")
    return {"ok": True, "armed": info["armed"], "hours": info.get("hours"), "until_local": info.get("until_local")}


@router.post("/disarm")
def disarm() -> Dict[str, Any]:
    info = svc().disarm(by="desktop")
    return {"ok": True, "armed": info["armed"]}


@router.get("/log")
def log(limit: int = 50) -> Dict[str, Any]:
    rows = svc().audit_log(max(1, min(limit, 200)))
    return {"ok": True, "rows": rows}


@router.post("/alerts/ack")
def ack(body: dict) -> Dict[str, Any]:
    s = svc()
    target = str(body.get("id", "all"))
    try:
        ids = [int(a["id"]) for a in s.alerts(("new", "surfaced"))] if target == "all" else [int(target)]
    except (TypeError, ValueError):
        return _err("id must be an alert number or 'all'")
    done = [i for i in ids if s.store.alert_set_state(i, "acked")]
    return {"ok": True, "acknowledged": done}


@router.post("/alerts/check")
def check_now() -> Dict[str, Any]:
    res = svc().watcher.poll_once(force=True)
    return {"ok": "error" not in res, "polled": res.get("polled", False), "error": res.get("error"),
            "raised": len(res.get("raised", []))}


@router.get("/rules")
def rules() -> Dict[str, Any]:
    s = svc()
    try:
        state = s.get_state()
    except Exception as exc:  # the card still lists rules even if the car is unreachable
        logging.getLogger(__name__).info("rules listed without live state: %s", exc)
        state = None
    return {"ok": True, "rules": s.watcher.engine.describe_rules(state)}


@router.post("/rules/{rule_id}")
def set_rule(rule_id: str, body: dict) -> Dict[str, Any]:
    s = svc()
    changes: Dict[str, Any] = {}
    if "enabled" in body:
        en = body["enabled"]
        changes["enabled"] = en if isinstance(en, bool) else str(en).strip().lower() in ("1", "true", "yes", "on")
    extra = body.get("settings")
    if extra is not None and not isinstance(extra, dict):
        return _err("settings must be an object of name: value pairs")
    changes.update(extra or {})
    try:
        cfg = s.watcher.engine.set_rule(rule_id, **changes)
    except KeyError:
        return _err(f"unknown rule {rule_id}")
    except ValueError as exc:
        return _err(str(exc))
    except (OSError, RuntimeError) as exc:      # e.g. a managed install: the setting can't be written
        return _err(f"couldn't save the rule to your Hermes settings ({type(exc).__name__}: {exc})")
    return {"ok": True, "rule": rule_id, "now": cfg}


@router.post("/sim")
def sim(body: dict) -> Dict[str, Any]:
    """Demo controls — only available while the simulated car is the adapter."""
    s = svc()
    adapter = s.adapter
    if not getattr(adapter, "simulated", False) or not hasattr(adapter, "control"):
        return _err("simulator controls are only available with the simulated car")
    action = str(body.get("action", ""))
    params = body.get("params")
    if params is not None and not isinstance(params, dict):
        return _err("params must be an object")
    kwargs = {k: v for k, v in (params or {}).items() if k in ("minutes", "miles", "to", "kind", "percent", "temp_f", "door", "tire", "psi", "psi_per_hour")}
    try:
        res = adapter.control(action, **kwargs)
    except Exception as exc:  # AdapterError or bad numbers: report, don't 500
        return _err(str(exc))
    s.get_state(force=True)
    s.watcher.poll_once(force=True)  # let the rules react to the new situation right away
    return {"ok": True, "note": res["note"]}


@router.post("/discover")
def discover() -> Dict[str, Any]:
    return svc().discover_ha()


@router.post("/import")
def import_history(body: dict) -> Dict[str, Any]:
    """Optional backfill of trips/charges from TeslaMate into Pit Crew's own history."""
    try:
        days = int(body.get("days") or 365)
    except (TypeError, ValueError):
        return _err("days must be a number")
    return svc().import_history(str(body.get("source", "teslamate")), days)
