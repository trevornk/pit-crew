"""Hermes hooks that put approval-class commands through Hermes' real human-approval pipeline.

* ``pre_tool_call`` (for ``car_command``): refuse hopeless commands early, fail closed for unattended
  runs, and return ``{"action": "approve"}`` so Hermes asks a person — with a one-time rule key.
* ``post_approval_response``: records the person's actual answer against the ticket, so the tool can
  require *positive* human confirmation rather than assuming the prompt happened.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional

from ..brain.commands import ArgError, get_spec, validate_args
from ..brain.models import describe_state
from .approval import PATTERN_PREFIX

logger = logging.getLogger(__name__)

TOOL_NAME = "car_command"
BRIDGE_NAME = "tool_call"   # Hermes Tool Search bridge: {"calls": [{"name", "arguments"}, ...]}

BATCH_MESSAGE = ("pit-crew: send approval-needed car commands on their own, one per tool_call. "
                 "Nothing in this batch was sent.")
UNPARSEABLE_MESSAGE = ("pit-crew: could not read this tool_call batch, and it names car_command, so it was NOT sent. "
                       "Retry with {\"calls\":[{\"name\":\"car_command\",\"arguments\":{...}}]}.")


def _as_dict(value: Any) -> Optional[Dict[str, Any]]:
    """A dict, or a JSON-string dict; ``None`` -> {} ; anything else -> None (unreadable)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def unwrap_bridge_calls(args: Any) -> Optional[list]:
    """The ``[(name, arguments_or_None)]`` entries of a ``tool_call`` bridge invocation, mirroring Hermes'
    ``normalize_tool_call_entries`` (batch shape, legacy single shape, JSON-string envelope, bare dict).
    ``arguments`` is ``None`` when that entry's arguments cannot be read. ``None`` -> the envelope itself is unreadable."""
    args = _as_dict(args)
    if args is None:
        return None
    raw = args.get("calls")
    if raw is None:
        raw = [{"name": args.get("name"), "arguments": args.get("arguments")}]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return None
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            return None
        out.append((str(entry.get("name") or "").strip(), _as_dict(entry.get("arguments"))))
    return out


def split_tool_args(args: Optional[Dict[str, Any]]) -> tuple:
    """(command, raw_args, reason) from the ``car_command`` tool arguments."""
    args = dict(args or {})
    command = str(args.pop("command", "") or "").strip().lower()
    reason = str(args.pop("reason", "") or "")
    inner = args.pop("args", None)
    raw = dict(inner) if isinstance(inner, dict) else {}
    for k, v in args.items():  # tolerate models that put arguments at the top level
        raw.setdefault(k, v)
    return command, raw, reason


class ApprovalGate:
    def __init__(self, get_service: Callable[[], Any]) -> None:
        self._get = get_service

    # -- pre_tool_call ----------------------------------------------------------------------
    def pre_tool_call(self, tool_name: str = "", args: Optional[Dict[str, Any]] = None, task_id: str = "",
                      session_id: str = "", **kwargs: Any) -> Optional[Dict[str, Any]]:
        if tool_name == BRIDGE_NAME:
            return self._bridge(args, task_id, session_id)
        if tool_name != TOOL_NAME:
            return None
        try:
            return self._decide(args, task_id, session_id)
        except Exception as exc:
            # A policy hook that cannot decide must fail CLOSED for approval-class commands.
            logger.exception("pit-crew approval gate crashed")
            command, _raw, _reason = split_tool_args(args)
            spec = get_spec(command)
            if spec is not None and spec.requires_approval:
                return {"action": "block",
                        "message": f"pit-crew safety gate error ({type(exc).__name__}); command not sent."}
            return None

    def _bridge(self, args: Any, task_id: str, session_id: str) -> Optional[Dict[str, Any]]:
        """Hermes Tool Search defers plugin tools behind ``tool_call`` and fires ``pre_tool_call`` only for the
        bridge name, so the gate has to look inside. Fails closed for anything approval-class it cannot read."""
        try:
            entries = unwrap_bridge_calls(args)
            if entries is None:
                # Cannot even read the envelope: block only if it plausibly names car_command.
                return {"action": "block", "message": UNPARSEABLE_MESSAGE} if TOOL_NAME in str(args) else None
            mine = [(n, a) for n, a in entries if n == TOOL_NAME]
            if not mine:
                return None
            if any(a is None for _n, a in mine):
                return {"action": "block", "message": UNPARSEABLE_MESSAGE}
            if len(entries) == 1:
                return self._decide(mine[0][1], task_id, session_id)
            for _n, a in mine:
                if self._needs_approval(a):
                    return {"action": "block", "message": BATCH_MESSAGE}
            return None
        except Exception as exc:
            logger.exception("pit-crew approval gate crashed (bridge)")
            return {"action": "block",
                    "message": f"pit-crew safety gate error ({type(exc).__name__}); command not sent."}

    def _needs_approval(self, args: Dict[str, Any]) -> bool:
        """Same conditions as ``_decide`` under which a human prompt (or an unattended block) would apply."""
        command, raw, _reason = split_tool_args(args)
        spec = get_spec(command)
        if spec is None or not spec.requires_approval:
            return False
        try:
            validate_args(spec, raw)
        except ArgError:
            return False
        return bool(self._get().armed_info()["armed"])

    def _decide(self, args: Optional[Dict[str, Any]], task_id: str, session_id: str) -> Optional[Dict[str, Any]]:
        svc = self._get()
        command, raw, reason = split_tool_args(args)
        spec = get_spec(command)
        if spec is None or not spec.requires_approval:
            return None  # unknown -> the tool reports it; safe-direction/comfort -> no human needed
        try:
            clean = validate_args(spec, raw)
        except ArgError:
            return None  # the tool will return the validation error; nothing is executed
        if not svc.armed_info()["armed"]:
            return None  # dry run: nothing is sent, so nothing to approve
        if svc.is_unattended():
            svc.engine.record_block(command=spec.name, args=clean, surface="tool", session=session_id or task_id,
                                    reason="approval-class command in an unattended run", approval="unattended")
            return {"action": "block",
                    "message": (f"pit-crew: '{spec.name}' needs a human's approval and this is a scheduled/"
                                "unattended run, so it was NOT sent. Tell the user instead.")}
        pre = svc.engine.preflight(spec.name, raw)
        if not pre["ok"]:
            svc.engine.record_block(command=spec.name, args=clean, surface="tool", session=session_id or task_id,
                                    reason=pre["message"])
            return {"action": "block", "message": f"pit-crew safety policy refused '{spec.name}': {pre['message']}."}
        ticket = svc.broker.issue(spec.name, clean, task_id)
        state = pre["state"]
        lines = [f"Pit Crew wants to send: {spec.name.upper().replace('_', ' ')}"
                 + (f" {', '.join(f'{k}={v}' for k, v in clean.items() if k not in ('override_zone', 'wake_ok'))}" if clean else ""),
                 f"Car: {describe_state(state, svc.now())}"]
        if reason:
            shown = " ".join(str(reason).split())[:120]
            lines.append(f"Model's stated reason (unverified): {shown}")
        for w in pre["policy"].warnings:
            lines.append(f"WARNING: {w}")
        lines.append("Approve only if you asked for this.")
        return {"action": "approve", "message": "\n".join(lines), "rule_key": svc.broker.rule_key(ticket)}

    # -- post_approval_response ----------------------------------------------------------------
    def post_approval_response(self, pattern_key: str = "", choice: str = "", **kwargs: Any) -> None:
        if not str(pattern_key).startswith(PATTERN_PREFIX):
            return
        try:
            self._get().broker.resolve(str(pattern_key), str(choice))
        except Exception:
            logger.exception("failed to record approval response")
