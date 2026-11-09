"""Getting alerts to the user.

Three layers, strongest last:

1. **Stored** — every alert is in the plugin DB (Desktop feed, ``/car alerts``, ``car_alerts``).
2. **Next turn** — a ``pre_llm_call`` context injection hands unseen alerts to the model the next time the user
   talks to Hermes (no permissions needed).
3. **Push** — ``ctx.inject_message(session_key=…)`` into the chat bound with ``/car notify here`` so the agent
   proactively tells the user. Needs ``allow_gateway_injection: true`` for this plugin (documented).

Why ``inject_message``: ``post_message`` only works inside a live turn, and cron delivery is job-scoped; a
free-running watcher has neither, but a bound session key is exactly the "user's chat" Hermes can route to.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..redact import scrub

logger = logging.getLogger(__name__)

PUSH_PREFIX = (
    "[Automated car alert from the pit-crew watcher — not typed by the user. Tell the user about it plainly in one or "
    "two sentences, mention the suggested action as an offer, and do NOT send any car command until they say yes.]"
)


_MAX_LINE = 400


def _one_line(text: Any, limit: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[: limit - 1] + "…"


def format_alert_line(a: Dict[str, Any]) -> str:
    line = f"- {_one_line(a['title'], 120)}: {_one_line(a['body'], 220)} Suggested: {_one_line(a['suggestion'], 120)}"
    return line[:_MAX_LINE]


def format_push(alerts: List[Dict[str, Any]]) -> str:
    return PUSH_PREFIX + "\n" + "\n".join(format_alert_line(a) for a in alerts)


def format_context(alerts: List[Dict[str, Any]]) -> str:
    return ("Car alerts the watcher raised that the user has not seen yet (mention briefly if relevant, "
            "offer the suggested action, don't run it without a yes):\n" + "\n".join(format_alert_line(a) for a in alerts))


class Deliverer:
    def __init__(self, service: Any) -> None:
        self._svc = service

    # -- binding ----------------------------------------------------------------------------
    def bind(self, session_key: str, platform: str) -> Dict[str, Any]:
        target = {"session_key": session_key or "", "platform": platform or "", "bound_at": self._svc._wall()}
        self._svc.store.kv_set("notify_target", target)
        return target

    def unbind(self) -> None:
        self._svc.store.kv_delete("notify_target")

    def target(self) -> Optional[Dict[str, Any]]:
        return self._svc.store.kv_get("notify_target")

    def status(self) -> Dict[str, Any]:
        t = self.target()
        last = self._svc.store.kv_get("notify_last", {}) or {}
        return {"bound": t is not None, "platform": (t or {}).get("platform"),
                "push_enabled": bool(self._svc.settings.get("alert_push")), "last_push": last}

    # -- pushing ----------------------------------------------------------------------------
    undeliverable = False   # set when the last attempt had a bound chat but this process could not inject into it

    def push_pending(self) -> bool:
        """Push every still-``new`` non-info alert. Safe to call from any process: each alert is claimed atomically
        (new -> pushing), so two processes never push the same one, and a failed push puts it back to ``new``."""
        rows = [a for a in self._svc.store.alerts_list(("new",), limit=20) if a.get("severity") != "info"]
        return self.push(list(reversed(rows))) if rows else False

    def push(self, alerts: List[Dict[str, Any]]) -> bool:
        """Push ``alerts`` (dicts with id/title/body/suggestion/severity). Returns True if a host accepted it."""
        svc = self._svc
        self.undeliverable = False
        pushable = [a for a in alerts if a.get("severity") != "info"]
        if not pushable or not svc.settings.get("alert_push"):
            return False
        target = self.target()
        ctx = svc.ctx
        if target is None or ctx is None:
            return False
        claimed = [a for a in pushable if svc.store.alert_claim_push(int(a["id"]), svc._wall())]
        if not claimed:
            return False
        text = scrub(format_push(claimed))
        try:
            key = target.get("session_key") or None
            ok = bool(ctx.inject_message(text, session_key=key) if key else ctx.inject_message(text))
        except Exception as exc:
            logger.warning("alert push failed: %s", type(exc).__name__)
            ok = False
        if ok:
            for a in claimed:
                svc.store.alert_transition(int(a["id"]), "surfaced", ("pushing",))   # not if it was resolved/acked meanwhile
        else:
            self.undeliverable = True
            for a in claimed:
                svc.store.alert_unclaim_push(int(a["id"]))   # still 'new': the next turn's context will carry it
        svc.store.kv_set("notify_last", {"at": svc._wall(), "ok": ok, "count": len(claimed),
                                         "hint": None if ok else (
                                             "This Hermes process could not deliver the alert (no live gateway here, "
                                             "or Hermes refused it). The gateway process delivers it when it can; "
                                             "if alerts never reach your chat, check that the gateway is running and "
                                             "plugins.entries.pit-crew.allow_gateway_injection is true in config.yaml, "
                                             "then run `/car notify here` again.")})
        return ok

    # -- next-turn context -------------------------------------------------------------------
    def context_for_next_turn(self, limit: int = 5) -> Optional[str]:
        rows = self._svc.store.alerts_list(("new",), limit=limit)
        if not rows:
            return None
        for r in rows:
            self._svc.store.alert_transition(int(r["id"]), "surfaced", ("new",))
        return scrub(format_context(list(reversed(rows))))
