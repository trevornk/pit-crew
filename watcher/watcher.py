"""The background watcher: polls the car, runs the rules, and pushes alerts.

* Polling is **cheap and never wakes the car**: it only reads (Home Assistant states / TeslaMate status).
* Interval backs off when the car is asleep (``asleep_poll_interval_s``) and exponentially on errors.
* One watcher per data directory: a lease row in the plugin DB elects it, so several Hermes processes
  (gateway, Desktop, CLI) never double-alert.
* Runs as a supervised ``ctx.spawn_task`` when a loop is available, else on a daemon thread.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import sqlite3
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional

from ..adapters.base import AdapterError, SecretScopeUnavailable
from ..events import notify_ui
from .delivery import Deliverer
from .engine import RuleEngine

logger = logging.getLogger(__name__)

LEASE = "watcher"
BG_STATE = "credential_unavailable_in_background"
MAX_ERROR_BACKOFF_S = 900.0
HEARTBEAT_S = 60.0          # the lease is renewed on this beat, independent of poll interval / sleep / backoff
LEASE_TTL_S = 180.0         # three missed beats before another process may take over
YIELD_S = 300.0             # a process that cannot deliver pushes steps aside this long so one that can takes over


class Watcher:
    def __init__(self, service: Any) -> None:
        self._svc = service
        self.engine = RuleEngine(service)
        self.deliverer = Deliverer(service)
        self.holder = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._errors = 0
        self._warned_scope = False
        self._turn_lock = threading.Lock()
        self._turn_in_flight = False
        self._turn_last = 0.0
        self._holds = False
        self.last_poll: Optional[float] = None
        self.last_error: Optional[str] = None

    # -- one poll ---------------------------------------------------------------------------
    def poll_once(self, *, force: bool = False, from_turn: bool = False) -> Dict[str, Any]:
        """Poll, evaluate, deliver. ``force`` (used by `car_alerts check_now`, the page's buttons) skips the lease and
        the enabled switch, and only STORES alerts: pushing is the lease holder's job. ``from_turn``: the turn-time
        fallback (runs in the chat's secret scope) — it must not set or clear the background-degraded state.
        Never raises: any unexpected error becomes ``{"error": ...}`` and counts toward the back-off."""
        try:
            return self._poll_once(force=force, from_turn=from_turn)
        except sqlite3.OperationalError as exc:      # "database is locked" etc.: skip this round, try again
            logger.warning("watcher: database busy (%s); skipping this round", exc)
            return {"polled": False, "raised": [], "pushed": False, "skipped": "database busy"}
        except Exception as exc:  # noqa: BLE001 - the watcher must outlive any single bad poll
            logger.exception("watcher: poll failed unexpectedly")
            self._errors += 1
            self.last_error = f"{type(exc).__name__}"
            return {"polled": False, "raised": [], "pushed": False, "error": f"unexpected error ({type(exc).__name__})"}

    def _yielded(self) -> bool:
        key = f"watcher_yield:{self.holder}"
        until = self._svc.store.kv_get(key)
        if until is None:
            return False
        if float(until) > self._svc._wall():
            return True
        self._svc.store.kv_delete(key)
        return False

    def _yield_lease(self) -> None:
        """This process polled fine but cannot deliver pushes: hand the lease to one that can."""
        svc = self._svc
        svc.store.kv_set(f"watcher_yield:{self.holder}", svc._wall() + YIELD_S)
        self.release()
        logger.info("watcher: this process cannot deliver pushes; yielding the watcher lease for %d s", YIELD_S)

    def _poll_once(self, *, force: bool = False, from_turn: bool = False) -> Dict[str, Any]:
        svc = self._svc
        result: Dict[str, Any] = {"polled": False, "raised": [], "pushed": False}
        if not force:
            if not svc.settings.get("watcher_enabled"):
                return result | {"skipped": "watcher disabled"}
            if self._yielded():
                return result | {"skipped": "yielding to a process that can deliver pushes"}
            if not svc.store.lease_acquire(LEASE, self.holder, ttl_s=LEASE_TTL_S, now=svc._wall()):
                self._holds = False
                return result | {"skipped": "another process holds the watcher lease"}
            self._holds = True
        try:
            state = svc.get_state(force=True)
        except SecretScopeUnavailable as exc:
            self.last_error = str(exc)
            if not from_turn and not force:   # only the lease holder speaks for "background": a forced poll may be in a process that CAN read secrets
                changed = svc.set_background_state(BG_STATE)
                if changed or not self._warned_scope:
                    self._warned_scope = True
                    logger.warning("watcher: plugin secrets are not readable from background tasks in this Hermes "
                                   "(no secret scope); background alerts are paused and will be checked during chats")
            return result | {"error": str(exc), "degraded": BG_STATE}
        except AdapterError as exc:
            self._errors += 1
            self.last_error = str(exc)
            logger.warning("watcher: cannot read the car (%s)", exc)
            return result | {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - an odd payload / a bug in one adapter must not stop the watcher
            self._errors += 1
            self.last_error = f"unexpected error reading the car ({type(exc).__name__})"
            logger.exception("watcher: unexpected error reading the car")
            return result | {"error": self.last_error}
        if not from_turn and not force and svc.set_background_state(None):
            logger.info("watcher: background credential reads work again; resuming normal background polling")
            self._warned_scope = False
        self._errors = 0
        self.last_error = None
        self.last_poll = svc._wall()
        result["polled"] = True
        try:
            raised = self.engine.evaluate(state, svc.now())
        except Exception:
            logger.exception("watcher: rule evaluation crashed")
            return result | {"error": "rule evaluation failed (see logs)"}
        result["raised"] = raised
        notify_ui("state.changed", {"alerts": len(raised)})
        if not force and not from_turn:
            # Push EVERY still-'new' alert (including ones stored by a forced poll in another process).
            try:
                result["pushed"] = self.deliverer.push_pending()
                if self.deliverer.undeliverable:
                    self._yield_lease()
            except Exception:
                logger.exception("watcher: delivery crashed")
        result["asleep"] = state.value("asleep") is True
        return result

    # -- turn-time fallback (only while the background watcher cannot read secrets) ---------------------------
    def schedule_turn_check(self) -> bool:
        """Called from ``pre_llm_call`` (inside the turn's secret scope). Starts at most one read-only alert check per
        poll interval on a worker thread that carries a COPY of this context, so the scope travels. Never blocks the
        turn, never raises, never sends a vehicle command. Returns True if a check was started."""
        svc = self._svc
        try:
            if not svc.background_state or not svc.settings.get("watcher_enabled"):
                return False
            now = svc._wall()
            with self._turn_lock:
                if self._turn_in_flight or now - self._turn_last < float(svc.settings.get("poll_interval_s")):
                    return False
                self._turn_in_flight, self._turn_last = True, now
            ctx = contextvars.copy_context()
            threading.Thread(target=ctx.run, args=(self._turn_check,), name="pit-crew-turn-check", daemon=True).start()
            return True
        except Exception:
            logger.exception("could not schedule the turn-time alert check")
            with self._turn_lock:
                self._turn_in_flight = False
            return False

    def _turn_check(self) -> None:
        try:
            self.poll_once(from_turn=True)
        except Exception:
            logger.exception("turn-time alert check failed")
        finally:
            with self._turn_lock:
                self._turn_in_flight = False

    def next_interval(self, last: Dict[str, Any]) -> float:
        s = self._svc.settings
        if self._errors:
            return min(MAX_ERROR_BACKOFF_S, float(s.get("poll_interval_s")) * (2 ** min(self._errors, 5)))
        if last.get("asleep"):
            return float(s.get("asleep_poll_interval_s"))
        return float(s.get("poll_interval_s"))

    # -- loops ------------------------------------------------------------------------------
    async def run(self, stop: Optional[asyncio.Event] = None, *, initial_delay_s: float = 20.0) -> None:
        stop = stop or asyncio.Event()
        try:
            await asyncio.wait_for(stop.wait(), timeout=initial_delay_s)
            return
        except asyncio.TimeoutError:
            pass
        heartbeat = asyncio.ensure_future(self._heartbeat(stop))
        try:
            while not stop.is_set():
                try:
                    last = await asyncio.to_thread(self.poll_once)
                except Exception:  # noqa: BLE001 - poll_once already catches; this is the last line of defence
                    logger.exception("watcher: poll crashed; backing off")
                    self._errors += 1
                    last = {}
                interval = max(15.0, self.next_interval(last))
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    continue
        finally:
            heartbeat.cancel()
            self.release()

    async def _heartbeat(self, stop: asyncio.Event) -> None:
        """Keep the lease alive on a fixed beat — never tied to the poll interval, the asleep sleep or error back-off,
        so ownership stays put instead of drifting to whichever process polls next."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_S)
                return
            except asyncio.TimeoutError:
                pass
            if not self._holds:
                continue
            try:
                ok = await asyncio.to_thread(self._renew)
            except Exception:  # noqa: BLE001
                logger.debug("watcher: lease renewal failed", exc_info=True)
                continue
            if not ok:
                self._holds = False

    def _renew(self) -> bool:
        svc = self._svc
        return bool(svc.store.lease_acquire(LEASE, self.holder, ttl_s=LEASE_TTL_S, now=svc._wall()))

    def release(self) -> None:
        self._holds = False
        try:
            self._svc.store.lease_release(LEASE, self.holder)
        except Exception:
            logger.debug("could not release watcher lease", exc_info=True)


# ---------------------------------------------------------------------------------------------
_started_lock = threading.Lock()
_started: Dict[str, Any] = {}


async def run_watcher(get_service: Callable[[], Any], stop: asyncio.Event, initial_delay_s: float = 20.0) -> None:
    """Wait ``initial_delay_s`` (so plugin registration does no I/O), then run the service's watcher."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=initial_delay_s)
        return
    except asyncio.TimeoutError:
        pass
    await get_service().watcher.run(stop, initial_delay_s=0.0)


async def _supervised(get_service: Callable[[], Any], stop_evt: threading.Event, initial_delay_s: float) -> None:
    """Run the watcher on whatever loop this coroutine lands on; ``stop_evt`` (thread-safe) ends it from any thread."""
    aio_stop = asyncio.Event()

    async def _watch_stop() -> None:
        while not stop_evt.is_set():
            await asyncio.sleep(1.0)
        aio_stop.set()

    await asyncio.gather(_watch_stop(), run_watcher(get_service, aio_stop, initial_delay_s))


def _stopper(stop_evt: threading.Event) -> Callable[[], None]:
    """Unload handler: stop the watcher AND forget it, so re-registering in the same process (reload / update) starts a new one
    instead of answering 'already' with nothing running."""
    def stop() -> None:
        stop_evt.set()
        with _started_lock:
            _started.clear()
    return stop


def start_watcher(ctx: Any, get_service: Callable[[], Any], initial_delay_s: float = 20.0) -> str:
    """Start the watcher once per process. Returns 'task' | 'thread' | 'already'.

    Always tries the host's supervised ``ctx.spawn_task`` first. On Hermes v0.23.0 that needs a running event loop
    (``hermes_cli/plugins.py`` uses ``asyncio.get_running_loop()``), so at ``register()`` time it usually raises RuntimeError;
    some newer or patched Hermes builds run it on a Hermes-owned loop instead. Whenever it is absent or raises, the plugin
    starts its own daemon thread. Either way the stop signal is a thread-safe event set from ``ctx.on_unload``, so it works
    on whichever loop/thread runs the watcher."""
    with _started_lock:
        if _started.get("mode"):
            return "already"
        stop_evt = threading.Event()
        spawn = getattr(ctx, "spawn_task", None)
        if callable(spawn):
            coro = _supervised(get_service, stop_evt, initial_delay_s)
            try:
                spawn(coro, name="pit-crew:watcher")
            except Exception as exc:
                coro.close()  # never leave an un-awaited coroutine behind
                if not isinstance(exc, RuntimeError):
                    logger.warning("ctx.spawn_task failed (%s); using the plugin's own watcher thread", type(exc).__name__)
            else:
                ctx.on_unload(_stopper(stop_evt))
                _started["mode"] = "task"
                return "task"

        def _thread_main() -> None:
            try:
                asyncio.run(_supervised(get_service, stop_evt, initial_delay_s))
            except Exception:
                logger.exception("pit-crew watcher thread crashed")

        threading.Thread(target=_thread_main, name="pit-crew-watcher", daemon=True).start()
        ctx.on_unload(_stopper(stop_evt))
        _started["mode"] = "thread"
        return "thread"


def reset_started_for_tests() -> None:
    with _started_lock:
        _started.clear()
