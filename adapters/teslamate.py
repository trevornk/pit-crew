"""TeslaMate adapter — READ-ONLY, via TeslaMateApi.

Only ``GET`` requests to ``/api/v1/cars``, ``/cars/:id/status``, ``/cars/:id/drives``,
``/cars/:id/charges`` and ``/cars/:id/battery-health`` are ever made. TeslaMateApi's command
endpoints are never called (there is intentionally no code path that can), which the tests assert.

It can serve as a history provider next to a Home Assistant live adapter, or alone as a read-only
live-state adapter (no commands).
"""

from __future__ import annotations

import logging
import math
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..brain.commands import Capabilities
from ..brain.models import FIELD_NAMES, FieldValue, VehicleState
from ..constants import ENV_TESLAMATE_TOKEN
from .base import (AdapterError, BatteryHealth, BuildContext, CarAdapter, ChargeRecord, DriveRecord,
                   HistoryProvider)
from .http import json_request, normalize_base_url

logger = logging.getLogger(__name__)

PAGE_SIZE = 100
MAX_PAGES = 10
SLEEPY_STATES = ("asleep", "offline", "suspended")
PARKED_TM_STATES = ("asleep", "online", "charging", "suspended", "updating")   # same allowlist as the HA adapter


def _ts(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _with_units(data: Any, key: str) -> List[Dict[str, Any]]:
    """Rows of ``data[key]``, each tagged with the response's ``units`` so normalisation can convert km/°C (R12)."""
    body = (data or {}).get("data") or {}
    units = body.get("units") or {}
    return [dict(r, _units=units) for r in (body.get(key) or []) if isinstance(r, dict)]


def _conv(raw: Mapping[str, Any]):
    units = raw.get("_units") or {}
    km = units.get("unit_of_length") == "km"
    celsius = units.get("unit_of_temperature") == "C"
    return (0.621371 if km else 1.0), (lambda t: None if t is None else (t * 9 / 5 + 32 if celsius else t))


def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


class TeslaMateClient:
    """Minimal read-only client. Every request goes through :meth:`_get` (HTTP GET only)."""

    def __init__(self, base_url: str, car_id: int = 1, token: Optional[str] = None, timeout: float = 10.0):
        self._base = normalize_base_url(base_url, "TeslaMate API")
        self.car_id = int(car_id)
        self._token = token
        self._timeout = timeout

    def _get(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        url = f"{self._base}/api/v1{path}"
        if params:
            from urllib.parse import urlencode
            url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
        return json_request("GET", url, token=self._token, timeout=self._timeout, what="TeslaMate API")

    def cars(self) -> List[Dict[str, Any]]:
        data = self._get("/cars")
        return list(((data or {}).get("data") or {}).get("cars") or [])

    def status(self) -> Dict[str, Any]:
        """The ``data`` object of ``/cars/:id/status``: ``{"car":…, "status":…, "units":…}``."""
        return ((self._get(f"/cars/{self.car_id}/status") or {}).get("data") or {})

    def drives(self, start: float, end: float, page: int = 1, show: int = PAGE_SIZE) -> List[Dict[str, Any]]:
        data = self._get(f"/cars/{self.car_id}/drives", {
            "startDate": datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDate": datetime.fromtimestamp(end, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "page": page, "show": show})
        return _with_units(data, "drives")

    def charges(self, start: float, end: float, page: int = 1, show: int = PAGE_SIZE) -> List[Dict[str, Any]]:
        data = self._get(f"/cars/{self.car_id}/charges", {
            "startDate": datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDate": datetime.fromtimestamp(end, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "page": page, "show": show})
        return _with_units(data, "charges")

    def battery_health(self) -> Dict[str, Any]:
        return ((self._get(f"/cars/{self.car_id}/battery-health") or {}).get("data") or {}).get("battery_health") or {}


# --------------------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------------------
def normalize_drive(raw: Mapping[str, Any]) -> Optional[DriveRecord]:
    start, end = _ts(raw.get("start_date")), _ts(raw.get("end_date"))
    if start is None or end is None:
        return None
    mult, tf = _conv(raw)
    dur = _num(raw.get("duration_min")) or max(0.0, (end - start) / 60.0)
    avg = _num(raw.get("speed_avg"))
    avg = None if avg is None else avg * mult
    odo = (_num((raw.get("odometer_details") or {}).get("odometer_distance")) or 0.0) * mult
    # Odometer can be zeroed/missing: fall back to average speed x duration.
    miles = odo if odo > 0 else ((avg or 0.0) * dur / 60.0)
    kwh = _num(raw.get("energy_consumed_net"))
    cons = _num(raw.get("consumption_net"))     # kWh per km in a metric install
    wh_mi = (kwh * 1000.0 / miles) if (kwh is not None and miles >= 0.3) else (None if cons is None else cons * 1000.0 / mult)
    smax = _num(raw.get("speed_max"))
    batt = raw.get("battery_details") or {}
    return DriveRecord(
        start=start, end=end, miles=round(miles, 2), duration_min=round(dur, 1),
        start_pct=_num(batt.get("start_battery_level")), end_pct=_num(batt.get("end_battery_level")),
        kwh=kwh, wh_per_mi=round(wh_mi, 1) if wh_mi is not None else None,
        outside_temp_f=tf(_num(raw.get("outside_temp_avg"))), speed_avg_mph=avg,
        speed_max_mph=None if smax is None else smax * mult)


def normalize_charge(raw: Mapping[str, Any]) -> Optional[ChargeRecord]:
    start, end = _ts(raw.get("start_date")), _ts(raw.get("end_date"))
    added = _num(raw.get("charge_energy_added"))
    if start is None or end is None or added is None:
        return None
    batt = raw.get("battery_details") or {}
    _, tf = _conv(raw)
    return ChargeRecord(
        start=start, end=end, kwh_added=added, kwh_used=_num(raw.get("charge_energy_used")),
        cost=_num(raw.get("cost")), start_pct=_num(batt.get("start_battery_level")),
        end_pct=_num(batt.get("end_battery_level")),
        duration_min=_num(raw.get("duration_min")) or max(0.0, (end - start) / 60.0),
        outside_temp_f=tf(_num(raw.get("outside_temp_avg"))))


def status_to_state(data: Mapping[str, Any], name: str, now: float, source: str = "teslamate") -> VehicleState:
    """Map TeslaMateApi's ``data`` object of ``/cars/:id/status`` (car, status, units) to a VehicleState."""
    status = data.get("status") or {}
    car = status.get("car_status") or {}
    batt = status.get("battery_details") or {}
    chg = status.get("charging_details") or {}
    clim = status.get("climate_details") or {}
    drv = status.get("driving_details") or {}
    ver = status.get("car_versions") or {}
    geo = status.get("car_geodata") or {}
    tm_state = str(status.get("state") or "").lower()
    since = _ts(status.get("state_since"))
    # When the car is asleep/suspended TeslaMate shows the last known values; age them from then.
    stamp = since if (tm_state in SLEEPY_STATES and since) else now
    units = data.get("units") or status.get("units") or {}

    fields: Dict[str, FieldValue] = {}

    def put(name_: str, value: Any) -> None:
        if value is not None:
            fields[name_] = FieldValue(value, stamp)

    mult_mi = 0.621371 if units.get("unit_of_length") == "km" else 1.0

    def temp(v: Any) -> Optional[float]:
        n = _num(v)
        if n is None:
            return None
        return n * 9 / 5 + 32 if units.get("unit_of_temperature") == "C" else n

    put("battery_pct", _num(batt.get("battery_level")))
    rng = _num(batt.get("rated_battery_range")) or _num(batt.get("ideal_battery_range"))
    put("range_mi", None if rng is None else round(rng * mult_mi, 1))
    plugged = chg.get("plugged_in")
    put("plugged_in", plugged if isinstance(plugged, bool) else None)
    cs = str(chg.get("charging_state") or "").lower()
    if cs:
        put("charging_state", {"charging": "charging", "complete": "complete", "stopped": "stopped",
                               "starting": "starting", "disconnected": "disconnected",
                               "nopower": "no_power"}.get(cs, "unknown"))
    elif plugged is False:
        put("charging_state", "disconnected")
    put("charge_power_kw", _num(chg.get("charger_power")))
    put("charge_limit_pct", _num(chg.get("charge_limit_soc")))
    ttf = _num(chg.get("time_to_full_charge"))
    put("charge_eta_min", None if ttf is None else round(ttf * 60.0, 1))  # TeslaMate reports hours
    put("charge_current_a", _num(chg.get("charger_actual_current")))
    put("charge_current_max_a", _num(chg.get("charge_current_request_max")))
    put("charger_voltage_v", _num(chg.get("charger_voltage")))
    put("charge_port_open", chg.get("charge_port_door_open") if isinstance(chg.get("charge_port_door_open"), bool) else None)
    put("locked", car.get("locked") if isinstance(car.get("locked"), bool) else None)
    if "doors_open" in car:
        doors = [n for n, k in (("front_driver", "driver_front_door_open"), ("rear_driver", "driver_rear_door_open"),
                                ("front_passenger", "passenger_front_door_open"),
                                ("rear_passenger", "passenger_rear_door_open")) if car.get(k)]
        put("doors_open", doors if (doors or not car.get("doors_open")) else ["unknown"])
    if "windows_open" in car:
        put("windows_open", ["unknown"] if car.get("windows_open") else [])
    for fname, key in (("trunk_open", "trunk_open"), ("frunk_open", "frunk_open"), ("sentry_on", "sentry_mode"),
                       ("user_present", "is_user_present")):
        if isinstance(car.get(key), bool):
            put(fname, car[key])
    if isinstance(clim.get("is_climate_on"), bool):
        put("climate_on", clim["is_climate_on"])
    put("cabin_temp_f", temp(clim.get("inside_temp")))
    put("outside_temp_f", temp(clim.get("outside_temp")))
    zone = geo.get("geofence")
    if zone and zone != "REDACTED":
        z = str(zone).strip().lower().replace(" ", "_")
        put("location_zone", z)
    shift = str(drv.get("shift_state") or "").upper()
    if shift in ("P", "D", "R", "N"):
        put("shift_state", shift)
    elif not shift and tm_state in PARKED_TM_STATES:
        put("shift_state", "P")  # TeslaMate reports an empty gear when parked; offline/start/unknown prove nothing (R15)
    speed = _num(drv.get("speed"))
    if speed is not None:
        put("speed_mph", speed * mult_mi)
        put("moving", speed * mult_mi > 1.0)
    put("asleep", tm_state in ("asleep", "offline"))
    if isinstance(ver.get("update_available"), bool):
        put("software_update_available", ver["update_available"])
    put("software_version", ver.get("version") or None)
    odo = _num(status.get("odometer"))
    if odo:
        put("odometer_mi", round(odo * mult_mi, 1))
    for f in FIELD_NAMES:
        fields.setdefault(f, FieldValue(None, None, False))
    return VehicleState(name=name, source=source, captured_at=now, fields=fields)


class TeslaMateLive(CarAdapter):
    """Read-only live state from TeslaMateApi ``/status`` (no commands)."""

    kind = "teslamate"
    label = "TeslaMate (read-only)"

    def __init__(self, client: TeslaMateClient, name: str = ""):
        self._client = client
        self._name = name

    def capabilities(self) -> Capabilities:
        return Capabilities(frozenset(FIELD_NAMES), frozenset(), history=True,
                            notes="TeslaMate is read-only: no commands. Pair it with Home Assistant for control.")

    def fetch_state(self, now: float) -> VehicleState:
        data = self._client.status()
        if not data or not data.get("status"):
            raise AdapterError("TeslaMate API returned no status; check the car id")
        name = self._name or str((data.get("car") or {}).get("car_name") or "Car")
        if name == "REDACTED":
            name = self._name or "Car"
        return status_to_state(data, name, now)


#: Fields the overlay may take from TeslaMate when its reading is newer than Home Assistant's.
OVERLAY_FIELDS = ("locked", "doors_open", "windows_open", "trunk_open", "frunk_open", "sentry_on", "shift_state",
                  "speed_mph", "moving", "location_zone", "charging_state", "charge_power_kw", "battery_pct", "range_mi")
MOTION_FIELDS = ("shift_state", "speed_mph", "moving")
#: TeslaMateApi's /status has NO per-field or per-poll time (only ``state_since``, when the car's state last changed;
#: verified in teslamateapi src/v1_TeslaMateAPICarsStatus.go @ 3e158df). While the car is awake TeslaMate keeps polling
#: it, but the API does not say when it last did, so an unchanged reading is treated as this many seconds old.
TM_AWAKE_AGE_S = 60.0
SOURCE_NOTE = ("TeslaMateApi gives no per-field times: its readings are aged from the car's last state change "
               "(state_since) while the car sleeps, as {age} s old while it is awake, or from Pit Crew's previous look at "
               "TeslaMate when the value changed since. Each merged field shows the source whose reading is newer.")


def _says_moving(name: str, value: Any) -> bool:
    if name == "moving":
        return value is True
    if name == "speed_mph":
        n = _num(value)
        return n is not None and n > 1.0
    return isinstance(value, str) and value.upper() in ("D", "R", "N")


class TeslaMateOverlay:
    """Read-only fresher-state source next to Home Assistant (never commands; :class:`TeslaMateClient` only GETs).

    Each read stamps every overlay field honestly (see :data:`SOURCE_NOTE`). Pit Crew remembers what it last saw per
    field: a value that differs from the previous look was published by TeslaMate *after* that look, so its time is at
    least the previous look's time. :meth:`baseline` takes such a look right after a command was sent, which is what
    lets a read-back confirm from TeslaMate (only a reading stamped after the send counts)."""

    def __init__(self, client: TeslaMateClient) -> None:
        self._client = client
        self._seen: Dict[str, Tuple[Any, float, float]] = {}    # field -> (value, time of that look, changed no earlier than)
        self._lock = threading.Lock()
        self.last_error: Optional[str] = None

    def baseline(self, at: float) -> None:
        self.read(at)

    def read(self, now: float) -> Optional[VehicleState]:
        try:
            data = self._client.status()
        except AdapterError as exc:
            self.last_error = str(exc)
            return None
        if not data or not data.get("status"):
            self.last_error = "TeslaMate API returned no status; check TeslaMate · Car id"
            return None
        self.last_error = None
        st = status_to_state(data, "", now, source="teslamate")
        status = data.get("status") or {}
        tm_state = str(status.get("state") or "").lower()
        since = _ts(status.get("state_since"))
        sleepy = tm_state in SLEEPY_STATES and since is not None
        with self._lock:
            for name in OVERLAY_FIELDS:
                fv = st.fields.get(name)
                if fv is None or not fv.available:
                    continue
                base = since if sleepy else now - TM_AWAKE_AGE_S
                prev = self._seen.get(name)
                changed_lb = prev[2] if prev is not None and prev[0] == fv.value else (prev[1] if prev else 0.0)
                fv.updated_at = min(now, max(base, changed_lb))
                # only a stamp that came from the awake-car assumption is an estimate; state_since and an observed change
                # (the value differed from Pit Crew's earlier look) are real times
                fv.estimated = not sleepy and base >= changed_lb
                fv.source = "teslamate"
                self._seen[name] = (fv.value, now, changed_lb)
        return st

    @staticmethod
    def merge(fields: Dict[str, FieldValue], tm: VehicleState, car_reported_at: Optional[float]) -> None:
        """Per field, keep whichever source reported more recently. A Home Assistant value that is only the integration's
        optimistic write counts as of HA's last real re-read. Motion fails closed: if either source says moving (D/R/N,
        speed > 1), that reading is kept whatever its age, as everywhere else in Pit Crew."""
        for name in OVERLAY_FIELDS:
            t = tm.fields.get(name)
            h = fields.get(name)
            if t is None or not t.available:
                if h is not None and h.available:
                    h.source = "home_assistant"
                continue
            if h is None or not h.available:
                fields[name] = t
                continue
            h_time = (car_reported_at or 0.0) if h.assumed else (h.updated_at or 0.0)
            if name in MOTION_FIELDS and _says_moving(name, h.value) != _says_moving(name, t.value):
                take_tm = _says_moving(name, t.value)
            else:
                take_tm = (t.updated_at or 0.0) > h_time
            if take_tm:
                fields[name] = t
            else:
                h.source = "home_assistant"


class TeslaMateHistory(HistoryProvider):
    kind = "teslamate"
    label = "TeslaMate history"

    def __init__(self, client: TeslaMateClient):
        self._client = client

    def _paged(self, fetch: Any, start: float, end: float, limit: int) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            rows = fetch(start, end, page=page, show=PAGE_SIZE)
            out.extend(rows)
            if len(rows) < PAGE_SIZE or len(out) >= limit:
                break
        return out[:limit]

    def drives(self, start: float, end: float, limit: int = 500) -> List[DriveRecord]:
        recs = [normalize_drive(r) for r in self._paged(self._client.drives, start, end, limit)]
        return sorted([r for r in recs if r and start <= r.start <= end], key=lambda r: r.start)

    def charges(self, start: float, end: float, limit: int = 500) -> List[ChargeRecord]:
        recs = [normalize_charge(r) for r in self._paged(self._client.charges, start, end, limit)]
        return sorted([r for r in recs if r and start <= r.start <= end], key=lambda r: r.start)

    def battery_health(self) -> Optional[BatteryHealth]:
        bh = self._client.battery_health()
        if not bh:
            return None
        return BatteryHealth(
            max_capacity_kwh=_num(bh.get("max_capacity")), current_capacity_kwh=_num(bh.get("current_capacity")),
            health_pct=_num(bh.get("battery_health_percentage")), max_range_mi=_num(bh.get("max_range")),
            current_range_mi=_num(bh.get("current_range")))


def _client(ctx: BuildContext) -> TeslaMateClient:
    s = ctx.settings
    try:
        car_id = int(s.get("teslamate_car_id") or 1)
    except (TypeError, ValueError):
        raise AdapterError("TeslaMate · Car id must be a whole number (usually 1)") from None
    return TeslaMateClient(str(s.get("teslamate_url") or ""), car_id, ctx.secret(ENV_TESLAMATE_TOKEN))


def build_teslamate_overlay(ctx: BuildContext) -> Optional[TeslaMateOverlay]:
    """The optional TeslaMate overlay next to Home Assistant: only when ``teslamate_url`` is set. A TeslaMate problem never
    stops Home Assistant from working (logged; Pit Crew then reads Home Assistant only)."""
    if not str(ctx.settings.get("teslamate_url") or "").strip():
        return None
    try:
        return TeslaMateOverlay(_client(ctx))
    except AdapterError as exc:
        logger.info("TeslaMate overlay disabled: %s", exc)
        return None


def build_teslamate_live(ctx: BuildContext) -> TeslaMateLive:
    return TeslaMateLive(_client(ctx), name=str(ctx.settings.get("car_name") or ""))


def build_teslamate_history(ctx: BuildContext) -> TeslaMateHistory:
    return TeslaMateHistory(_client(ctx))
