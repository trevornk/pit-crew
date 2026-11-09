"""Automation Blueprints — ready-made scheduled jobs (these create ordinary Hermes cron jobs)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

SKILL = "pit-crew:car-agent"


def register_blueprints(ctx: Any) -> int:
    """Register the car blueprints; returns how many Hermes accepted."""
    count = 0
    common_time = {"name": "time", "type": "time", "label": "What time?"}
    weekdays = {"name": "recurrence", "type": "weekdays", "label": "Which days?", "default": "weekdays"}
    deliver = {"name": "deliver", "type": "enum", "label": "Where to deliver?", "default": "origin",
               "options": ["origin", "local"], "strict": False}

    specs = [
        dict(
            key="morning-brief", title="Morning car brief", category="daily",
            description="A short weekday-morning brief: is the car ready for today, anything wrong, one suggested action.",
            schedule_template="{minute} {hour} * * {dow}",
            prompt_template=(
                "Give me a short morning car brief. First call car_status. If you have a calendar tool, find my first "
                "appointment today and its distance; then call car_readiness for that departure (otherwise for my usual "
                "departure). Then call car_alerts with action=check_now. Say plainly whether the car is ready, mention "
                "anything open/unlocked/not charging, and offer at most ONE suggested action. This is a read-only brief: "
                "do not send any car commands. If the car is a simulation, say so."),
            slots=[{**common_time, "default": "07:00"}, weekdays, deliver], tags=["car", "daily"]),
        dict(
            key="precondition", title="Precondition the car before departure", category="daily",
            description="Warm or cool the cabin shortly before you leave (a comfort command: runs unattended, within limits).",
            schedule_template="{minute} {hour} * * {dow}",
            prompt_template=(
                "Precondition my car for departure. Call car_status first. If the car is asleep, or is not plugged in "
                "and its battery is under 30%, do nothing and tell me why. Otherwise call car_command with "
                "command=climate_on and args {{\"temp_f\": {temp_f}}}. Report the result honestly using the result's "
                "status: `confirmed` = the car confirmed it; `sent_unconfirmed` = sent but not verified; `dry_run` = "
                "the car is in practice mode and nothing was sent. Never retry in a loop."),
            slots=[{**common_time, "default": "07:15"}, weekdays,
                   {"name": "temp_f", "type": "text", "label": "Target cabin temperature (°F)", "default": "70"}, deliver],
            tags=["car", "comfort"]),
        dict(
            key="nightly-security", title="Nightly car security check", category="daily",
            description="Every night: check for an unlocked car, open doors/windows/trunk, Sentry off away from home — and lock it if needed.",
            schedule_template="{minute} {hour} * * *",
            prompt_template=(
                "Run my nightly car security check. Call car_alerts with action=check_now and car_status. If the car is "
                "unlocked while at home (or any trusted place), call car_command with command=lock (locking is "
                "safe-direction and needs no approval) and report whether the car CONFIRMED it. Never unlock, and "
                "never open anything. If anything is open (doors, windows, trunk), or the car is unlocked away from "
                "home, tell me plainly. If everything is fine, say 'Car is locked and secure' in one line."),
            slots=[{**common_time, "default": "22:30"}, deliver], tags=["car", "security"]),
    ]
    for spec in specs:
        try:
            handle = ctx.register_automation_blueprint(
                spec["key"], title=spec["title"], description=spec["description"], category=spec["category"],
                schedule_template=spec["schedule_template"], prompt_template=spec["prompt_template"],
                slots=spec["slots"], deliver_default="origin", skills=(SKILL,), tags=tuple(spec["tags"]))
            count += 1 if handle is not None else 0
        except Exception as exc:
            logger.warning("could not register blueprint %s: %s", spec["key"], exc)
    return count
