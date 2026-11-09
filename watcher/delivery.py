"""Getting alerts to the user.

Three layers, strongest last:

1. **Stored** — every alert is in the plugin DB (Desktop feed, ``/car alerts``, ``car_alerts``).
2. **Next turn** — a ``pre_llm_call`` context injection hands unseen alerts to the model the next time the user
   talks to Hermes (no permissions needed). This is also the only layer a Desktop/CLI-only user with no messaging
   platform gets.
3. **Push** — Hermes' own cross-platform send (``tools.send_message_tool.send_message_tool``, the function behind
   cron ``deliver=``, ``hermes send`` and the kanban notifier) to a *delivery target* such as ``telegram``,
   ``discord:#car`` or ``matrix:!room:server``. A plain message to the platform: no agent turn, no gateway session
   entry, no live gateway needed in this process.

Why not ``ctx.inject_message``: it needs a live messaging gateway *in the calling process*, a gateway session-store entry
for the key (a room Hermes posts to daily from cron had none: "not routed"), and it starts an agent turn in that chat for
every alert. See docs/DECISIONS.md.

Why not ``ctx.dispatch_tool("send_message", ...)``: ``send_message`` is intentionally NOT a registered tool in Hermes
(tools/send_message_tool.py header; toolsets.py "no agent-callable send_message tool"), so the registry answers
"Unknown tool". Callers import ``send_message_tool`` directly, as ``hermes send`` does (hermes_cli/send_cmd.py).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from ..redact import scrub

logger = logging.getLogger(__name__)

PUSH_HEADER = "Pit Crew car alert"


_MAX_LINE = 400


def _one_line(text: Any, limit: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[: limit - 1] + "…"


def format_alert_line(a: Dict[str, Any]) -> str:
    line = f"- {_one_line(a['title'], 120)}: {_one_line(a['body'], 220)} Suggested: {_one_line(a['suggestion'], 120)}"
    return line[:_MAX_LINE]


def format_push(alerts: List[Dict[str, Any]]) -> str:
    """A plain message for a person (it goes to the platform as-is; no agent reads it)."""
    n = len(alerts)
    return (f"{PUSH_HEADER}{'s' if n != 1 else ''}:\n" + "\n".join(format_alert_line(a) for a in alerts)
            + "\n(Ask Hermes to act on it; nothing is sent to the car until you say yes.)")


def format_context(alerts: List[Dict[str, Any]]) -> str:
    return ("Car alerts the watcher raised that the user has not seen yet (mention briefly if relevant, "
            "offer the suggested action, don't run it without a yes):\n" + "\n".join(format_alert_line(a) for a in alerts))


NO_TARGET_HINT = ("No place is set for pushed alerts. Run `/car notify here` in the chat you want, or "
                  "`/car notify telegram` / `discord:#car` / `matrix:!room:server` (same as a cron job's deliver), "
                  "or set alert_target in the plugin settings. Until then alerts are in `/car alerts` and mentioned on your next turn.")
UNAVAILABLE_HINT = ("This Hermes cannot do a cross-platform send from here (tools.send_message_tool is not importable), so "
                    "pushes cannot be delivered; alerts stay in `/car alerts` and are mentioned on your next turn.")
TEST_LINE = "Pit Crew test alert: delivery works"
TEST_MESSAGE = TEST_LINE + ". There is no real alert and nothing to do."

#: pushes are retried with backoff (30 s, 1, 2, 4 ... min, capped at 30 min) until this long after the alert was raised; after that the
#: alert stays in the store / next-turn context / `/car alerts` and status says it was given up on
PUSH_GIVEUP_S = 6 * 3600.0
PUSH_BACKOFF_BASE_S = 30.0
PUSH_BACKOFF_MAX_S = 1800.0

#: error kinds: only these mean "this PROCESS cannot send" (so the watcher lease is handed to one that can); a wrong target or
#: a platform that is down fails the same way everywhere
KIND_UNAVAILABLE, KIND_SCOPE, KIND_REFUSED = "unavailable", "scope", "refused"
_TARGET_RE = re.compile(r"^[a-z][a-z0-9_-]*(:\S+)?$")
_LOCAL_SURFACES = ("", "cli", "tui", "desktop", "local", "acp", "api_server", "codex", "gateway", "kanban", "tool", "webhook",
                   "msgraph_webhook", "cron")


def valid_target(text: str) -> bool:
    """Shape check only (``platform`` or ``platform:ref``). Hermes resolves the rest when it sends."""
    return bool(_TARGET_RE.match((text or "").strip()))


def hermes_send(target: str, text: str) -> Tuple[bool, str, Optional[str]]:
    """Send ``text`` to ``target`` through Hermes' own cross-platform send. Returns ``(ok, reason, kind)``.

    ``ok`` is the platform sender's own answer: ``send_message_tool`` returns ``{"success": true, ...}`` once the
    platform API accepted the message, or ``{"error": ...}``. That is delivery to the platform, not proof a person read it.
    Runs in the caller's context, so the secret scope the watcher polled the car in is the scope the platform tokens are
    read in (``agent.secret_scope.get_secret`` / ``gateway.config._getenv``)."""
    try:
        from tools.send_message_tool import send_message_tool
    except Exception as exc:                                    # noqa: BLE001 - not running inside a Hermes with the tool
        return False, f"send_message is not available ({type(exc).__name__})", KIND_UNAVAILABLE
    try:
        raw = send_message_tool({"action": "send", "target": target, "message": text})
    except Exception as exc:                                    # noqa: BLE001
        kind = KIND_SCOPE if any(c.__name__ == "UnscopedSecretError" for c in type(exc).__mro__) else KIND_REFUSED
        return False, f"send_message raised {type(exc).__name__}", kind
    try:
        res = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except ValueError:
        return False, "send_message returned something that is not JSON", KIND_REFUSED
    if not isinstance(res, dict):
        return False, "send_message returned an unexpected result", KIND_REFUSED
    if res.get("success") is True and not res.get("error"):
        return True, "", None
    err = _one_line(res.get("error") or "send_message reported failure without saying why", 300)
    kind = KIND_SCOPE if "secret scope" in err.lower() or "UnscopedSecret" in err else KIND_REFUSED
    return False, scrub(err), kind


def list_targets() -> Optional[str]:
    """The targets Hermes can send to (``send_message``'s own ``list``), or None when unavailable."""
    try:
        from tools.send_message_tool import send_message_tool
        res = json.loads(send_message_tool({"action": "list"}))
    except Exception:                                           # noqa: BLE001
        return None
    t = res.get("targets") if isinstance(res, dict) else None
    return _one_line(t, 1500) if t else None


def session_env(name: str) -> str:
    """One of Hermes' session context variables for the running command ('' when there is no gateway session context)."""
    try:
        from gateway.session_context import get_session_env
        return str(get_session_env(name, "") or "")
    except Exception:                                           # noqa: BLE001
        return ""


def target_from_session(platform: str = "") -> Tuple[Optional[str], str]:
    """The delivery target of the chat a slash command runs in, as ``(target, why_not)``.

    Read from Hermes' session context variables (``gateway.session_context.get_session_env``: HERMES_SESSION_PLATFORM /
    _CHAT_ID / _THREAD_ID, bound for plugin command handlers by ``GatewayRunner._session_env_scope``), NOT parsed out of the
    session key: Hermes' own key parser (``gateway/run.py _parse_session_key``) splits on ``:`` and cannot hold a Matrix
    room id (``!room:server``). Format is the ``send_message`` target syntax, ``platform:chat_id[:thread_id]``; Matrix
    gets no thread suffix because its room id already contains ``:``."""
    plat = (session_env("HERMES_SESSION_PLATFORM") or platform or "").strip().lower()
    chat = session_env("HERMES_SESSION_CHAT_ID").strip()
    thread = session_env("HERMES_SESSION_THREAD_ID").strip()
    if plat in _LOCAL_SURFACES:
        return None, "this is a Desktop/CLI session, not a messaging chat"
    if not chat:
        return None, f"Hermes did not tell me which {plat} chat this is"
    target = f"{plat}:{chat}"
    if thread and plat != "matrix":
        target += f":{thread}"
    return target, ""


class Deliverer:
    def __init__(self, service: Any) -> None:
        self._svc = service

    # -- where alerts go --------------------------------------------------------------------
    # Rule (one, documented): the plugin setting ``alert_target`` wins when it is non-empty; otherwise the target stored by
    # ``/car notify here`` or ``/car notify <target>``. The setting is the visible, config-file way; the command is the
    # convenient way for people who never open settings.
    def bind(self, target: str, how: str = "command") -> Optional[Dict[str, Any]]:
        """Store a delivery target. Refused (``None``) when it is not ``platform`` / ``platform:ref`` shaped."""
        target = (target or "").strip()
        if not valid_target(target):
            return None
        t = {"target": target, "how": how, "bound_at": self._svc._wall()}
        self._svc.store.kv_set("notify_target", t)
        return t

    def unbind(self) -> None:
        self._svc.store.kv_delete("notify_target")

    def legacy_binding(self) -> bool:
        """An old ``/car notify here`` binding (session key, from the inject_message days) that cannot be turned into a target."""
        t = self._svc.store.kv_get("notify_target")
        return bool(t) and not t.get("target")

    def target(self) -> Optional[Dict[str, Any]]:
        configured = str(self._svc.settings.get("alert_target") or "").strip()
        if configured:
            return {"target": configured, "source": "setting"}
        t = self._svc.store.kv_get("notify_target")
        if t and t.get("target"):
            return {"target": t["target"], "source": "command", "bound_at": t.get("bound_at")}
        return None

    def status(self) -> Dict[str, Any]:
        svc = self._svc
        t = self.target()
        last = svc.store.kv_get("notify_last", {}) or {}
        pending = svc.store.alerts_push_summary(svc._wall(), PUSH_GIVEUP_S) if t is not None else {"waiting": 0, "gave_up": 0}
        return {"bound": t is not None, "target": (t or {}).get("target"), "source": (t or {}).get("source"),
                "legacy_binding": self.legacy_binding(),
                "push_enabled": bool(svc.settings.get("alert_push")), "last_push": last,
                "waiting_for_push": pending["waiting"], "gave_up": pending["gave_up"]}

    # -- pushing ----------------------------------------------------------------------------
    undeliverable = False   # set when the last attempt failed because THIS process cannot send (a different process may)

    def push_pending(self) -> bool:
        """Push every alert still owed a push (``pushed_ts`` empty; ``new`` OR ``surfaced`` -- showing it in some chat's next
        turn does not count as delivered). Safe to call from any process: each alert is claimed atomically, two processes
        never push the same one, and a failed push is retried later with backoff."""
        svc = self._svc
        if self.target() is None:
            return False
        rows = svc.store.alerts_for_push(svc._wall(), PUSH_GIVEUP_S)
        return self.push(list(reversed(rows))) if rows else False

    @staticmethod
    def _backoff(attempts: int) -> float:
        return min(PUSH_BACKOFF_MAX_S, PUSH_BACKOFF_BASE_S * (2 ** max(0, attempts)))

    def push(self, alerts: List[Dict[str, Any]]) -> bool:
        """Push ``alerts`` (dicts with id/title/body/suggestion/severity). True only if Hermes' send reported success, i.e. the
        platform accepted the message."""
        svc = self._svc
        self.undeliverable = False
        pushable = [a for a in alerts if a.get("severity") != "info"]
        if not pushable or not svc.settings.get("alert_push"):
            return False
        target = self.target()
        if target is None:
            return False
        claimed = [a for a in pushable if svc.store.alert_claim_push(int(a["id"]), svc._wall())]
        if not claimed:
            return False
        ok, reason, kind = hermes_send(target["target"], scrub(format_push(claimed)))
        if not ok:
            logger.warning("alert push failed: %s", reason)
        now = svc._wall()
        if ok:
            for a in claimed:
                svc.store.alert_push_done(int(a["id"]), now)
        else:
            self.undeliverable = kind in (KIND_UNAVAILABLE, KIND_SCOPE)
            for a in claimed:
                svc.store.alert_push_failed(int(a["id"]), now + self._backoff(int(a.get("push_attempts") or 0)), reason)
        svc.store.kv_set("notify_last", {"at": now, "ok": ok, "delivered": ok, "target": target["target"], "count": len(claimed),
                                         "reason": scrub(reason) if reason else None,
                                         "hint": None if ok else self.failure_hint(reason, kind)})
        return ok

    @staticmethod
    def failure_hint(reason: str, kind: Optional[str]) -> str:
        if kind == KIND_UNAVAILABLE:
            return UNAVAILABLE_HINT
        extra = (" This process has no profile secret scope for the platform's token; the gateway process can usually send."
                 if kind == KIND_SCOPE else "")
        return (f"Hermes could not deliver it: {scrub(reason)}.{extra} Check the target with `/car notify list` and that the "
                "platform is set up in Hermes (token and, for a bare platform name, its home channel). It will be retried.")

    def send_test(self) -> Dict[str, Any]:
        """Send a clearly labelled test line through the SAME path real alerts use, to the same target. Creates no alert row
        and touches no rule, dedupe or ``notify_last`` state."""
        svc = self._svc
        target = self.target()
        if target is None:
            return {"ok": False, "reason": "not_bound", "hint": NO_TARGET_HINT}
        ok, why, kind = hermes_send(target["target"], TEST_MESSAGE)
        if not ok:
            logger.warning("test alert push failed: %s", why)
        return {"ok": ok, "reason": None if ok else (kind or "refused"), "why": scrub(why) if why else None, "target": target["target"],
                "hint": None if ok else self.failure_hint(why, kind),
                "push_enabled": bool(svc.settings.get("alert_push"))}

    # -- next-turn context -------------------------------------------------------------------
    def context_for_next_turn(self, limit: int = 5) -> Optional[str]:
        rows = self._svc.store.alerts_list(("new",), limit=limit)
        if not rows:
            return None
        for r in rows:
            self._svc.store.alert_transition(int(r["id"]), "surfaced", ("new",))
        return scrub(format_context(list(reversed(rows))))
