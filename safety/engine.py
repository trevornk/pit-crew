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
from .gate import approval_mode_sentence

logger = logging.getLogger(__name__)

APPROVAL_KIND = "hermes_approval"
AUTO_APPROVED_NOTE = ("Sent without asking: your Hermes approvals didn't require a prompt (approvals off, yolo, or "
                      "already allowed this session).")
POLICY_FLAGS = ("override_zone", "wake_ok")
#: "the car is already in that state" is only said when the car itself reported it at most this long before the command
#: (a door locked by hand a minute ago is invisible to a 10-minute-old reading; see docs/DECISIONS.md)
ALREADY_MAX_AGE_S = 60.0


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
    def preflight(self, command: str, raw_args: Optional[Mapping[str, Any]], *,
                  cached_max_age_s: Optional[float] = None) -> Dict[str, Any]:
        """Validate + policy-check without executing. Used by the approval hook and the Desktop dialog.

        ``cached_max_age_s``: accept a state read at most this many seconds ago instead of forcing a live read (the
        approval hook uses this so a slow car/HA read can't hang Hermes' hook; the tool's own preflight is always live)."""
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
            if cached_max_age_s is None:
                state = svc.get_state(force=True)  # safety decisions never use a cached read
            else:
                state = svc.get_state(max_age_s=float(cached_max_age_s))
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
            need = ""
            if spec.requires_approval:
                need = " If it were live: " + approval_mode_sentence(spec.name, settings.get("require_human_for"))
            paused = armed_info.get("state") == "paused"
            why = ("a practice run was requested" if force_dry_run else
                   "live mode is paused (practice)" if paused else "practice mode is on")
            how = ("To send real commands again, resume live mode (`/car live`, or Go live on the Pit Crew page)."
                   if paused else
                   "To send real commands, go live (`/car live`, or the Go live button on the Pit Crew page); or set "
                   "Safety · Live mode to 'always' in Pit Crew settings to skip this step.")
            msg = (f"PRACTICE RUN — {label.replace("_", " ")} passed all safety checks, but {why}, so the car was NOT contacted.{need} "
                   + how)
            if spec.caution and args.get("on", True):
                msg += " Heads-up: " + spec.caution
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
                                         "(scheduled / unattended run), so it was not sent. No setting changes this: "
                                         "ask for it in a chat")
            if approval is None or approval.kind != APPROVAL_KIND:
                listed = spec.name in (settings.get("require_human_for") or ())
                return self._refuse_live(spec, args, surface, session, pol, armed, "missing",
                                         "no confirmed human approval was found for this exact command, so it was "
                                         "not sent (fail closed)" + (
                                             f". It is on your {policy_mod._setting('require_human_for')} list, so only "
                                             "a person's answer to Hermes' prompt counts; remove it there to follow your "
                                             "Hermes approval setting instead" if listed else ""))
            if approval.denied:
                return self._refuse_live(spec, args, surface, session, pol, armed, "denied",
                                         f"the human declined ({approval.detail})")
            approval_status = "hermes_auto" if approval.auto else "confirmed"

        # -- live: rate limit + duplicate suppression (atomic across processes) -------------------
        wall = svc._wall()
        res = svc.store.reserve_live(
            ts=wall, surface=surface, session=session, command=spec.name, args=args, risk=spec.risk,
            approval=approval_status, adapter=svc.describe_adapter().get("kind", ""),
            rate_window_s=float(settings["rate_limit_window_s"]), rate_max=int(settings["rate_limit_commands"]),
            dedupe_window_s=float(settings["dedupe_window_s"]), canon=canonical_args)
        if res["verdict"] == "rate":
            msg = (f"rate limit reached ({settings['rate_limit_commands']} live commands per "
                   f"{int(settings['rate_limit_window_s'] // 60)} min); try again later, or raise "
                   f"{policy_mod._setting('rate_limit_commands')} in Pit Crew settings")
            aid = self._audit(surface=surface, session=session, command=spec.name, args=args, risk=spec.risk,
                              mode="live", policy="allowed", approval=approval_status, status="rate_limited",
                              detail=msg)
            return self._result(ok=False, status="rate_limited", command=spec.name, spec=spec, armed=armed,
                                policy=pol, approval=approval_status, message=f"Not sent: {msg}.", audit_id=aid)
        if res["verdict"] == "dup":
            msg = (f"the same command was already sent {res['age_s']} s ago; skipped as a duplicate (repeats are ignored "
                   f"for {int(settings['dedupe_window_s'])} s: {policy_mod._setting('dedupe_window_s')})")
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
        quick, latency = self._quick_readback(spec, settings)
        try:
            auto_wake = settings.get("wake_for_commands") == "auto"
            if (args.get("wake_ok") or auto_wake) and hasattr(svc.adapter, "allow_wake_once"):
                # the human agreed to wake the car for THIS command only (or set wake_for_commands: auto); a status
                # read never wakes it either way
                svc.adapter.allow_wake_once()
            sent_at = svc.now()
            t0 = time.monotonic()
            sent = svc.adapter.send_command(spec.name, send_args)
            send_ms = int((time.monotonic() - t0) * 1000)
        except AdapterError as exc:
            if (isinstance(exc, AdapterUnavailable) and not isinstance(exc, SecretScopeUnavailable)) \
                    or getattr(exc, "partial", False):
                # A timeout / half-finished multi-step command: the car may well have acted. Say so, and look.
                rb = self._read_back(spec, send_args, float(settings["verify_timeout_s"]),
                                     float(settings["verify_interval_s"]), sent_at=sent_at, single_read=quick)
                if rb["status"] == "confirmed":
                    msg = (f"The request to the car timed out or only partly completed ({exc}), but the car "
                           f"shows the change at {rb['at_local']}.")
                    if approval_status == "hermes_auto":
                        msg += " " + AUTO_APPROVED_NOTE
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
        t1 = time.monotonic()
        rb = self._read_back(spec, send_args, float(settings["verify_timeout_s"]), float(settings["verify_interval_s"]),
                             sent_at=sent_at, before=pre.get("state"), single_read=quick)
        if quick and rb["status"] in ("accepted", "not_confirmed"):
            # The car's own report cannot arrive inside any wait we could reasonably make: say so now, don't stall.
            rb = {**rb, "status": "accepted", "quick": True, "waited_s": 0,
                  "detail": (f"read once right after sending; {svc.adapter.service_name()}'s report of the car comes "
                             f"about every {self._age_words(latency)}, so waiting longer would only delay this answer")}
        timing = {"send": send_ms, "readback": int((time.monotonic() - t1) * 1000)}
        waited = int(rb.get('waited_s') or float(settings['verify_timeout_s']))
        if rb.get("quick"):
            status, ok = "sent_unconfirmed", True
            who = svc.adapter.service_name()
            msg = (f"Sent {label}: {who[:1].upper() + who[1:]} accepted it. The car reports back within "
                   f"~{self._age_words(latency)}; check car_status later before relying on it.")
        elif rb["status"] == "confirmed" and rb.get("already"):
            status, ok = "confirmed", True
            msg = (f"Sent {label}. The car itself reported this ({rb['already']}) less than a minute before the command, "
                   "so nothing needed to change.")
        elif rb["status"] == "confirmed":
            status, ok = "confirmed", True
            msg = f"Sent {label}, and the car confirmed it at {rb['at_local']}."
        elif rb["status"] == "accepted":
            status, ok = "sent_unconfirmed", True
            msg = (f"Sent {label}: the car's service accepted it, but the car itself has NOT reported back yet (checked "
                   f"for {waited} s), so this is not confirmed. {rb.get('detail', '')} "
                   "Check status again later before relying on it.")
        elif rb["status"] == "not_confirmed":
            status, ok = "sent_unconfirmed", True
            msg = (f"Sent {label}, but the car has NOT confirmed it yet (checked for {waited} s). "
                   f"{rb.get('detail', '')} Check status again before relying on it.")
        else:
            status, ok = "sent_unconfirmed", True
            msg = (f"Sent {label}. This car does not report anything that lets me verify it, so I can't confirm "
                   "that it took effect.")
        if rb.get("stale_match"):
            msg += " " + rb["stale_match"]
        if spec.caution and args.get("on", True):
            msg += " Heads-up: " + spec.caution
        if approval_status == "hermes_auto":
            msg += " " + AUTO_APPROVED_NOTE
        tail = f"{sent.detail} | timing: service answered in {timing['send']} ms, read-back took {timing['readback']} ms"
        head = (reason[:max(0, 480 - len(tail))] + " | ") if reason else ""
        aid = finish(status, head + tail, rb["status"], rb.get("at"))
        return self._result(ok=ok, status=status, command=spec.name, spec=spec, armed=armed, policy=pol,
                            approval=approval_status, readback={k: v for k, v in rb.items() if k != "at"},
                            message=msg, audit_id=aid, timing_ms=timing)

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

    def _quick_readback(self, spec: CommandSpec, settings: Mapping[str, Any]) -> Tuple[bool, float]:
        """(read once and answer now?, the adapter's report latency). True when the car's own report takes far longer
        (more than twice) than the longest read-back wait for this command: e.g. tesla_fleet re-reads every 600 s."""
        try:
            fn = getattr(self._svc.adapter, "report_latency_s", None)
            latency = fn() if callable(fn) else None
        except AdapterError:
            latency = None
        if not isinstance(latency, (int, float)) or spec.verify is None:
            return False, 0.0
        window = max(0.0, float(settings["verify_timeout_s"]), spec.verify_window_s)
        return float(latency) > 2.0 * window, float(latency)

    def _read_back(self, spec: CommandSpec, args: Mapping[str, Any], timeout_s: float,
                   interval_s: float, *, sent_at: Optional[float] = None, before: Optional[VehicleState] = None,
                   single_read: bool = False) -> Dict[str, Any]:
        """Re-read the car until its OWN report shows the change.

        * ``confirmed`` needs the relevant fields to have been *written after the command went out*; an old value that
          happens to match proves nothing (HA can serve a 36-minute-old "charging").
        * A value the integration wrote itself right after the command (``FieldValue.assumed``: tesla_fleet, teslemetry and
          tessie set the entity optimistically as soon as the API call returns) is not the car's report either: if that
          is all there is when time runs out, the result is ``accepted`` ("the service took it, the car hasn't
          reported back"), never ``confirmed``.
        * "Already in that state" (``confirmed`` + ``already``) needs the car's own report from at most
          :data:`ALREADY_MAX_AGE_S` before the command. An older matching reading is only mentioned (``stale_match``):
          the door may have been locked by hand since.
        * Commands whose result lags (charging: Fleet API takes 30-90 s) keep looking for ``spec.verify_window_s``.
        * ``single_read``: one read, no waiting (the adapter's report latency is far longer than any wait; see
          :meth:`_quick_readback`)."""
        svc = self._svc
        if spec.verify is None:
            return {"status": "unverifiable", "detail": "this command has no observable result"}
        names = tuple(spec.verify_fields)
        already = stale_match = ""
        if before is not None:
            try:
                was = spec.verify(before, args)
            except (TypeError, ValueError, KeyError):
                was = None
            if was is True:
                age = self._reported_age(before, names)
                if age is not None and age <= ALREADY_MAX_AGE_S:
                    already = self._already_text(spec, args)
                else:
                    when = "of unknown age" if age is None else f"{self._age_words(age)} old"
                    stale_match = (f"(The last reading before sending ({when}) said {self._already_text(spec, args)}, "
                                   "but it may be out of date, so the command was sent anyway and that reading proves "
                                   "nothing.)")
        window = 0.0 if single_read else max(0.0, timeout_s, spec.verify_window_s)
        step = max(0.05, interval_s, 5.0 if spec.verify_window_s else 0.0)
        started = svc._wall()
        deadline = started + window
        last: Optional[VehicleState] = None
        extra = {"stale_match": stale_match} if stale_match else {}
        while True:
            try:
                last = svc.get_state(force=True)
            except AdapterError as exc:
                return {"status": "not_confirmed", "detail": f"could not re-read the car: {exc}", **extra}
            verdict = spec.verify(last, args)
            saw_old_match = saw_assumed = False     # judged on the LATEST read only (a re-read can undo an assumption)
            if verdict is None:
                return {"status": "unverifiable", "detail": "the car does not report the field needed to verify this",
                        **extra}
            if verdict:
                obs = self._observed_at(last, names)
                assumed = any(last.fields[n].assumed for n in names if n in last.fields)
                if already:     # the car itself said so < 1 min before; HA re-writing the same value changes nothing
                    return {"status": "confirmed", "at": svc._wall(), "at_local": fmt_clock(svc.local_dt(svc.now())),
                            "already": already, "detail": f"already {already} before the command; nothing needed to change"}
                if assumed:
                    saw_assumed = True      # the integration's own optimistic write: not the car's report
                elif sent_at is None or (obs is not None and obs >= sent_at - 5.0):
                    at = svc._wall()
                    return {"status": "confirmed", "at": at, "at_local": fmt_clock(svc.local_dt(svc.now())),
                            "detail": "state re-read from the car shows the change", **extra}
                else:
                    saw_old_match = True        # matches, but the value predates the command: not evidence
            if svc._wall() >= deadline:
                if saw_assumed:
                    return {"status": "accepted", "waited_s": window, **extra,
                            "detail": ("Home Assistant shows the new value, but only because its integration set it "
                                       "itself when the command was accepted; it has not re-read the car since "
                                       "(Tesla Fleet re-reads about every 10 minutes).")}
                if saw_old_match:
                    detail = ("the matching value was written before the command was sent, so it is not proof; the "
                              "integration has not refreshed it since")
                else:
                    detail = "state still shows the old value"
                return {"status": "not_confirmed", "detail": detail, "waited_s": window, **extra}
            svc._sleep(step)

    @staticmethod
    def _reported_age(state: VehicleState, names: Tuple[str, ...]) -> Optional[float]:
        """How long before ``state`` was captured the car itself last reported all of ``names`` (None: unknown, or one
        of them is only the integration's optimistic assumption)."""
        stamps = []
        for n in names:
            fv = state.fields.get(n)
            if fv is None or not fv.available or fv.updated_at is None or fv.assumed:
                return None
            stamps.append(fv.updated_at)
        return max(0.0, state.captured_at - min(stamps)) if stamps else None

    @staticmethod
    def _age_words(age_s: float) -> str:
        if age_s < 90:
            return f"{int(age_s)} s"
        if age_s < 5400:
            return f"{int(round(age_s / 60))} min"
        return f"{age_s / 3600:.1f} h"

    @staticmethod
    def _already_text(spec: CommandSpec, args: Mapping[str, Any]) -> str:
        words = {"lock": "locked", "unlock": "unlocked", "close_windows": "closed", "sentry_on": "on", "sentry_off": "off",
                 "open_trunk": "open", "open_frunk": "open", "vent_windows": "open", "climate_on": "on",
                 "climate_off": "off", "charge_start": "charging", "charge_stop": "not charging",
                 "defrost": "on" if args.get("on", True) else "off"}
        return words.get(spec.name, "in that state")
