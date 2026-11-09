"""Direct Tesla adapter — no Home Assistant needed.

One adapter, two connection modes, both through the ``tesla-fleet-api`` package (Apache-2.0, the library Home
Assistant's own Tesla integrations use):

* ``teslemetry`` — Teslemetry: a static API access token.
* ``tessie``     — Tessie: a static API access token.

The token is a declared secret read ONLY through Hermes' secret scope (``agent.secret_scope.get_secret``); the plugin never
reads files or the environment for credentials, and never refreshes, rotates or stores OAuth tokens (catalog rule 11).
Tesla's own Fleet API (your developer app + OAuth) is supported *through Home Assistant's official Tesla Fleet integration*
(use the ``homeassistant`` adapter): Home Assistant holds and refreshes those tokens. A native Fleet-direct mode is planned,
pending a Hermes OAuth enhancement (token-exchange ``audience`` + a fixed redirect port).

The package is imported lazily (the plugin loads, and every other adapter works, without it).

Never wakes a sleeping car to poll
----------------------------------
``fetch_state`` first asks the cheap vehicle-state endpoint whether the car is online. Only an **online** car gets a
data call; for an asleep/offline car the last known data is returned, marked ``asleep`` and aging naturally (so it shows
as stale), and nothing is sent that could wake it. Data calls are also rate-limited by us (``tesla_min_poll_s``), back
off on HTTP 429, and the library's ``wake_up`` is only ever called for a *command* the human explicitly allowed to wake the
car (``wake_ok`` + approval where required) — never for polling.

Privacy: latitude/longitude are read only when you configured zones, converted to a zone name immediately, and discarded.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..brain.commands import COMMANDS, Capabilities
from ..brain.models import FIELD_NAMES, FieldValue, VehicleState
from ..brain.units import PSI_PER_BAR, c_to_f
from ..constants import ENV_TESLA_TOKEN
from ..redact import scrub
from .base import (AdapterError, AdapterUnavailable, BuildContext, CarAdapter, CommandNotSupported, CommandResult)

logger = logging.getLogger(__name__)

PIP_REQUIREMENT = "tesla-fleet-api>=1.5,<2"
MODES = ("teslemetry", "tessie")
DATA_ENDPOINTS = ["charge_state", "climate_state", "drive_state", "vehicle_state"]
CALL_TIMEOUT_S = 40.0
RATE_LIMIT_BACKOFF_S = 300.0
ERROR_BACKOFF_S = 60.0   # after a failed data call: serve the cache instead of re-asking every poll
BOOST_WINDOW_S = 45.0
BOOST_MIN_POLL_S = 4.0
SEAT_IDS = {"driver": 0, "passenger": 1, "rear_left": 2, "rear_center": 4, "rear_right": 5}
CHARGING_MAP = {"charging": "charging", "complete": "complete", "stopped": "stopped", "starting": "starting",
                "disconnected": "disconnected", "nopower": "no_power"}
ERROR_TEXT = {
    "InvalidToken": "Tesla/the service rejected the access token — create a new one",
    "InvalidTeslemetryToken": "Teslemetry rejected the access token — create a new one",
    "InvalidTessieToken": "Tessie rejected the access token — create a new one",
    "MissingToken": "no access token was provided",
    "RateLimited": "the service is rate limiting requests; backing off",
    "VehicleOffline": "the car is offline/asleep",
    "PaymentRequired": "the service reports a billing/subscription problem",
    "SubscriptionRequired": "the Teslemetry subscription does not cover this",
    "VehicleSubscriptionRequired": "this car is not covered by the Teslemetry subscription",
    "MobileAccessDisabled": "mobile access is disabled in the car's settings",
    "Forbidden": "the token is not allowed to do that (missing scope?)",
    "InvalidScope": "the token lacks a required scope",
    "NotFound": "the car/VIN was not found for this account",
    "UnsupportedVehicle": "this vehicle is not supported by that API",
    "Locked": "the car refused: it is locked/in a state that blocks this",
    "PreconditionFailed": "the car's current state does not allow that command",
    "GatewayTimeout": "the service timed out talking to the car",
    "ServiceUnavailable": "the service is unavailable right now",
}


class WakeRefused(AdapterError):
    """Raised when a command needs a sleeping car to be woken and nobody allowed that."""


@dataclass
class TeslaConfig:
    mode: str
    vin: str
    token: Optional[str] = None            # Teslemetry / Tessie static API token (from get_secret)
    min_poll_s: float = 120.0
    zones: Dict[str, Tuple[float, float, float]] = field(default_factory=dict)  # name -> (lat, lon, radius_m)
    name: str = ""


# ----------------------------------------------------------------------------------------------------------
# Pure mapping (no I/O): Tesla "vehicle_data" JSON -> VehicleState
# ----------------------------------------------------------------------------------------------------------
def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def zone_for(lat: Any, lon: Any, zones: Mapping[str, Tuple[float, float, float]]) -> Optional[str]:
    """'home' / a named zone / 'away'. The coordinates exist only inside this call."""
    if not zones or not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    best: Optional[Tuple[float, str]] = None
    for name, (zlat, zlon, radius) in zones.items():
        d = _haversine_m(lat, lon, zlat, zlon)
        if d <= radius and (best is None or d < best[0]):
            best = (d, name)
    return best[1] if best else "away"


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def tesla_data_to_state(data: Mapping[str, Any], now: float, *, name: str,
                        zones: Optional[Mapping[str, Tuple[float, float, float]]] = None,
                        source: str = "tesla") -> VehicleState:
    """Map the Fleet-API-shaped ``vehicle_data`` response (``{"response": {...}}`` or the inner dict)."""
    d = dict(data.get("response") or data) if isinstance(data, Mapping) else {}
    cs = d.get("charge_state") or {}
    cl = d.get("climate_state") or {}
    vs = d.get("vehicle_state") or {}
    ds = d.get("drive_state") or {}
    fields: Dict[str, FieldValue] = {}

    def stamp(section: Mapping[str, Any]) -> float:
        ts = _num(section.get("timestamp"))
        return ts / 1000.0 if ts and ts > 1e11 else (ts if ts and ts > 1e9 else now)

    def put(fname: str, value: Any, section: Mapping[str, Any]) -> None:
        if value is not None:
            fields[fname] = FieldValue(value, min(stamp(section), now))

    if cs:
        put("battery_pct", _num(cs.get("battery_level")), cs)
        put("range_mi", _num(cs.get("battery_range")), cs)
        raw = str(cs.get("charging_state") or "").replace("_", "").lower()
        if raw:
            put("charging_state", CHARGING_MAP.get(raw, "unknown"), cs)
            put("plugged_in", raw != "disconnected", cs)
        put("charge_power_kw", _num(cs.get("charger_power")), cs)
        put("charge_limit_pct", _num(cs.get("charge_limit_soc")), cs)
        put("charge_eta_min", _num(cs.get("minutes_to_full_charge")), cs)
        put("charge_current_a", _num(cs.get("charger_actual_current")), cs)
        put("charge_current_max_a", _num(cs.get("charge_current_request_max")), cs)
        put("charger_voltage_v", _num(cs.get("charger_voltage")), cs)
        if isinstance(cs.get("charge_port_door_open"), bool):
            put("charge_port_open", cs["charge_port_door_open"], cs)
    if vs:
        if isinstance(vs.get("locked"), bool):
            put("locked", vs["locked"], vs)
        if any(k in vs for k in ("df", "dr", "pf", "pr")):
            put("doors_open", [n for n, k in (("front_driver", "df"), ("rear_driver", "dr"), ("front_passenger", "pf"),
                                               ("rear_passenger", "pr")) if vs.get(k)], vs)
        if any(k in vs for k in ("fd_window", "fp_window", "rd_window", "rp_window")):
            put("windows_open", [n for n, k in (("front_driver", "fd_window"), ("front_passenger", "fp_window"),
                                                 ("rear_driver", "rd_window"), ("rear_passenger", "rp_window"))
                                 if vs.get(k)], vs)
        if "rt" in vs:
            put("trunk_open", bool(vs.get("rt")), vs)
        if "ft" in vs:
            put("frunk_open", bool(vs.get("ft")), vs)
        if isinstance(vs.get("sentry_mode"), bool):
            put("sentry_on", vs["sentry_mode"], vs)
        if isinstance(vs.get("is_user_present"), bool):
            put("user_present", vs["is_user_present"], vs)
        ver = str(vs.get("car_version") or "").split(" ")[0]
        put("software_version", ver or None, vs)
        upd = (vs.get("software_update") or {}).get("status")
        if upd is not None or "software_update" in vs:
            put("software_update_available", str(upd or "").lower() in ("available", "scheduled", "downloading",
                                                                         "downloading_wifi_wait", "installing"), vs)
        put("odometer_mi", _num(vs.get("odometer")), vs)
        # Tire pressures arrive in bar. Each has its own "last seen" epoch: a tire sensor that has not reported for
        # hours must look old, not fresh because the rest of the vehicle_state block is.
        for pos in ("fl", "fr", "rl", "rr"):
            bar = _num(vs.get("tpms_pressure_" + pos))
            if bar is not None and bar > 0:
                seen = _num(vs.get("tpms_last_seen_pressure_time_" + pos))
                at = seen if seen and 1e9 < seen <= now else stamp(vs)
                fields["tire_psi_" + pos] = FieldValue(round(bar * PSI_PER_BAR, 1), min(at, now))
        # The car's own TPMS warnings: soft = low pressure, hard = very low. Either one means "this tire needs attention".
        for pos in ("fl", "fr", "rl", "rr"):
            flags = [vs.get(f"tpms_{kind}_warning_{pos}") for kind in ("soft", "hard")]
            known = [x for x in flags if isinstance(x, bool)]
            if known:
                put("tire_warn_" + pos, any(known), vs)
        for axle, key in (("front", "tpms_rcp_front_value"), ("rear", "tpms_rcp_rear_value")):
            bar = _num(vs.get(key))
            if bar is not None and bar > 0:
                put(f"tire_rec_{axle}_psi", round(bar * PSI_PER_BAR, 1), vs)
    if cl:
        if isinstance(cl.get("is_climate_on"), bool):
            put("climate_on", cl["is_climate_on"], cl)
        for fname, key in (("cabin_temp_f", "inside_temp"), ("outside_temp_f", "outside_temp"),
                           ("target_temp_f", "driver_temp_setting")):
            c = _num(cl.get(key))
            put(fname, None if c is None else round(c_to_f(c), 1), cl)
        if "defrost_mode" in cl:
            put("defrost_on", bool(cl.get("defrost_mode")), cl)
    if ds:
        shift = ds.get("shift_state")
        shift = (str(shift).upper() if shift else "P")
        put("shift_state", shift, ds)
        speed = _num(ds.get("speed")) or 0.0
        put("speed_mph", speed, ds)
        put("moving", shift in ("D", "R", "N") or speed > 1.0, ds)
        if zones:
            put("location_zone", zone_for(ds.get("latitude"), ds.get("longitude"), zones), ds)
    fields["asleep"] = FieldValue(False, now)
    for f in FIELD_NAMES:
        fields.setdefault(f, FieldValue(None, None, False))
    # Teslas report their recommended pressure; older firmware may not, and a Model 3/Y placard is 42 psi (90% = 38)
    return VehicleState(name=name, source=source, captured_at=now, fields=fields, hints={"tire_min_psi": 38.0})


# ----------------------------------------------------------------------------------------------------------
# Async plumbing: the library is asyncio-only; the adapter API is synchronous.
# ----------------------------------------------------------------------------------------------------------
class AsyncRunner:
    """Runs coroutines on one private event loop in a daemon thread (the aiohttp session lives there)."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def _ensure(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                t = threading.Thread(target=loop.run_forever, name="pit-crew-tesla", daemon=True)
                t.start()
                self._loop, self._thread = loop, t
            return self._loop

    def run(self, coro: Any, timeout: float = CALL_TIMEOUT_S) -> Any:
        loop = self._ensure()
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return fut.result(timeout)
        except FutureTimeout:
            fut.cancel()
            raise AdapterUnavailable("the Tesla service did not answer in time") from None

    def close(self) -> None:
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        if not loop.is_running():
            loop.close()                      # release the selector and self-pipe descriptors (one pair per replaced adapter)


def describe_error(exc: BaseException) -> str:
    """Safe, secret-free text for a library error (its class name picks the message)."""
    for cls in type(exc).__mro__:
        if cls.__name__ in ERROR_TEXT:
            return ERROR_TEXT[cls.__name__]
    text = scrub(str(exc))[:160]
    return f"{type(exc).__name__}" + (f": {text}" if text else "")


# ----------------------------------------------------------------------------------------------------------
# The client: a thin, mockable wrapper around tesla-fleet-api (all library knowledge is in this class)
# ----------------------------------------------------------------------------------------------------------
class LibraryClient:
    """``vehicle_state() / vehicle_data() / command() / wake()`` over tesla-fleet-api. Tests replace the whole
    class (or inject a fake ``lib`` module); nothing outside this class imports the package."""

    def __init__(self, cfg: TeslaConfig, runner: AsyncRunner, lib: Any = None, session_factory: Any = None) -> None:
        self._session_factory = session_factory
        self._cfg = cfg
        self._runner = runner
        self._lib = lib
        self._api: Any = None
        self._vehicle: Any = None
        self._session: Any = None
        self._build_lock: Optional[asyncio.Lock] = None

    # -- lazy import / construction ------------------------------------------------------------------
    def _import(self) -> Any:
        if self._lib is None:
            try:
                import tesla_fleet_api  # lazy: the plugin works without it

                self._lib = tesla_fleet_api
            except ImportError:
                raise AdapterError("the direct Tesla adapter needs the 'tesla-fleet-api' package. Install it with "
                                   f"`pip install \"{PIP_REQUIREMENT}\"` into Hermes' Python (hermes plugins install does "
                                   "this for you), then restart") from None
        return self._lib

    async def _build(self) -> None:
        if self._vehicle is not None:
            return
        if self._build_lock is None:
            self._build_lock = asyncio.Lock()
        async with self._build_lock:
            if self._vehicle is not None:
                return
            lib, cfg = self._import(), self._cfg
            if self._session_factory is not None:
                self._session = self._session_factory()
            else:
                import aiohttp  # a dependency of tesla-fleet-api

                self._session = aiohttp.ClientSession()
            if cfg.mode == "teslemetry":
                self._api = lib.Teslemetry(self._session, cfg.token)
            else:
                self._api = lib.Tessie(self._session, cfg.token)
            self._vehicle = self._api.vehicles.createFleet(cfg.vin)

    def close(self) -> None:
        """Close the aiohttp session on the runner's loop, then stop the loop thread. Never raises."""
        try:
            session = self._session
            if session is not None and getattr(self._runner, "_loop", None) is not None:
                async def _shut() -> None:
                    await session.close()

                self._runner.run(_shut(), timeout=5.0)
        except Exception:  # noqa: BLE001
            logger.debug("tesla session close failed", exc_info=True)
        finally:
            self._session = None
            try:
                self._runner.close()
            except Exception:  # noqa: BLE001
                logger.debug("tesla runner close failed", exc_info=True)

    async def _guard(self, coro: Any) -> Any:
        """Await a library call, turning its BaseException-derived errors into AdapterError."""
        try:
            return await coro
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except AdapterError:
            raise
        except BaseException as exc:  # tesla_fleet_api.TeslaFleetError derives from BaseException
            if type(exc).__name__ == "RateLimited":
                raise RateLimitedError(describe_error(exc)) from None
            raise AdapterError(describe_error(exc)) from None

    # -- operations --------------------------------------------------------------------------------------
    def vehicle_state(self) -> str:
        """'online' | 'asleep' | 'offline' (cheap; never wakes the car)."""
        async def go() -> str:
            await self._build()
            if self._cfg.mode == "tessie":
                resp = await self._guard(self._vehicle.state(use_cache=True))
            else:
                resp = await self._guard(self._vehicle.vehicle())
            inner = resp.get("response", resp) if isinstance(resp, dict) else {}
            return str(inner.get("state") or "online").lower()
        return self._runner.run(go())

    def vehicle_data(self, with_location: bool) -> Dict[str, Any]:
        async def go() -> Dict[str, Any]:
            await self._build()
            endpoints = DATA_ENDPOINTS + (["location_data"] if with_location else [])
            if self._cfg.mode == "tessie":
                resp = await self._guard(self._vehicle.state(use_cache=True))
            else:
                resp = await self._guard(self._vehicle.vehicle_data(endpoints=endpoints))
            return resp if isinstance(resp, dict) else {}
        return self._runner.run(go())

    def wake(self) -> None:
        async def go() -> None:
            await self._build()
            if self._cfg.mode == "tessie":
                await self._guard(self._vehicle.wake())
            else:
                await self._guard(self._vehicle.wake_up(wait=True))
        self._runner.run(go(), timeout=90.0)

    def command(self, name: str, args: Mapping[str, Any]) -> Tuple[bool, str]:
        async def go() -> Tuple[bool, str]:
            await self._build()
            call = self._plan(name, args)
            resp = await self._guard(call)
            inner = resp.get("response", resp) if isinstance(resp, dict) else {}
            ok = inner.get("result", True) if isinstance(inner, dict) else True
            reason = str(inner.get("reason") or "") if isinstance(inner, dict) else ""
            return bool(ok), reason
        return self._runner.run(go())

    # -- command mapping (per backend) -----------------------------------------------------------------------
    def _plan(self, name: str, a: Mapping[str, Any]) -> Any:
        v = self._vehicle
        tessie = self._cfg.mode == "tessie"
        c = (lambda f: round((float(f) - 32.0) * 5.0 / 9.0 * 2) / 2.0)
        if tessie:
            return self._plan_tessie(v, name, a, c)
        if name == "climate_on":
            async def climate_on() -> Any:
                if "temp_f" in a:
                    await v.set_temps(c(a["temp_f"]), c(a["temp_f"]))
                return await v.auto_conditioning_start()
            return climate_on()
        simple = {
            "climate_off": lambda: v.auto_conditioning_stop(),
            "set_temp": lambda: v.set_temps(c(a["temp_f"]), c(a["temp_f"])),
            "defrost": lambda: v.set_preconditioning_max(bool(a.get("on", True)), False),
            "seat_heat": lambda: v.remote_seat_heater_request(SEAT_IDS[a["seat"]], int(a["level"])),
            "charge_limit": lambda: v.set_charge_limit(int(a["percent"])),
            "charge_start": lambda: v.charge_start(),
            "charge_stop": lambda: v.charge_stop(),
            "charge_amps": lambda: v.set_charging_amps(int(a["amps"])),
            "lock": lambda: v.door_lock(),
            "unlock": lambda: v.door_unlock(),
            "open_trunk": lambda: v.actuate_trunk("rear"),
            "open_frunk": lambda: v.actuate_trunk("front"),
            "vent_windows": lambda: v.window_control("vent"),
            "close_windows": lambda: v.window_control("close"),
            "sentry_on": lambda: v.set_sentry_mode(True),
            "sentry_off": lambda: v.set_sentry_mode(False),
            "honk": lambda: v.honk_horn(),
            "flash": lambda: v.flash_lights(),
        }
        if name not in simple:
            raise CommandNotSupported(f"the direct Tesla adapter has no '{name}' command")
        return simple[name]()

    @staticmethod
    def _plan_tessie(v: Any, name: str, a: Mapping[str, Any], c: Any) -> Any:
        if name == "climate_on":
            async def climate_on() -> Any:
                if "temp_f" in a:
                    await v.set_temperatures(c(a["temp_f"]))
                return await v.start_climate()
            return climate_on()
        simple = {
            "climate_off": lambda: v.stop_climate(),
            "set_temp": lambda: v.set_temperatures(c(a["temp_f"])),
            "seat_heat": lambda: v.set_seat_heat(a["seat"], int(a["level"])),
            "charge_limit": lambda: v.set_charge_limit(int(a["percent"])),
            "charge_start": lambda: v.start_charging(),
            "charge_stop": lambda: v.stop_charging(),
            "charge_amps": lambda: v.tessie_set_charging_amps(int(a["amps"])),
            "lock": lambda: v.lock(),
            "unlock": lambda: v.unlock(),
            "open_trunk": lambda: v.activate_rear_trunk(),
            "open_frunk": lambda: v.activate_front_trunk(),
            "vent_windows": lambda: v.vent_windows(),
            "close_windows": lambda: v.close_windows(),
            "sentry_on": lambda: v.enable_sentry(),
            "sentry_off": lambda: v.disable_sentry(),
            "honk": lambda: v.honk(),
            "flash": lambda: v.flash(),
        }
        if name not in simple:
            raise CommandNotSupported(f"the Tessie mode has no '{name}' command")
        return simple[name]()


class RateLimitedError(AdapterUnavailable):
    pass


# ----------------------------------------------------------------------------------------------------------
# The adapter
# ----------------------------------------------------------------------------------------------------------
class TeslaDirectAdapter(CarAdapter):
    kind = "tesla"

    def close(self) -> None:
        closer = getattr(self._client, "close", None)
        if callable(closer):
            closer()

    def __init__(self, cfg: TeslaConfig, client: Any) -> None:
        self._cfg = cfg
        self._client = client
        self._lock = threading.RLock()
        self._cache: Optional[VehicleState] = None
        self._last_data_at: float = 0.0
        self._backoff_until: float = 0.0
        self._error_until: float = 0.0
        self._last_error: str = ""
        self._boost_until: float = 0.0
        self._wake_once = False
        self._last_seen_now = 0.0
        self._asleep = False
        self.label = {"teslemetry": "Tesla via Teslemetry",
                      "tessie": "Tesla via Tessie"}[cfg.mode]
        self.stats = {"state_calls": 0, "data_calls": 0, "wake_calls": 0}

    # -- capabilities ---------------------------------------------------------------------------------------
    def _can_command(self) -> bool:
        return True  # Teslemetry and Tessie sign/relay commands server-side; the token is enough

    def capabilities(self) -> Capabilities:
        fields = set(FIELD_NAMES)
        if not self._cfg.zones:
            fields.discard("location_zone")
        return Capabilities(frozenset(fields), frozenset(COMMANDS), history=False, notes=self.label)

    # -- state ----------------------------------------------------------------------------------------------
    def _min_poll(self, now: float) -> float:
        return BOOST_MIN_POLL_S if now < self._boost_until else float(self._cfg.min_poll_s)

    def _asleep_view(self, now: float) -> VehicleState:
        """Last known data, flagged asleep; field timestamps are untouched so they age and show as stale."""
        if self._cache is None:
            fields = {f: FieldValue(None, None, False) for f in FIELD_NAMES}
            fields["asleep"] = FieldValue(True, now)
            return VehicleState(name=self._cfg.name or "Tesla", source=self.kind, captured_at=now, fields=fields)
        st = self._cache.copy()
        st.captured_at = now
        st.fields["asleep"] = FieldValue(True, now)
        return st

    def fetch_state(self, now: float) -> VehicleState:
        with self._lock:
            self._last_seen_now = now
            if now < self._backoff_until:
                if self._cache is not None:
                    return self._cached(now)
                raise AdapterUnavailable(f"rate limited by the service; retrying in {int(self._backoff_until - now)} s")
            if now < self._error_until:
                # a data call just failed: don't hammer a metered/rate-limited API with two calls per poll while it does
                if self._cache is not None:
                    return self._cached(now)
                raise AdapterUnavailable(f"the Tesla service returned an error; retrying in {int(self._error_until - now)} s ({self._last_error})")
            if self._cache is not None and now - self._last_data_at < self._min_poll(now):
                return self._cached(now)
            try:
                self.stats["state_calls"] += 1
                state_name = self._client.vehicle_state()
                self._asleep = state_name != "online"
                if self._asleep:
                    self._last_data_at = now  # don't re-ask the cheap endpoint more often than the poll interval
                    return self._asleep_view(now)
                self.stats["data_calls"] += 1
                data = self._client.vehicle_data(with_location=bool(self._cfg.zones))
            except RateLimitedError:
                self._backoff_until = now + RATE_LIMIT_BACKOFF_S
                logger.warning("Tesla service rate limit hit; backing off for %d s", RATE_LIMIT_BACKOFF_S)
                if self._cache is not None:
                    return self._cached(now)
                raise
            except AdapterError as exc:
                self._error_until, self._last_error = now + ERROR_BACKOFF_S, str(exc)[:200]
                raise
            st = tesla_data_to_state(data, now, name=self._cfg.name or "Tesla", zones=self._cfg.zones or None,
                                     source=self.kind)
            self._cache, self._last_data_at = st, now
            return st

    def _cached(self, now: float) -> VehicleState:
        assert self._cache is not None
        if self._asleep:
            return self._asleep_view(now)
        st = self._cache.copy()
        st.captured_at = now
        return st

    # -- commands -----------------------------------------------------------------------------------------------
    def allow_wake_once(self) -> None:
        """Called by the safety engine when the human allowed waking the car for the next command only."""
        self._wake_once = True

    def send_command(self, command: str, args: Mapping[str, Any]) -> CommandResult:
        with self._lock:
            wake_ok, self._wake_once = self._wake_once, False
            if self._asleep or self._cache is None:
                try:
                    self.stats["state_calls"] += 1
                    online = self._client.vehicle_state() == "online"
                except AdapterError:
                    online = False
                if not online:
                    if not wake_ok:
                        raise WakeRefused("the car is asleep and waking it was not allowed for this command")
                    self.stats["wake_calls"] += 1
                    self._client.wake()  # only here, only on explicit permission
            ok, reason = self._client.command(command, dict(args))
            self._boost_until = self._last_seen_now + BOOST_WINDOW_S
            if not ok:
                return CommandResult(False, reason or "the car declined the command")
            return CommandResult(True, "sent to " + self.label)

    def describe(self) -> Dict[str, Any]:
        return {"kind": self.kind, "label": self.label, "simulated": False, "mode": self._cfg.mode}


# ----------------------------------------------------------------------------------------------------------
def _setting_float(s: Mapping[str, Any], key: str, default: float, label: str) -> float:
    """A numeric setting; garbage is a clear AdapterError, not a ValueError out of the factory."""
    raw = s.get(key)
    if raw in (None, ""):
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise AdapterError(f"“{label}” must be a number (got {str(raw)[:20]!r})") from None
    if not math.isfinite(val):
        raise AdapterError(f"“{label}” must be a finite number")
    return val


def _zones_from_settings(s: Mapping[str, Any]) -> Dict[str, Tuple[float, float, float]]:
    zones: Dict[str, Tuple[float, float, float]] = {}
    lat = _setting_float(s, "tesla_home_latitude", 0.0, "Tesla · Home latitude")
    lon = _setting_float(s, "tesla_home_longitude", 0.0, "Tesla · Home longitude")
    if lat or lon:
        zones["home"] = (lat, lon, _setting_float(s, "tesla_home_radius_m", 150.0, "Tesla · Home radius (m)") or 150.0)
    extra = s.get("tesla_zones") or {}
    if not isinstance(extra, Mapping):
        logger.warning("ignoring tesla_zones: expected a mapping of name -> [lat, lon, radius_m]")
        extra = {}
    for zname, spec in extra.items():
        try:
            zlat, zlon, zr = spec
            zones[str(zname).strip().lower()] = (float(zlat), float(zlon), float(zr))
        except (TypeError, ValueError):
            logger.warning("ignoring malformed tesla_zones entry %r (expected [lat, lon, radius_m])", zname)
    return zones


def build_tesla(ctx: BuildContext, *, client_factory: Any = None) -> TeslaDirectAdapter:
    s = ctx.settings
    mode = str(s.get("tesla_mode") or "teslemetry").strip().lower()
    if mode not in MODES:
        raise AdapterError(f"tesla_mode must be one of {', '.join(MODES)}")
    vin = str(s.get("tesla_vin") or "").strip().upper()
    if not vin:
        raise AdapterError("The Tesla VIN isn't set. Enter your car's 17-character VIN in Settings → Plugins → Pit Crew → “Tesla · VIN”")
    cfg = TeslaConfig(mode=mode, vin=vin, min_poll_s=_setting_float(s, "tesla_min_poll_s", 120.0, "Advanced · Min seconds between Tesla data calls") or 120.0,
                      zones=_zones_from_settings(s), name=str(s.get("car_name") or ""))
    # The ONLY way a credential reaches this adapter: a declared secret via Hermes' secret scope (ctx.secret).
    cfg.token = ctx.secret(ENV_TESLA_TOKEN) or ""
    if not cfg.token:
        raise AdapterError(f"The {mode.title()} access token is missing. Paste it into Settings → Plugins → Pit Crew → “Tesla · Access token” (or set {ENV_TESLA_TOKEN} in the profile .env)")
    client = client_factory(cfg) if client_factory else LibraryClient(cfg, AsyncRunner())
    return TeslaDirectAdapter(cfg, client)
