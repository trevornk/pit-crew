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
from typing import Any, Dict, List, Optional, Tuple

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


UNDELIVERABLE_HINT = ("This Hermes process could not deliver the alert (no live gateway here, "
                      "or Hermes refused it). The gateway process delivers it when it can; "
                      "if alerts never reach your chat, check that the gateway is running and "
                      "plugins.entries.pit-crew.allow_gateway_injection is true in config.yaml, "
                      "then run `/car notify here` again.")
NOT_ROUTED_HINT = ("If it does not appear, check the gateway log (a line \"Plugin message injection was not routed\" means the gateway "
                   "accepted the call but had no matching live chat session or delivery adapter for the bound chat, or was "
                   "shutting down). Run `/car notify here` again in the chat you want.")
TEST_LINE = "Pit Crew test alert: delivery works"
TEST_MESSAGE = (
    "[Automated test of car-alert delivery from the pit-crew plugin \u2014 not typed by the user. Tell the user in one "
    "short sentence that the test arrived. There is no real alert and nothing to do; do NOT send any car command.]\n"
    + TEST_LINE)


#: pushes are retried with backoff (30 s, 1, 2, 4 ... min, capped at 30 min) until this long after the alert was raised; after that the
#: alert stays in the store / next-turn context / `/car alerts` and status says it was given up on
PUSH_GIVEUP_S = 6 * 3600.0
PUSH_BACKOFF_BASE_S = 30.0
PUSH_BACKOFF_MAX_S = 1800.0

#: Hermes' own words for why ``inject_message`` returned False, mapped to what it means for the user. The core logs
#: these at WARNING on the ``hermes_cli.plugins`` logger and returns a bare ``False``; Pit Crew listens for them.
_REASONS = (
    ("no live gateway is available",
     "this Hermes process is not the messaging gateway (it has no gateway to hand the message to: typically the Desktop "
     "app or a CLI session, not the process that runs your Matrix/Telegram/... chats)"),
    ("gateway injection denied",
     "Hermes denied it: plugins.entries.pit-crew.allow_gateway_injection is not true for this profile"),
    ("requires an existing session_key",
     "Hermes needs a bound chat (session key) and none was given"),
    ("gateway scheduling failed", "the gateway failed while scheduling the message"),
    ("TUI scheduling failed", "the Desktop/TUI host failed while scheduling the message"),
)


class _CaptureWarnings(logging.Handler):
    """Collect Hermes' WARNING lines from the plugin-injection logger during one inject call (message text only)."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.append(record.getMessage())
        except Exception:      # noqa: BLE001 - logging must never break delivery
            pass


def inject(ctx: Any, text: str, session_key: Optional[str]) -> Tuple[bool, str]:
    """Call ``ctx.inject_message`` and say WHY when it fails: ``(ok, reason)``. ``reason`` is scrubbed plain text built from
    the exception type or Hermes' own logged refusal, never the message or the session key."""
    if not session_key:
        # Key-less injection is on Hermes' deprecation track (plugins docs: "may be retired later"); never rely on it.
        return False, ("no chat session is bound (Pit Crew needs this chat's session key). Run `/car notify here` in the "
                       "chat you want the alerts in")
    cap = _CaptureWarnings()
    lg = logging.getLogger("hermes_cli.plugins")
    lg.addHandler(cap)
    try:
        ok = bool(ctx.inject_message(text, session_key=session_key))
        exc_note = ""
    except Exception as exc:                        # noqa: BLE001
        ok, exc_note = False, f"inject_message raised {type(exc).__name__}"
    finally:
        lg.removeHandler(cap)
    if ok:
        return True, ""
    joined = " | ".join(cap.messages)
    for needle, meaning in _REASONS:
        if needle in joined:
            return False, meaning
    if exc_note:
        return False, exc_note
    return False, ("Hermes returned False without saying why (the session may not exist in the gateway, belong to another "
                   "profile, or no longer be authorised; the gateway log has the detail)")


class Deliverer:
    def __init__(self, service: Any) -> None:
        self._svc = service

    # -- binding ----------------------------------------------------------------------------
    def bind(self, session_key: str, platform: str) -> Optional[Dict[str, Any]]:
        """Bind pushes to this chat. Refused (``None``) without a session key: Hermes routes keyed injections only."""
        if not (session_key or "").strip():
            return None
        target = {"session_key": session_key or "", "platform": platform or "", "bound_at": self._svc._wall()}
        self._svc.store.kv_set("notify_target", target)
        return target

    def unbind(self) -> None:
        self._svc.store.kv_delete("notify_target")

    def target(self) -> Optional[Dict[str, Any]]:
        return self._svc.store.kv_get("notify_target")

    def status(self) -> Dict[str, Any]:
        svc = self._svc
        t = self.target()
        last = svc.store.kv_get("notify_last", {}) or {}
        pending = svc.store.alerts_push_summary(svc._wall(), PUSH_GIVEUP_S) if t is not None else {"waiting": 0, "gave_up": 0}
        return {"bound": t is not None, "platform": (t or {}).get("platform"),
                "push_enabled": bool(svc.settings.get("alert_push")), "last_push": last,
                "waiting_for_push": pending["waiting"], "gave_up": pending["gave_up"]}

    # -- pushing ----------------------------------------------------------------------------
    undeliverable = False   # set when the last attempt had a bound chat but this process could not inject into it

    def push_pending(self) -> bool:
        """Push every alert still owed a push (``pushed_ts`` empty; ``new`` OR ``surfaced`` -- showing it in some chat's next
        turn does not count as delivered to the bound chat). Safe to call from any process: each alert is claimed atomically,
        two processes never push the same one, and a failed push is retried later with backoff."""
        svc = self._svc
        if self.target() is None:
            return False
        rows = svc.store.alerts_for_push(svc._wall(), PUSH_GIVEUP_S)
        return self.push(list(reversed(rows))) if rows else False

    @staticmethod
    def _backoff(attempts: int) -> float:
        return min(PUSH_BACKOFF_MAX_S, PUSH_BACKOFF_BASE_S * (2 ** max(0, attempts)))

    def push(self, alerts: List[Dict[str, Any]]) -> bool:
        """Push ``alerts`` (dicts with id/title/body/suggestion/severity). Returns True if a host ACCEPTED it. Hermes routes
        the message asynchronously and only logs an unroutable key, so accepted is not the same as arrived."""
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
        ok, reason = inject(ctx, text, target.get("session_key") or "")
        if not ok:
            logger.warning("alert push failed: %s", reason)
        now = svc._wall()
        if ok:
            for a in claimed:
                svc.store.alert_push_done(int(a["id"]), now)
        else:
            self.undeliverable = True
            for a in claimed:
                svc.store.alert_push_failed(int(a["id"]), now + self._backoff(int(a.get("push_attempts") or 0)), reason)
        svc.store.kv_set("notify_last", {"at": now, "ok": ok, "accepted_by_hermes": ok, "count": len(claimed), "reason": scrub(reason) if reason else None,
                                         "hint": None if ok else UNDELIVERABLE_HINT + " Reason: " + scrub(reason) + "."})
        return ok

    def send_test(self) -> Dict[str, Any]:
        """Push a clearly labelled test line through the SAME inject call real alerts use, to the bound target.
        Creates no alert row and touches no rule, dedupe or ``notify_last`` state."""
        svc = self._svc
        target = self.target()
        if target is None:
            return {"ok": False, "reason": "not_bound", "hint": "No chat is bound for pushed alerts. Run `/car notify here` in the chat you want."}
        ctx = svc.ctx
        if ctx is None:
            return {"ok": False, "reason": "no_context", "hint": UNDELIVERABLE_HINT}
        ok, why = inject(ctx, TEST_MESSAGE, target.get("session_key") or "")
        if not ok:
            logger.warning("test alert push failed: %s", why)
        return {"ok": ok, "reason": None if ok else "refused", "why": scrub(why) if why else None,
                "hint": None if ok else UNDELIVERABLE_HINT + " Reason: " + scrub(why) + ".",
                "push_enabled": bool(svc.settings.get("alert_push"))}

    # -- next-turn context -------------------------------------------------------------------
    def context_for_next_turn(self, limit: int = 5) -> Optional[str]:
        rows = self._svc.store.alerts_list(("new",), limit=limit)
        if not rows:
            return None
        for r in rows:
            self._svc.store.alert_transition(int(r["id"]), "surfaced", ("new",))
        return scrub(format_context(list(reversed(rows))))
