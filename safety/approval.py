"""Approval tickets: tie a *human decision made in Hermes' approval pipeline* to one tool call.

Flow for a model-initiated approval-class command:

1. ``pre_tool_call`` hook -> :meth:`ApprovalBroker.issue` creates a ticket with a random nonce and the
   hook returns ``{"action": "approve", "rule_key": "pit-crew:<command>:<nonce>"}``. Hermes shows its
   normal approval prompt (CLI, Desktop, Telegram…). The nonce in the rule key means "always allow"
   can never turn into a standing permission: that key will never occur again.
2. ``post_approval_response`` hook -> :meth:`resolve` marks the ticket confirmed (human chose
   once/session/always) or denied.
3. The tool handler -> :meth:`take` consumes the ticket. In ``strict`` mode only a *confirmed* ticket
   counts, so a Hermes configuration that skips the prompt (YOLO / approvals off / cron auto-approve)
   cannot silently unlock the car.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..brain.commands import canonical_args

RULE_PREFIX = "pit-crew"
PATTERN_PREFIX = "plugin_rule:" + RULE_PREFIX + ":"
TICKET_TTL_S = 600.0
CONFIRMED_VALID_S = 120.0      # a human "yes" is for the call that is about to run, not for a later one
APPROVING_CHOICES = ("once", "session", "always")


@dataclass
class Ticket:
    nonce: str
    command: str
    digest: str
    task_id: str
    issued_at: float
    state: str = "pending"  # pending | confirmed | denied | consumed
    choice: str = ""
    decided_at: float = 0.0
    seq: int = 0              # issue order (timestamps can tie)


@dataclass
class ApprovalEvidence:
    """What the safety engine accepts as proof that a human approved this exact command."""

    kind: str  # always "hermes_approval" (a ticket from Hermes' own approval pipeline); the engine requires it
    detail: str = ""
    denied: bool = False


class ApprovalBroker:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._tickets: Dict[str, Ticket] = {}
        self._seq = 0

    def _gc(self) -> None:
        cutoff = self._clock() - TICKET_TTL_S
        for nonce in [n for n, t in self._tickets.items() if t.issued_at < cutoff]:
            del self._tickets[nonce]

    def issue(self, command: str, args: dict, task_id: str = "") -> Ticket:
        ticket = Ticket(nonce=secrets.token_hex(8), command=command, digest=canonical_args({"c": command, **args}),
                        task_id=task_id or "", issued_at=self._clock())
        with self._lock:
            self._gc()
            self._seq += 1
            ticket.seq = self._seq
            self._tickets[ticket.nonce] = ticket
        return ticket

    @staticmethod
    def rule_key(ticket: Ticket) -> str:
        return f"{RULE_PREFIX}:{ticket.command}:{ticket.nonce}"

    def resolve(self, pattern_key: str, choice: str) -> bool:
        """Record the human's decision for a ``post_approval_response``; ignores foreign keys."""
        if not pattern_key.startswith(PATTERN_PREFIX):
            return False
        nonce = pattern_key.rsplit(":", 1)[-1]
        with self._lock:
            ticket = self._tickets.get(nonce)
            if ticket is None or ticket.state != "pending":
                return False
            ticket.choice = choice
            ticket.decided_at = self._clock()
            ticket.state = "confirmed" if choice in APPROVING_CHOICES else "denied"
            return True

    def take(self, command: str, args: dict, task_id: str = "", strict: bool = True) -> Optional[ApprovalEvidence]:
        """Consume the newest matching ticket and return evidence, or ``None`` if there is none.

        ``strict``: only a ticket the human positively confirmed counts. Not strict: a ticket whose
        prompt Hermes passed (pending) also counts — we then rely on Hermes' own gate.
        """
        digest = canonical_args({"c": command, **args})
        with self._lock:
            self._gc()
            candidates: List[Ticket] = [
                t for t in self._tickets.values()
                if t.digest == digest and t.state != "consumed"
                and (not task_id or not t.task_id or t.task_id == task_id)]
            candidates.sort(key=lambda t: (t.issued_at, t.seq), reverse=True)
            # Only the NEWEST matching ticket can speak for this call. Falling through to an older confirmed one let a
            # call whose own prompt never happened (pending) ride on an approval given to an earlier call that never ran.
            for t in candidates[:1]:
                if t.state == "denied":
                    t.state = "consumed"
                    return ApprovalEvidence("hermes_approval", f"declined ({t.choice})", denied=True)
                if t.state == "confirmed" and self._clock() - (t.decided_at or t.issued_at) > CONFIRMED_VALID_S:
                    return None
                if t.state == "confirmed" or (t.state == "pending" and not strict):
                    t.state = "consumed"
                    return ApprovalEvidence("hermes_approval", t.choice or "gate passed")
            return None

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._tickets.values() if t.state == "pending")
