"""The service object that ties settings, adapters, store and the brain together.

One instance per process (``get_service()``). Tools, hooks, the slash command, the watcher and
the Desktop backend all go through it so they always agree on state, armed status and the audit
log. Everything here is synchronous and thread-safe.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .adapters import base as adapters_base
from .adapters.base import (CREDENTIAL_BG_TEXT, SecretScopeUnavailable, AdapterError, BuildContext, CarAdapter, HistoryProvider)
from .brain.commands import Capabilities
from .brain.models import VehicleState, describe_state
from .redact import scrub
from .history_store import CombinedHistory, OwnHistory, Recorder, import_history
from .constants import ENV_HA_TOKEN, ENV_TESLAMATE_TOKEN, PLUGIN_ID
from .settings import Settings, hermes_settings_provider
from .store import Store, open_store

logger = logging.getLogger(__name__)

BG_KEY = "background_credential_state"
BG_STATE = "credential_unavailable_in_background"
SecretGetter = Callable[[str], Optional[str]]


def hermes_secret_getter(name: str) -> Optional[str]:
    """Read a declared secret through Hermes' profile-aware secret scope (catalog rule 11).

    This is the ONLY credential route in the plugin: no ``os.environ``, no ``.env``/``auth.json``/token files."""
    try:
        from agent.secret_scope import get_secret
    except ImportError:
        logger.debug("agent.secret_scope unavailable (not running inside Hermes)")
        return None
    try:
        value = get_secret(name)
    except Exception as exc:
        # UnscopedSecretError (matched by class name: no hard import dependency) = no secret scope in this context
        # (background task under profile multiplexing). Never fall back to os.environ/.env (catalog rule 11).
        if any(c.__name__ == "UnscopedSecretError" for c in type(exc).__mro__):
            raise SecretScopeUnavailable() from None
        logger.warning("could not read secret %s through Hermes' secret scope (%s)", name, type(exc).__name__)
        return None
    return value or None


def _system_zone() -> Any:
    """R13: a real IANA zone when the OS names one (so DST changes are honoured); else today's fixed offset."""
    import os
    try:
        from zoneinfo import ZoneInfo
        real = os.path.realpath("/etc/localtime")      # (never the environment: catalog rule 11)
        name = real.split("zoneinfo/", 1)[1] if "zoneinfo/" in real else ""
        if name:
            return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - any failure falls back to the fixed offset
        pass
    return datetime.now().astimezone().tzinfo


class CarService:
    def __init__(self, settings: Settings, secret: SecretGetter, store: Store,
                 wall_clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        # Importing the package registers the built-in adapters.
        from . import adapters as _builtin  # noqa: F401

        self.settings = settings
        self._secret = secret
        self.store = store
        self._wall = wall_clock
        self._sleep = sleep
        self._lock = threading.RLock()
        self._adapter: Optional[CarAdapter] = None
        self._history: Optional[HistoryProvider] = None
        self._fingerprint: Optional[tuple] = None
        self._last_state: Optional[VehicleState] = None
        self._last_fetch_wall: float = 0.0
        self.last_error: Optional[str] = None
        self.ctx: Any = None  # the Hermes PluginContext once register() ran (for ask_choice etc.)
        self._engine = None
        self._recorder = Recorder(store, lambda: float(self.settings.get("battery_kwh")),
                                  lambda: float(self.settings.get("electricity_rate_usd_kwh")))
        self._watcher = None
        from .safety.approval import ApprovalBroker

        self._broker = ApprovalBroker(wall_clock)

    # -- adapters -------------------------------------------------------------------------
    def _build_ctx(self) -> BuildContext:
        return BuildContext(settings=self.settings.snapshot(), secret=self._secret, store=self.store,
                            clock=self._wall)

    def _ensure_built(self) -> None:
        fp = self.settings.adapter_fingerprint()
        if self._adapter is not None and fp == self._fingerprint:
            return
        with self._lock:
            if self._adapter is not None and fp == self._fingerprint:
                return
            snap = self.settings.snapshot()
            bctx = self._build_ctx()
            old = self._adapter
            self._adapter = adapters_base.build_adapter(snap["adapter"], bctx)
            if old is not None:
                try:
                    old.close()
                except Exception:  # noqa: BLE001
                    logger.debug("closing the previous adapter failed", exc_info=True)
            try:
                if snap["history"] == "own":
                    own = OwnHistory(self.store, lambda: float(self.settings.get("battery_kwh")), self.now)
                    backfill = (adapters_base.build_history("simulator", bctx)
                                if getattr(self._adapter, "simulated", False) else None)
                    self._history = CombinedHistory(own, backfill) if backfill is not None else own
                else:
                    self._history = adapters_base.build_history(snap["history"], bctx)
            except AdapterError as exc:
                logger.warning("history provider unavailable: %s", exc)
                self._history = None
            if self._history is None and getattr(self._adapter, "simulated", False):
                self._history = adapters_base.build_history("simulator", bctx)
            self._fingerprint = fp
            self._last_state = None

    def close(self) -> None:
        """Release the adapter's threads/sessions (plugin unload)."""
        with self._lock:
            if self._adapter is not None:
                try:
                    self._adapter.close()
                except Exception:  # noqa: BLE001
                    logger.debug("closing the adapter failed", exc_info=True)

    @property
    def adapter(self) -> CarAdapter:
        self._ensure_built()
        assert self._adapter is not None
        return self._adapter

    @property
    def history_provider(self) -> Optional[HistoryProvider]:
        self._ensure_built()
        return self._history

    def capabilities(self) -> Capabilities:
        caps = self.adapter.capabilities()
        if self.history_provider is not None:
            caps = Capabilities(caps.state_fields, caps.commands, True, caps.notes)
        return caps

    # -- time -----------------------------------------------------------------------------
    def now(self) -> float:
        """Epoch seconds — the simulator's clock when simulating, wall clock otherwise."""
        clk = self.adapter.clock()
        return clk if clk is not None else self._wall()

    def local_tz(self):
        name = self.settings.get("timezone")
        if name:
            try:
                from zoneinfo import ZoneInfo

                return ZoneInfo(name)
            except Exception as exc:
                logger.warning("unknown timezone %r in settings (%s); using system local time", name, type(exc).__name__)
        return _system_zone()

    def local_dt(self, ts: Optional[float] = None) -> datetime:
        return datetime.fromtimestamp(self.now() if ts is None else ts, tz=self.local_tz())

    # -- state ----------------------------------------------------------------------------
    def get_state(self, *, max_age_s: float = 10.0, force: bool = False) -> VehicleState:
        """Latest state, cached for ``max_age_s`` wall seconds. Raises :class:`AdapterError`."""
        self._ensure_built()
        with self._lock:
            if (not force and self._last_state is not None
                    and self._wall() - self._last_fetch_wall <= max_age_s):
                return self._last_state
            try:
                state = self.adapter.fetch_state(self.now())
            except AdapterError as exc:
                self.last_error = scrub(str(exc))
                raise
            self.last_error = None
            self._recorder.record(state, state.captured_at)
            volts = state.value("charger_voltage_v")
            if state.is_charging and isinstance(volts, (int, float)) and volts > 50:
                self.store.kv_set("last_charge_volts", float(volts))   # R10: remembered for when the charger is idle
            self._last_state = state
            self._last_fetch_wall = self._wall()
            return state

    # -- degraded background credential state ---------------------------------------------------------------
    @property
    def background_state(self) -> Optional[str]:
        """``credential_unavailable_in_background`` while the watcher cannot read plugin secrets (shared via the DB)."""
        return self.store.kv_get(BG_KEY)

    def set_background_state(self, value: Optional[str]) -> bool:
        """Record/clear the degraded state; returns True when it changed."""
        if self.background_state == value:
            return False
        if value:
            self.store.kv_set(BG_KEY, value)
        else:
            self.store.kv_delete(BG_KEY)
        return True

    def motion_hint(self) -> str:
        fn = getattr(self._adapter, "motion_hint", None) if self._adapter is not None else None
        return str(fn()) if callable(fn) else ""

    def warnings(self, state: Optional[VehicleState] = None) -> List[str]:
        """Plain-language, actionable setup warnings (e.g. the car has no motion source, so unlocks are blocked)."""
        out: List[str] = []
        if self.background_state:
            out.append(CREDENTIAL_BG_TEXT)
        out.extend(self._link_warnings())
        try:
            fn = getattr(self.adapter, "motion_warnings", None)
            if callable(fn):
                out.extend(fn())
            elif state is not None and not any(state.is_available(f) for f in ("moving", "speed_mph", "shift_state")):
                out.append("This car reports no speed, gear or driving state, so Pit Crew can only treat it as parked while it is "
                           "plugged in; unlock/trunk/frunk/vent are blocked otherwise. " + self.motion_hint())
        except AdapterError:
            pass
        try:
            fn = getattr(self.adapter, "setup_warnings", None)
            if callable(fn):
                out.extend(fn())
        except AdapterError:
            pass
        return out

    def _link_warnings(self) -> List[str]:
        """Plain-HTTP / unverified-TLS warnings for a Home Assistant link (the bearer token travels over it)."""
        from urllib.parse import urlsplit

        if str(self.settings.get("adapter")) != "homeassistant":
            return []
        url = str(self.settings.get("ha_url") or "")
        host = (urlsplit(url).hostname or "") if url else ""
        out: List[str] = []
        if url.lower().startswith("http://") and host not in ("localhost", "127.0.0.1", "::1"):
            out.append("Home Assistant is reached over plain http://, so its access token travels unencrypted on your network. "
                       "Prefer https:// (or a VPN) if anything else shares that network.")
        if url.lower().startswith("https://") and not self.settings.get("ha_verify_tls"):
            out.append("Certificate checking for Home Assistant is turned off, so Pit Crew cannot tell if something is "
                       "impersonating it. Turn \"Verify HTTPS certificate\" back on unless you use a self-signed certificate.")
        return out

    SETUP_HINTS = {
        "homeassistant": "Enter your Home Assistant address and an access token (Settings → Plugins → Pit Crew), then run "
                         "`/car discover` to find your car.",
        "tesla": "Enter your Tesla VIN and your Teslemetry or Tessie token (Settings → Plugins → Pit Crew).",
        "teslamate": "Enter your TeslaMateApi address (Settings → Plugins → Pit Crew).",
    }

    def connection_info(self, status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Plain-language connection state for the pages and ``/car``: is a car readable, and if not, what to do."""
        desc = self.describe_adapter()
        kind = str(desc.get("kind") or self.settings.get("adapter") or "")
        ok = bool((status or {}).get("ok")) if status is not None else "error" not in desc
        names = {"simulator": "Simulated car", "homeassistant": "Home Assistant", "tesla": "Tesla (direct)",
                 "teslamate": "TeslaMate"}
        out: Dict[str, Any] = {"kind": kind, "label": names.get(kind, desc.get("label") or kind or "unknown"),
                               "connected": ok, "simulated": bool(desc.get("simulated"))}
        if not ok:
            err = str((status or {}).get("error") or desc.get("error") or "")
            out["error"] = err
            hint = self.SETUP_HINTS.get(kind, "Open Settings → Plugins → Pit Crew and check the Connection section.")
            out["hint"] = hint
            out["message"] = f"Pit Crew can't read your car yet. {err}".strip()
        return out

    def describe_adapter(self) -> Dict[str, Any]:
        """Adapter description that never raises (a misconfigured adapter is reported, not thrown)."""
        try:
            return self.adapter.describe()
        except AdapterError as exc:
            return {"kind": str(self.settings.get("adapter")), "label": "not connected", "simulated": False,
                    "error": scrub(str(exc))}

    def status(self, *, force: bool = False) -> Dict[str, Any]:
        """JSON-ready status (never raises): state, summary, safety posture, capabilities."""
        out: Dict[str, Any] = {"adapter": self.describe_adapter()}
        try:
            state = self.get_state(force=force)
        except SecretScopeUnavailable as exc:
            out.update({"ok": False, "error": scrub(str(exc)), "warnings": [CREDENTIAL_BG_TEXT],
                        "degraded": BG_STATE})
            return out
        except AdapterError as exc:
            out.update({"ok": False, "error": scrub(str(exc)),
                        "hint": "Check the adapter settings (URL, token, entity slug) or switch the adapter to 'simulator'."})
            return out
        now = self.now()
        out.update({"ok": True, "summary": describe_state(state, now),
                    **state.to_public(now, float(self.settings.get("stale_after_s")), self.local_tz())})
        out["safety"] = self.safety_posture()
        warns = self.warnings(state)
        if warns:
            out["warnings"] = warns
        if self.background_state:
            out["degraded"] = self.background_state
        caps = self.capabilities()
        out["can_do"] = sorted(caps.commands)
        return out

    # -- history ----------------------------------------------------------------------------
    def history(self, kind: str = "summary", *, days: Optional[int] = 30, start: Optional[float] = None,
                end: Optional[float] = None) -> Dict[str, Any]:
        """Aggregated trips / charges / efficiency / battery health (never raises)."""
        from .brain.history import history_report

        kind = (kind or "summary").strip().lower()
        if kind not in ("summary", "trips", "charges", "efficiency", "battery"):
            return {"ok": False, "error": f"unknown kind '{kind}'",
                    "hint": "use summary, trips, charges, efficiency or battery"}
        try:
            provider = self.history_provider
            return history_report(provider, kind, self.now(), days=days, start=start, end=end, tz=self.local_tz())
        except AdapterError as exc:
            return {"ok": False, "error": scrub(str(exc))}

    def import_history(self, source: str = "teslamate", days: int = 365) -> Dict[str, Any]:
        """Backfill the own history store from an optional source (TeslaMate). Idempotent. Never raises."""
        days = max(1, min(int(days), 3650))
        try:
            provider = adapters_base.build_history(source, self._build_ctx())
            if provider is None:
                return {"ok": False, "error": f"unknown history source '{source}'"}
            now = self.now()
            counts = import_history(self.store, provider, now - days * 86400.0, now, source=source)
        except AdapterError as exc:
            return {"ok": False, "error": scrub(str(exc))}
        return {"ok": True, "source": source, "days": days, **counts}

    # -- readiness --------------------------------------------------------------------------
    def readiness(self, departure: Optional[str] = None, trip_mi: Optional[float] = None, *,
                  round_trip: bool = False, expected_temp_f: Optional[float] = None) -> Dict[str, Any]:
        """"Will the car be ready for <departure>?" (never raises)."""
        from datetime import timedelta
        from .brain import readiness as rd
        from .brain.timeparse import WhenError, parse_when
        from .settings import zones_list

        snap = self.settings.snapshot()
        now_dt = self.local_dt()
        try:
            when = parse_when(departure or snap["default_departure"], now_dt)
        except WhenError as exc:
            return {"ok": False, "error": scrub(str(exc)),
                    "hint": "give a departure like 'tomorrow 7:30 AM' or an ISO date-time"}
        if float(snap["battery_kwh"] or 0) <= 0:   # R18
            return {"ok": False, "error": "the battery_kwh setting must be greater than 0"}
        miles = float(trip_mi) if trip_mi is not None else float(snap["default_trip_mi"])
        if not (0 < miles <= 1500):
            return {"ok": False, "error": "trip_mi must be between 0 and 1500 miles"}
        if when.timestamp() - now_dt.timestamp() > 14 * 86400:
            return {"ok": False, "error": "departure is more than 14 days away"}
        try:
            state = self.get_state()
        except AdapterError as exc:
            return {"ok": False, "error": scrub(str(exc))}
        drives = []
        battery_kwh, battery_src = float(snap["battery_kwh"]), "your battery_kwh setting"
        provider = self.history_provider
        if provider is not None:
            try:
                drives = provider.drives(self.now() - 60 * 86400, self.now(), 600)
                bh = provider.battery_health()
                if bh and bh.current_capacity_kwh:
                    battery_kwh, battery_src = float(bh.current_capacity_kwh), "measured battery capacity (history)"
            except AdapterError as exc:
                logger.info("readiness: history unavailable (%s); using defaults", exc)
        result = rd.evaluate(
            state, when, now_dt, trip_mi=miles, round_trip=bool(round_trip), expected_temp_f=expected_temp_f,
            drives=drives, battery_kwh=battery_kwh, battery_kwh_source=battery_src,
            reserve_pct=float(snap["reserve_pct"]), can_do=self.capabilities().commands,
            home_zones=zones_list(snap["trusted_zones"]) or ["home"],
            last_charge_volts=self.store.kv_get("last_charge_volts"), scheduled_start=self._scheduled_start(snap, now_dt))
        result["vehicle"] = {"name": state.name, "simulated": state.simulated}
        result["armed"] = self.safety_posture().get("armed", False)
        return result

    @staticmethod
    def _scheduled_start(snap: Any, now_dt: Any) -> Optional[Any]:
        """Next occurrence of the owner's scheduled/off-peak charging start (``charge_schedule_start``), if set."""
        from datetime import timedelta
        from .settings import parse_hhmm
        text = str(snap.get("charge_schedule_start") or "").strip()
        if not text:
            return None
        h, m = parse_hhmm(text, (-1, -1))
        if h < 0:
            return None
        start = now_dt.replace(hour=h, minute=m, second=0, microsecond=0)
        return start if start > now_dt else start + timedelta(days=1)

    # -- Home Assistant discovery ------------------------------------------------------------
    def discover_ha(self) -> Dict[str, Any]:
        """Read-only: propose an HA entity mapping from /api/states (never raises)."""
        from .adapters.discovery import discover
        from .adapters.http import json_request, normalize_base_url

        snap = self.settings.snapshot()
        try:
            base = normalize_base_url(str(snap["ha_url"]), "Home Assistant")
            token = self._secret(ENV_HA_TOKEN)   # SecretScopeUnavailable is an AdapterError: reported as such below
            if not token:
                raise AdapterError("Home Assistant access token is missing. Paste a long-lived token into Settings → Plugins → Pit Crew → “Home Assistant · Access token”")
            states = json_request("GET", base + "/api/states", token=token, what="Home Assistant",
                                  verify_tls=bool(snap["ha_verify_tls"]))
        except AdapterError as exc:
            return {"ok": False, "error": scrub(str(exc))}
        if not isinstance(states, list):
            return {"ok": False, "error": "Home Assistant /api/states did not return a list"}
        return {"ok": True, **discover(states)}

    # -- safety: armed state, engine, approvals -------------------------------------------------
    @property
    def engine(self):
        if self._engine is None:
            from .safety.engine import SafetyEngine

            self._engine = SafetyEngine(self)
        return self._engine

    @property
    def broker(self):
        return self._broker

    def armed_info(self) -> Dict[str, Any]:
        """Effective armed state. Armed = a still-running ``/car arm`` OR the standing ``armed`` setting
        (unless the user ran ``/car disarm``, which overrides the setting until they arm again)."""
        wall = self._wall()
        st = self.store.kv_get("arm_state", {}) or {}
        until = st.get("until")
        runtime = bool(until) and float(until) > wall
        config = bool(self.settings.get("armed")) and not st.get("forced_disarm")
        return {"armed": runtime or config, "source": "runtime" if runtime else ("setting" if config else None),
                "until": float(until) if runtime else None, "setting_armed": bool(self.settings.get("armed")),
                "forced_disarm": bool(st.get("forced_disarm"))}

    def clamp_hours(self, hours: Optional[float]) -> float:
        """The live-mode duration actually used: between 15 minutes and 72 hours (so callers can say the truth)."""
        try:
            h = float(hours if hours is not None else self.settings.get("arm_default_hours"))
        except (TypeError, ValueError):
            h = float(self.settings.get("arm_default_hours"))
        if h != h:   # NaN
            h = float(self.settings.get("arm_default_hours"))
        return max(0.25, min(h, 72.0))

    def arm(self, hours: Optional[float] = None, *, by: str = "") -> Dict[str, Any]:
        hours = self.clamp_hours(hours)
        self.store.kv_set("arm_state", {"until": self._wall() + hours * 3600.0, "forced_disarm": False})
        self._audit_control("arm", by, f"live mode on for {hours:g} h")
        info = self.armed_info()
        info["hours"] = hours
        info["until_local"] = self.local_dt(info["until"]).isoformat(timespec="minutes") if info["until"] else None
        return info

    def disarm(self, *, by: str = "") -> Dict[str, Any]:
        self.store.kv_set("arm_state", {"until": None, "forced_disarm": True})
        self._audit_control("disarm", by, "back to practice mode")
        return self.armed_info()

    def _audit_control(self, what: str, by: str, detail: str) -> None:
        self.store.audit_add(ts=self._wall(), surface=by or "unknown", session="", command=what, args={}, risk="control",
                             mode="control", policy="n/a", approval="not_required", status="confirmed", detail=detail,
                             readback="n/a", readback_ts=None, adapter=self.describe_adapter().get("kind", ""))

    def safety_posture(self) -> Dict[str, Any]:
        info = self.armed_info()
        snap = self.settings.snapshot()
        out: Dict[str, Any] = {
            "armed": info["armed"], "mode": "live" if info["armed"] else "dry_run",
            "armed_by": info["source"], "trusted_zones": list(snap["trusted_zones"]),
            "max_state_age_s": snap["max_state_age_s"], "approval_confirmation": snap["approval_confirmation"],
            "rate_limit": f"{snap['rate_limit_commands']} per {int(snap['rate_limit_window_s'] // 60)} min"}
        if info["until"]:
            out["armed_until"] = self.local_dt(info["until"]).isoformat(timespec="minutes")
        return out

    def is_unattended(self) -> bool:
        """True inside a scheduled (cron) run, where no human can answer an approval prompt."""
        ctx = self.ctx
        if ctx is None:
            return False
        try:
            return ctx.current_cron_execution() is not None
        except Exception as exc:
            # If we cannot tell, fail closed: treat as unattended.
            logger.warning("could not determine cron context (%s); treating as unattended", exc)
            return True

    @property
    def watcher(self):
        if self._watcher is None:
            from .watcher.watcher import Watcher

            self._watcher = Watcher(self)
        return self._watcher

    def alerts(self, states=("new", "surfaced", "acked"), limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.store.alerts_list(states, limit=limit)
        for r in rows:
            r["when"] = self.local_dt(r["ts"]).isoformat(timespec="minutes")
        return rows

    def audit_log(self, limit: int = 30) -> List[Dict[str, Any]]:
        rows = self.store.audit_recent(limit)
        for r in rows:
            r["when"] = datetime.fromtimestamp(r["ts"], tz=self.local_tz()).isoformat(timespec="seconds")
        return rows

    def run_command(self, command: str, args: Optional[Dict[str, Any]] = None, **kw: Any) -> Dict[str, Any]:
        """Run a command through the safety engine (never raises)."""
        try:
            result = self.engine.execute(command, args or {}, **kw)
            from .events import notify_ui

            notify_ui("state.changed", {"command": command, "status": result.get("status")})
            return result
        except Exception as exc:  # defensive: report, never crash the agent loop
            logger.exception("command execution crashed")
            return {"ok": False, "status": "failed", "command": command, "armed": self.armed_info()["armed"],
                    "message": f"Internal error while running the command: {type(exc).__name__}. Nothing further was sent."}


# --------------------------------------------------------------------------------------
# Process-wide singleton
# --------------------------------------------------------------------------------------
_service: Optional[CarService] = None
_service_lock = threading.Lock()


_plugin_ctx: Any = None


def attach_ctx(ctx: Any) -> None:
    """Remember the Hermes PluginContext (set by ``register``); applied lazily so registering the
    plugin never touches the filesystem."""
    global _plugin_ctx
    _plugin_ctx = ctx
    if _service is not None:
        _service.ctx = ctx


def get_service() -> CarService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                svc = CarService(Settings(hermes_settings_provider()), hermes_secret_getter, open_store())
                svc.ctx = _plugin_ctx
                _service = svc
    return _service


def set_service(service: Optional[CarService]) -> None:
    """Install (or clear) the process-wide service — used by tests and by the loader."""
    global _service
    with _service_lock:
        _service = service
