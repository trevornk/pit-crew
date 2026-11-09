"""Tool handlers. Every handler returns a JSON string and never raises."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from .brain.commands import ArgError, get_spec, validate_args
from .safety.gate import split_tool_args
from .service import get_service

logger = logging.getLogger(__name__)


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, default=str, ensure_ascii=False)


def _truthy(value: Any) -> bool:
    """A model may pass the string "false"; ``bool("false")`` is True."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "y")
    return bool(value)


def _error(message: str, **extra: Any) -> str:
    return _json({"ok": False, "error": message, **extra})


def car_status(args: Dict[str, Any], **kwargs: Any) -> str:
    try:
        return _json(get_service().status(force=_truthy((args or {}).get("refresh"))))
    except Exception as exc:  # last-resort guard: tools must never raise into the agent loop
        logger.exception("car_status failed")
        return _error(f"car_status failed: {type(exc).__name__}: {exc}")


def car_history(args: Dict[str, Any], **kwargs: Any) -> str:
    try:
        from datetime import datetime

        args = args or {}
        svc = get_service()
        start = end = None
        tz = svc.local_tz()
        try:
            if args.get("start_date"):
                start = datetime.fromisoformat(str(args["start_date"])).replace(tzinfo=tz).timestamp()
            if args.get("end_date"):
                end = datetime.fromisoformat(str(args["end_date"])).replace(tzinfo=tz).timestamp() + 86400
        except ValueError:
            return _error("start_date/end_date must be ISO dates like 2026-10-01")
        days = args.get("days")
        return _json(svc.history(str(args.get("kind") or "summary"), days=int(days) if days is not None else 30,
                                 start=start, end=end))
    except Exception as exc:
        logger.exception("car_history failed")
        return _error(f"car_history failed: {type(exc).__name__}: {exc}")


def car_readiness(args: Dict[str, Any], **kwargs: Any) -> str:
    try:
        args = args or {}
        miles = args.get("trip_miles")
        temp = args.get("expected_temp_f")
        return _json(get_service().readiness(
            args.get("departure"), float(miles) if miles is not None else None,
            round_trip=_truthy(args.get("round_trip")), expected_temp_f=float(temp) if temp is not None else None))
    except (TypeError, ValueError) as exc:
        return _error(f"bad argument: {exc}")
    except Exception as exc:
        logger.exception("car_readiness failed")
        return _error(f"car_readiness failed: {type(exc).__name__}: {exc}")


def car_command(args: Dict[str, Any], task_id: str = "", session_id: str = "", **kwargs: Any) -> str:
    """Run one command through the safety engine. Approval evidence comes only from the ticket that the
    ``pre_tool_call`` hook issued and Hermes' approval pipeline confirmed — never from tool arguments."""
    try:
        svc = get_service()
        command, raw, reason = split_tool_args(args)
        if not command:
            return _error("car_command needs a 'command'", hint="e.g. {\"command\": \"lock\"}")
        evidence = None
        spec = get_spec(command)
        if spec is not None and spec.requires_approval:
            try:
                clean = validate_args(spec, raw)
            except ArgError:
                clean = None
            if clean is not None:
                # Follow the user's Hermes approval setting: a call Hermes let through without asking counts, unless
                # the user listed this command in require_human_for (then only a person's recorded "yes" does).
                strict = spec.name in svc.settings.get("require_human_for")
                evidence = svc.broker.take(spec.name, clean, task_id, strict=strict)
        result = svc.run_command(command, raw, surface="tool", session=session_id or task_id, reason=reason,
                                 approval=evidence, unattended=svc.is_unattended())
        return _json(result)
    except Exception as exc:
        logger.exception("car_command failed")
        return _error(f"car_command failed: {type(exc).__name__}: {exc}")


def car_alerts(args: Dict[str, Any], **kwargs: Any) -> str:
    try:
        args = args or {}
        svc = get_service()
        action = str(args.get("action") or "list").lower()
        if action == "check_now":
            result = svc.watcher.poll_once(force=True)
            open_alerts = svc.alerts(("new", "surfaced", "acked"))
            for a in svc.alerts(("new",)):
                svc.store.alert_set_state(int(a["id"]), "surfaced")
            return _json({"ok": "error" not in result, "checked": result.get("polled", False),
                          "error": result.get("error"), "newly_raised": result.get("raised", []),
                          "open_alerts": open_alerts, "armed": svc.armed_info()["armed"],
                          "summary": (f"{len(open_alerts)} open alert(s)." if open_alerts else "All clear: no open alerts.")})
        if action == "ack":
            target = str(args.get("alert_id") or "").strip().lower()
            if not target:
                return _error("ack needs alert_id (an id or 'all')")
            ids = [int(a["id"]) for a in svc.alerts(("new", "surfaced"))] if target == "all" else [int(target)]
            done = [i for i in ids if svc.store.alert_set_state(i, "acked")]
            return _json({"ok": True, "acknowledged": done})
        if action != "list":
            return _error(f"unknown action '{action}'", hint="use list, ack or check_now")
        states = ("new", "surfaced", "acked", "resolved") if _truthy(args.get("include_resolved")) else ("new", "surfaced", "acked")
        rows = svc.alerts(states)
        for r in rows:
            if r["state"] == "new":
                svc.store.alert_set_state(int(r["id"]), "surfaced")
        return _json({"ok": True, "alerts": [{k: r[k] for k in ("id", "when", "severity", "title", "body", "suggestion", "state", "rule")} for r in rows],
                      "count": len(rows), "summary": (f"{len(rows)} alert(s)." if rows else "No open alerts.")})
    except ValueError:
        return _error("alert_id must be a number or 'all'")
    except Exception as exc:
        logger.exception("car_alerts failed")
        return _error(f"car_alerts failed: {type(exc).__name__}: {exc}")


def car_rules(args: Dict[str, Any], **kwargs: Any) -> str:
    try:
        args = args or {}
        svc = get_service()
        engine = svc.watcher.engine
        action = str(args.get("action") or "list").lower()
        rule = str(args.get("rule") or "").strip()
        if action == "list":
            try:
                state = svc.get_state()
            except Exception as exc:  # the rule list is still useful when the car is unreachable
                logger.info("car_rules: listing without live state (%s)", exc)
                state = None
            return _json({"ok": True, "rules": engine.describe_rules(state), "watcher_enabled": bool(svc.settings.get("watcher_enabled")),
                          "poll_interval_s": svc.settings.get("poll_interval_s")})
        if action not in ("enable", "disable", "configure"):
            return _error(f"unknown action '{action}'", hint="use list, enable, disable or configure")
        if not rule:
            return _error(f"{action} needs a rule id")
        try:
            if action == "configure":
                changes = args.get("settings")
                if not isinstance(changes, dict) or not changes:
                    return _error("configure needs a non-empty 'settings' object")
                cfg = engine.set_rule(rule, **changes)
            else:
                cfg = engine.set_rule(rule, enabled=(action == "enable"))
        except KeyError:
            from .watcher.rules import RULES_BY_ID

            return _error(f"unknown rule '{rule}'", rules=sorted(RULES_BY_ID))
        except ValueError as exc:
            return _error(str(exc))
        except (OSError, RuntimeError) as exc:      # e.g. a managed install: the setting can't be written
            return _error(f"couldn't save the rule to your Hermes settings ({type(exc).__name__}: {exc})")
        return _json({"ok": True, "rule": rule, "now": cfg})
    except Exception as exc:
        logger.exception("car_rules failed")
        return _error(f"car_rules failed: {type(exc).__name__}: {exc}")
