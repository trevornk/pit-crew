"""Pit Crew — a proactive, safety-gated car agent for Hermes."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from . import schemas, slash, tools
from .blueprints import register_blueprints
from .constants import SLASH_COMMAND, TOOLSET
from .redact import HERMES_PATTERNS
from .safety.gate import ApprovalGate
from .service import attach_ctx, get_service
from .watcher.watcher import start_watcher

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).resolve().parent / "skills"


_context_failed_once = False


def _alert_context_hook(platform: str = "", **kwargs: Any) -> Optional[Dict[str, str]]:
    """pre_llm_call: hand the model any alerts the user hasn't seen, once — but only in turns a person reads
    (never in a scheduled/unattended run, which would swallow the alert)."""
    global _context_failed_once
    try:
        svc = get_service()
        if not svc.settings.get("alert_context") or platform == "cron" or svc.is_unattended():
            return None
        if svc.background_state:
            svc.watcher.schedule_turn_check()   # read-only, non-blocking, runs in a copy of this turn's secret scope
        text = svc.watcher.deliverer.context_for_next_turn()
        return {"context": text} if text else None
    except Exception:
        # Never break a turn over this; log the stack once, then just a short line.
        logger.warning("alert context injection failed", exc_info=not _context_failed_once)
        _context_failed_once = True
        return None


def register(ctx) -> None:
    """Wire tools, hooks, commands, blueprints, the bundled skill and the background watcher into Hermes."""
    attach_ctx(ctx)

    tool_specs = (
        ("car_status", schemas.CAR_STATUS, tools.car_status, "Live car state with freshness"),
        ("car_history", schemas.CAR_HISTORY, tools.car_history, "Trips, charges, efficiency and battery health"),
        ("car_readiness", schemas.CAR_READINESS, tools.car_readiness, "Will the car be ready for a departure?"),
        ("car_command", schemas.CAR_COMMAND, tools.car_command, "Send one safety-gated command to the car"),
        ("car_alerts", schemas.CAR_ALERTS, tools.car_alerts, "Read / acknowledge / check the watcher's alerts"),
        ("car_rules", schemas.CAR_RULES, tools.car_rules, "List, enable or tune the watcher's rules"),
    )
    for name, schema, handler, description in tool_specs:
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handler, description=description)

    # Approval pipeline: ask a human (via Hermes' real approval gate) before security/attention commands,
    # and record their actual answer so the tool can insist on positive confirmation.
    gate = ApprovalGate(get_service)
    ctx.register_hook("pre_tool_call", gate.pre_tool_call)
    ctx.register_hook("post_approval_response", gate.post_approval_response)
    ctx.register_hook("pre_llm_call", _alert_context_hook)

    ctx.register_command(SLASH_COMMAND, slash.handle, description="Your car: status, ready?, lock, warm up, alerts, go live",
                         args_hint="[ready|lock|warm|alerts|live|practice|log|notify|discover]")

    register_blueprints(ctx)

    if SKILLS_DIR.is_dir():
        for child in sorted(SKILLS_DIR.iterdir()):
            if (child / "SKILL.md").is_file():
                ctx.register_skill(child.name, child / "SKILL.md")

    # Mask VINs and Home Assistant tokens in transcripts and logs.
    ctx.register_redaction_patterns(HERMES_PATTERNS)

    # Background watcher (supervised task when a loop is available, else a daemon thread). It waits before
    # touching anything, so registration itself does no I/O.
    try:
        start_watcher(ctx, get_service)
    except Exception:
        logger.exception("could not start the pit-crew watcher; alerts will only run via car_alerts check_now")
    try:
        ctx.on_unload(lambda: get_service().close())
    except Exception:
        logger.debug("could not register the unload hook", exc_info=True)
