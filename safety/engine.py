"""The safety engine: the single door through which every command to the car passes.

Order of operations (each step can stop the command, and every outcome is written to the audit log):

1. vocabulary + argument validation + adapter capability
2. read the car's state; run the policy (moving / stale / untrusted zone / asleep / bounds)
3. armed? If not: **dry run** — nothing is sent, the reply says so
4. rate limit and duplicate suppression (live only)
5. human approval for approval-class commands (`unlock`, trunk, frunk, vent, sentry off, honk, flash);
   unattended contexts fail closed
6. send
7. read-back: poll the car until it shows the change, or time out — and report exactly that
"""

from __future__ import annotations

import threading
import logging
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from ..adapters.base import AdapterError, AdapterUnavailable, SecretScopeUnavailable
from ..brain.commands import ArgError, CommandSpec, canonical_args, get_spec, validate_args
from ..brain.models import VehicleState
from ..brain.timeparse import fmt_clock
from ..redact import scrub
from . import policy as policy_mod
from .approval import ApprovalEvidence

logger = logging.getLogger(__name__)

APPROVAL_KIND = "hermes_approval"
POLICY_FLAGS = ("override_zone", "wake_ok")


class SafetyEngine:
    def __init__(self, service: Any) -> None:
        self._svc = service
        self._exec_lock = threading.RLock()   # R5: parallel car_command calls must not both pass the dedupe/rate checks

    # -- helpers --------------------------------------------------------------------------
    @staticmethod
    def _describe(spec: CommandSpec, args: Mapping[str, Any]) -> str:
        shown = {k: v for k, v in args.items() if k not in POLICY_FLAGS}
        bits = ", ".join(f"{k}={v}" for k, v in shown.items())
        return f"{spec.name}({bits})" if bits else spec.name

    def _audit(self, *, surface: str, session: str, command: str, args: Mapping[str, Any], risk: str, mode: str,
               policy: str, approval: str, status: str, detail: str, readback: str = "n/a",
               readback_ts: Optional[float] = None) -> int:
        try:
            return self._svc.store.audit_add(
                ts=self._svc._wall(), surface=surface, session=session, command=command, args=dict(args), risk=risk,
                mode=mode, policy=policy, approval=approval, status=status, detail=scrub(detail)[:500],
                readback=readback, readback_ts=readback_ts, adapter=self._svc.describe_adapter().get("kind", ""))
        except Exception:  # an audit failure must never be silent, but must not hide the outcome either
            logger.exception("failed to write audit log row")
            return 0

    def _result(self, *, ok: bool, status: str, command: str, message: str, spec: Optional[CommandSpec] = None,
                armed: bool = False, dry_run: bool = False, policy: Optional[policy_mod.PolicyResult] = None,
                approval: str = "n/a", readback: Optional[Dict[str, Any]] = None, audit_id: int = 0,
                **extra: Any) -> Dict[str, Any]:
        out: Dict[str, Any] = {"ok": ok, "status": status, "command": command,
                               "risk": spec.risk if spec else "unknown", "armed": armed, "dry_run": dry_run,
                               "message": scrub(message), "approval": approval, "audit_id": audit_id}
        if policy is not None:
            out["policy"] = policy.to_dict()
        if readback is not None:
            out["readback"] = readback
        out.update(extra)
        return out

    # -- recording refusals made outside execute() (e.g. by the approval hook) -----------------
    def record_block(self, *, command: str, args: Mapping[str, Any], surface: str, session: str, reason: str,
                     approval: str = "n/a", policy: str = "denied") -> int:
        spec = get_spec(command)
        return self._audit(surface=surface, session=session, command=command, args=args,
                           risk=spec.risk if spec else "unknown", mode="blocked", policy=policy, approval=approval,
                           status="blocked", detail=reason)

    # -- main entry -----------------------------------------------------------------------
    def preflight(self, command: str, raw_args: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        """Validate + policy-check without executing. Used by the approval hook and the Desktop dialog."""
        svc = self._svc
        spec = get_spec(command)
        if spec is None:
            return {"ok": False, "status": "invalid", "message": f"unknown command '{command}'"}
        try:
            args = validate_args(spec, raw_args)
        except ArgError as exc:
            return {"ok": False, "status": "invalid", "message": str(exc), "spec": spec}
        try:
            supported = svc.capabilities().can_do(spec.name)
            adapter_label = svc.adapter.label
        except AdapterError as exc:
            return {"ok": False, "status": "blocked", "spec": spec, "args": args,
                    "message": f"the car adapter is not available ({exc})"}
        if not supported:
            return {"ok": False, "status": "unsupported", "spec": spec, "args": args,
                    "message": f"{adapter_label} cannot perform '{spec.name}'"}
        try:
            state = svc.get_state(force=True)  # safety decisions never use a cached read
        except AdapterError as exc:
            return {"ok": False, "status": "blocked", "spec": spec, "args": args,
                    "message": f"cannot read the car right now ({exc})"}
        pol = policy_mod.evaluate(spec, args, state, svc.settings.snapshot(), svc.now(), svc.motion_hint())
        return {"ok": pol.allowed, "status": "ok" if pol.allowed else "blocked", "spec": spec, "args": args,
                "state": state, "policy": pol,
                "message": "" if pol.allowed else "; ".join(pol.denials)}

    def execute(self, command: str, raw_args: Optional[Mapping[str, Any]] = None, *, surface: str,
                session: str = "", reason: str = "", approval: Optional[ApprovalEvidence] = None,
                unattended: bool = False, force_dry_run: bool = False) -> Dict[str, Any]:
        with self._exec_lock:
            return self._execute(command, raw_args, surface=surface, session=session, reason=reason,
                                 approval=approval, unattended=unattended, force_dry_run=force_dry_run)

    def _execute(self, command: str, raw_args: Optional[Mapping[str, Any]] = None, *, surface: str,
                 session: str = "", reason: str = "", approval: Optional[ApprovalEvidence] = None,
                 unattended: bool = False, force_dry_run: bool = False) -> Dict[str, Any]:
        svc = self._svc
        settings = svc.settings.snapshot()
        pre = self.preflight(command, raw_args)
        spec: Optional[CommandSpec] = pre.get("spec")
        args: Dict[str, Any] = dict(pre.get("args") or {})
        armed_info = svc.armed_info()
        armed = bool(armed_info["armed"]) and not force_dry_run

        if pre["status"] in ("invalid", "unsupported"):
            aid = self._audit(surface=surface, session=session, command=command, args=args or dict(raw_args or {}),
                              risk=spec.risk if spec else "unknown", mode="blocked", policy="n/a", approval="n/a",
                              status=pre["status"], detail=pre["message"])
            return self._result(ok=False, status=pre["status"], command=command, spec=spec, armed=armed,
                                message=pre["message"], audit_id=aid)
        assert spec is not None
        pol: Optional[policy_mod.PolicyResult] = pre.get("policy")
        label = self._describe(spec, args)

        if pol is None or not pol.allowed:
            msg = pre["message"] or "blocked by the safety policy"
            aid = self._audit(surface=surface, session=session, command=spec.name, args=args, risk=spec.risk,
                              mode="blocked", policy="denied", approval="n/a", status="blocked", detail=msg)
            return self._result(ok=False, status="blocked", command=spec.name, spec=spec, armed=armed, policy=pol,
                                message=f"Refused: {msg}.", audit_id=aid)

        # -- dry run ------------------------------------------------------------------------
        if not armed:
            need = " It would need your approval in Hermes before being sent." if spec.requires_approval else ""
            why = "practice mode is on" if not force_dry_run else "a practice run was requested"
            msg = (f"PRACTICE RUN — {label.replace("_", " ")} passed all safety checks, but {why}, so the car was NOT contacted.{need} "
                   "To send real commands, go live (`/car live`, or the Go live button on the Pit Crew page).")
            aid = self._audit(surface=surface, session=session, command=spec.name, args=args, risk=spec.risk,
                              mode="dry_run", policy="allowed", approval="not_required" if not spec.requires_approval else "deferred",
                              status="dry_run", detail=reason or "dry run")
            return self._result(ok=True, status="dry_run", command=spec.name, spec=spec, armed=False, dry_run=True,
                                policy=pol, message=msg, audit_id=aid, would_send=label)

        # -- live: approval --------------------------------------------------------------------
        approval_status = "not_required"
        if spec.requires_approval:
            if unattended:
                return self._refuse_live(spec, args, surface, session, pol, armed, "unattended",
                                         "this needs a human's approval and no human can answer here "
                                         "(scheduled / unattended run), so it was not sent")
            if approval is None or approval.kind != APPROVAL_KIND:
                return self._refuse_live(spec, args, surface, session, pol, armed, "missing",
                                         "no confirmed human approval was found for this exact command, so it was "
                                         "not sent (fail closed)")
            if approval.denied:
                return self._refuse_live(spec, args, surface, session, pol, armed, "denied",
                                         f"the human declined ({approval.detail})")
            approval_status = "confirmed"

        # -- live: rate limit + duplicate suppression (atomic across processes) -------------------
        wall = svc._wall()
        res = svc.store.reserve_live(
            ts=wall, surface=surface, session=session, command=spec.name, args=args, risk=spec.risk,
            approval=approval_status, adapter=svc.describe_adapter().get("kind", ""),
            rate_window_s=float(settings["rate_limit_window_s"]), rate_max=int(settings["rate_limit_commands"]),
            dedupe_window_s=float(settings["dedupe_window_s"]), canon=canonical_args)
        if res["verdict"] == "rate":
            msg = (f"rate limit reached ({settings['rate_limit_commands']} live commands per "
                   f"{int(settings['rate_limit_window_s'] // 60)} min); try again later")
            aid = self._audit(surface=surface, session=session, command=spec.name, args=args, risk=spec.risk,
                              mode="live", policy="allowed", approval=approval_status, status="rate_limited",
                              detail=msg)
            return self._result(ok=False, status="rate_limited", command=spec.name, spec=spec, armed=armed,
                                policy=pol, approval=approval_status, message=f"Not sent: {msg}.", audit_id=aid)
        if res["verdict"] == "dup":
            msg = f"the same command was already sent {res['age_s']} s ago; skipped as a duplicate"
            aid = self._audit(surface=surface, session=session, command=spec.name, args=args, risk=spec.risk,
                              mode="live", policy="allowed", approval=approval_status, status="duplicate", detail=msg)
            return self._result(ok=True, status="duplicate", command=spec.name, spec=spec, armed=armed, policy=pol,
                                approval=approval_status, message=f"Skipped: {msg}.", audit_id=aid)
        row_id = int(res["id"])

        def finish(status: str, detail: str, readback: str = "n/a", readback_ts: Optional[float] = None) -> int:
            try:
                svc.store.audit_finish(row_id, status=status, detail=scrub(detail)[:500], readback=readback,
                                       readback_ts=readback_ts)
            except Exception:
                logger.exception("failed to finish audit log row")
            return row_id

        # -- live: send --------------------------------------------------------------------------
        send_args = {k: v for k, v in args.items() if k not in POLICY_FLAGS}
        try:
            if args.get("wake_ok") and hasattr(svc.adapter, "allow_wake_once"):
                svc.adapter.allow_wake_once()  # the human agreed to wake the car for THIS command only
            sent_at = svc.now()
            sent = svc.adapter.send_command(spec.name, send_args)
        except AdapterError as exc:
            if (isinstance(exc, AdapterUnavailable) and not isinstance(exc, SecretScopeUnavailable)) \
                    or getattr(exc, "partial", False):
                # A timeout / half-finished multi-step command: the car may well have acted. Say so, and look.
                rb = self._read_back(spec, send_args, float(settings["verify_timeout_s"]),
                                     float(settings["verify_interval_s"]), sent_at=sent_at)
                if rb["status"] == "confirmed":
                    msg = (f"The request to the car timed out or only partly completed ({exc}), but the car "
                           f"shows the change at {rb['at_local']}.")
                    aid = finish("confirmed", msg, rb["status"], rb.get("at"))
                    return self._result(ok=True, status="confirmed", command=spec.name, spec=spec, armed=armed,
                                        policy=pol, approval=approval_status, message=msg, audit_id=aid,
                                        readback={k: v for k, v in rb.items() if k != "at"})
                msg = (f"Pit Crew could not tell whether the car got the command ({exc}). It MAY have been sent: "
                       "check the car's status before sending it again.")
                aid = finish("unknown_outcome", msg, rb["status"], rb.get("at"))
                return self._result(ok=False, status="unknown_outcome", command=spec.name, spec=spec, armed=armed,
                                    policy=pol, approval=approval_status, message=msg, audit_id=aid)
            msg = f"the car/adapter reported an error: {exc}"
            aid = finish("failed", msg)
            return self._result(ok=False, status="failed", command=spec.name, spec=spec, armed=armed, policy=pol,
                                approval=approval_status, message=f"Failed: {msg}.", audit_id=aid)
        except Exception as exc:  # noqa: BLE001 - the audit row must never stay 'sending'
            finish("unknown_outcome", f"unexpected error while sending: {type(exc).__name__}")
            raise
        if not sent.accepted:
            msg = f"the car/adapter declined the command ({sent.detail or 'no reason given'})"
            aid = finish("failed", msg)
            return self._result(ok=False, status="failed", command=spec.name, spec=spec, armed=armed, policy=pol,
                                approval=approval_status, message=f"Failed: {msg}.", audit_id=aid)

        # -- live: read-back ------------------------------------------------------------------------
        rb = self._read_back(spec, send_args, float(settings["verify_timeout_s"]), float(settings["verify_interval_s"]),
                             sent_at=sent_at, before=pre.get("state"), stale_after_s=float(settings.get("stale_after_s") or 900))
        if rb["status"] == "confirmed" and rb.get("already"):
            status, ok = "confirmed", True
            msg = (f"Sent {label}. The car already showed this ({rb['already']}) before the command, so nothing needed to "
                   "change.")
        elif rb["status"] == "confirmed":
            status, ok = "confirmed", True
            msg = f"Sent {label}, and the car confirmed it at {rb['at_local']}."
        elif rb["status"] == "not_confirmed":
            status, ok = "sent_unconfirmed", True
            msg = (f"Sent {label}, but the car has NOT confirmed it yet (checked for "
                   f"{int(rb.get('waited_s') or float(settings['verify_timeout_s']))} s). {rb.get('detail', '')} "
                   "Check status again before relying on it.")
        else:
            status, ok = "sent_unconfirmed", True
            msg = (f"Sent {label}. This car does not report anything that lets me verify it, so I can't confirm "
                   "that it took effect.")
        aid = finish(status, (reason + " | " if reason else "") + sent.detail, rb["status"], rb.get("at"))
        return self._result(ok=ok, status=status, command=spec.name, spec=spec, armed=armed, policy=pol,
                            approval=approval_status, readback={k: v for k, v in rb.items() if k != "at"},
                            message=msg, audit_id=aid)

    # -- internals ------------------------------------------------------------------------------
    def _refuse_live(self, spec: CommandSpec, args: Dict[str, Any], surface: str, session: str,
                     pol: policy_mod.PolicyResult, armed: bool, approval: str, why: str) -> Dict[str, Any]:
        aid = self._audit(surface=surface, session=session, command=spec.name, args=args, risk=spec.risk,
                          mode="blocked", policy="allowed", approval=approval, status="blocked", detail=why)
        return self._result(ok=False, status="blocked", command=spec.name, spec=spec, armed=armed, policy=pol,
                            approval=approval, message=f"Not sent: {why}.", audit_id=aid)

    @staticmethod
    def _observed_at(state: VehicleState, names: Tuple[str, ...]) -> Optional[float]:
        """The oldest time the car/integration actually wrote the fields a read-back relies on (the raw stamp, not one
        borrowed from other fields), or None when any is missing."""
        stamps = []
        for n in names:
            fv = state.fields.get(n)
            if fv is None or not fv.available or fv.updated_at is None:
                return None
            stamps.append(fv.changed_at if fv.changed_at is not None else fv.updated_at)
        return min(stamps) if stamps else None

    def _read_back(self, spec: CommandSpec, args: Mapping[str, Any], timeout_s: float,
                   interval_s: float, *, sent_at: Optional[float] = None, before: Optional[VehicleState] = None,
                   stale_after_s: float = 900.0) -> Dict[str, Any]:
        """Re-read the car until its OWN report shows the change.

        * ``confirmed`` needs the relevant fields to have been *written after the command went out*; an old value that
          happens to match proves nothing (HA can serve a 36-minute-old "charging").
        * If the car already showed the target state, with fresh data, before the command, that is reported as
          ``confirmed`` with ``already`` set ("already closed"), not as a failed change.
        * Commands whose result lags (charging: Fleet API takes 30-90 s) keep looking for ``spec.verify_window_s``."""
        svc = self._svc
        if spec.verify is None:
            return {"status": "unverifiable", "detail": "this command has no observable result"}
        names = tuple(spec.verify_fields)
        already = ""
        if before is not None:
            try:
                was = spec.verify(before, args)
            except (TypeError, ValueError, KeyError):
                was = None
            obs = self._observed_at(before, names)
            if was is True and obs is not None and before.captured_at - obs <= stale_after_s:
                already = self._already_text(spec, args)
        window = max(0.0, timeout_s, spec.verify_window_s)
        step = max(0.05, interval_s, 5.0 if spec.verify_window_s else 0.0)
        started = svc._wall()
        deadline = started + window
        last: Optional[VehicleState] = None
        saw_old_match = False
        while True:
            try:
                last = svc.get_state(force=True)
            except AdapterError as exc:
                return {"status": "not_confirmed", "detail": f"could not re-read the car: {exc}"}
            verdict = spec.verify(last, args)
            if verdict is None:
                return {"status": "unverifiable", "detail": "the car does not report the field needed to verify this"}
            if verdict:
                obs = self._observed_at(last, names)
                if already:
                    return {"status": "confirmed", "at": svc._wall(), "at_local": fmt_clock(svc.local_dt(svc.now())),
                            "already": already, "detail": f"already {already} before the command; nothing needed to change"}
                if sent_at is None or (obs is not None and obs >= sent_at - 5.0):
                    at = svc._wall()
                    return {"status": "confirmed", "at": at, "at_local": fmt_clock(svc.local_dt(svc.now())),
                            "detail": "state re-read from the car shows the change"}
                saw_old_match = True        # matches, but the value predates the command: not evidence
            if svc._wall() >= deadline:
                if saw_old_match:
                    detail = ("the matching value was written before the command was sent, so it is not proof; the "
                              "integration has not refreshed it since")
                else:
                    detail = "state still shows the old value"
                return {"status": "not_confirmed", "detail": detail, "waited_s": window}
            svc._sleep(step)

    @staticmethod
    def _already_text(spec: CommandSpec, args: Mapping[str, Any]) -> str:
        words = {"lock": "locked", "unlock": "unlocked", "close_windows": "closed", "sentry_on": "on", "sentry_off": "off",
                 "open_trunk": "open", "open_frunk": "open", "vent_windows": "open", "climate_on": "on",
                 "climate_off": "off", "charge_start": "charging", "charge_stop": "not charging",
                 "defrost": "on" if args.get("on", True) else "off"}
        return words.get(spec.name, "in that state")
