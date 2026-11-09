"""Best-effort push of "something changed" to the Desktop page (it also polls, so this is only a nudge)."""

from __future__ import annotations

import logging

from .constants import PLUGIN_ID

logger = logging.getLogger(__name__)


def notify_ui(event: str, payload: dict | None = None) -> None:
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event  # documented public helper

        broadcast_plugin_event(PLUGIN_ID, event, payload or {})
    except Exception as exc:  # no Desktop attached / older Hermes: harmless
        logger.debug("ui event %s not delivered: %s", event, type(exc).__name__)
