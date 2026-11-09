"""Hermes hooks that put approval-class commands through Hermes' real human-approval pipeline.

* ``pre_tool_call`` (for ``car_command``): refuse hopeless commands early, fail closed for unattended
  runs, and return ``{"action": "approve"}`` so Hermes asks a person — with a one-time rule key.
* ``post_approval_response``: records the person's actual answer against the ticket, so the tool can
  require *positive* human confirmation rather than assuming the prompt happened.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from ..brain.commands import ArgError, get_spec, validate_args
from ..brain.models import describe_state
from .approval import PATTERN_PREFIX

logger = logging.getLogger(__name__)

TOOL_NAME = "car_command"


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
