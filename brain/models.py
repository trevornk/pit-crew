"""Normalized, platform-independent vehicle state.

Every adapter maps its car's data into :class:`VehicleState`. Each field carries its own
freshness timestamp and an ``available`` flag so the brain never guesses: a field the platform
does not provide is ``available=False`` and is reported as such, never silently defaulted.

Location is a *zone* (``home`` / ``away`` / a named zone). Raw coordinates are never part of the
model, so they cannot leak into tool output or logs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

from ..redact import plain

CHARGING_STATES = ("charging", "starting", "stopped", "complete", "disconnected", "no_power", "unknown")
ACTIVELY_CHARGING = ("charging", "starting")


@dataclass(frozen=True)
class FieldSpec:
    """Static description of one normalized state field."""

    name: str
    label: str
    group: str
    unit: str = ""


_SPECS: List[FieldSpec] = [
    FieldSpec("battery_pct", "Battery", "battery", "%"),
    FieldSpec("range_mi", "Range", "battery", "mi"),
    FieldSpec("charging_state", "Charging state", "charging"),
    FieldSpec("plugged_in", "Plugged in", "charging"),
    FieldSpec("charge_power_kw", "Charge power", "charging", "kW"),
    FieldSpec("charge_limit_pct", "Charge limit", "charging", "%"),
    FieldSpec("charge_eta_min", "Minutes to charge limit", "charging", "min"),
    FieldSpec("charge_current_a", "Charge current", "charging", "A"),
    FieldSpec("charge_current_max_a", "Max charge current", "charging", "A"),
    FieldSpec("charger_voltage_v", "Charger voltage", "charging", "V"),
    FieldSpec("charge_port_open", "Charge port open", "charging"),
    FieldSpec("locked", "Locked", "security"),
    FieldSpec("doors_open", "Open doors", "security"),
    FieldSpec("windows_open", "Open windows", "security"),
    FieldSpec("trunk_open", "Trunk open", "security"),
    FieldSpec("frunk_open", "Frunk open", "security"),
    FieldSpec("sentry_on", "Sentry mode", "security"),
    FieldSpec("user_present", "Someone in the car", "security"),
    FieldSpec("climate_on", "Climate on", "climate"),
    FieldSpec("cabin_temp_f", "Cabin temperature", "climate", "°F"),
    FieldSpec("outside_temp_f", "Outside temperature", "climate", "°F"),
    FieldSpec("target_temp_f", "Target temperature", "climate", "°F"),
    FieldSpec("defrost_on", "Defrost on", "climate"),
    FieldSpec("location_zone", "Location zone", "location"),
    FieldSpec("shift_state", "Gear", "motion"),
    FieldSpec("speed_mph", "Speed", "motion", "mph"),
    FieldSpec("moving", "Moving", "motion"),
    FieldSpec("asleep", "Asleep", "status"),
    FieldSpec("software_update_available", "Software update available", "software"),
    FieldSpec("software_version", "Software version", "software"),
    FieldSpec("odometer_mi", "Odometer", "software", "mi"),
    FieldSpec("tire_psi_fl", "Tire pressure front left", "tires", "psi"),
    FieldSpec("tire_psi_fr", "Tire pressure front right", "tires", "psi"),
    FieldSpec("tire_psi_rl", "Tire pressure rear left", "tires", "psi"),
    FieldSpec("tire_psi_rr", "Tire pressure rear right", "tires", "psi"),
    FieldSpec("tire_rec_front_psi", "Recommended front tire pressure", "tires", "psi"),
    FieldSpec("tire_rec_rear_psi", "Recommended rear tire pressure", "tires", "psi"),
    FieldSpec("tire_warn_fl", "Car's tire warning front left", "tires"),
    FieldSpec("tire_warn_fr", "Car's tire warning front right", "tires"),
    FieldSpec("tire_warn_rl", "Car's tire warning rear left", "tires"),
    FieldSpec("tire_warn_rr", "Car's tire warning rear right", "tires"),
]

#: public (tool / page) names of the tire fields, in display order
TIRE_PUBLIC = {"tire_psi_fl": "front_left_psi", "tire_psi_fr": "front_right_psi", "tire_psi_rl": "rear_left_psi",
               "tire_psi_rr": "rear_right_psi", "tire_rec_front_psi": "recommended_front_psi",
               "tire_rec_rear_psi": "recommended_rear_psi"}

#: the car's own per-tire TPMS warning (True = the car says this tire needs attention), same public naming
TIRE_WARN_PUBLIC = {"tire_warn_fl": "front_left_warning", "tire_warn_fr": "front_right_warning",
                    "tire_warn_rl": "rear_left_warning", "tire_warn_rr": "rear_right_warning"}

FIELD_SPECS: Dict[str, FieldSpec] = {s.name: s for s in _SPECS}
FIELD_NAMES = tuple(FIELD_SPECS)


@dataclass
class FieldValue:
    """One field reading. ``updated_at`` is epoch seconds (when the car/platform last reported it)."""

    value: Any = None
    updated_at: Optional[float] = None
    available: bool = True
    #: when the value last actually *changed*, if the adapter knows it and it is older than ``updated_at``
    changed_at: Optional[float] = None
    #: True when the value was last written by the integration itself right after a command (an *optimistic* write:
    #: Home Assistant's tesla_fleet/teslemetry/tessie set the entity to the commanded value as soon as the API call
    #: returns) and the car has not been re-read since. It is the integration's assumption, not the car's report.
    assumed: bool = False


def unavailable() -> FieldValue:
    return FieldValue(value=None, updated_at=None, available=False)


def iso(ts: Optional[float], tz: Any = None) -> Optional[str]:
    """Epoch seconds -> ISO-8601 string (UTC unless a tzinfo is given)."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=tz or timezone.utc).isoformat(timespec="seconds")


@dataclass
class VehicleState:
    """A point-in-time snapshot of the car with per-field freshness."""

    name: str
    source: str
    captured_at: float
    fields: Dict[str, FieldValue] = field(default_factory=dict)
    simulated: bool = False
    #: facts about the car that are not readings (e.g. ``tire_min_psi``: the low-pressure default for this model)
    hints: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for fname in FIELD_NAMES:
            self.fields.setdefault(fname, unavailable())

    # -- access ---------------------------------------------------------------------------
    def value(self, name: str, default: Any = None) -> Any:
        fv = self.fields.get(name)
        if fv is None or not fv.available or fv.value is None:
            return default
        return fv.value

    def has(self, name: str) -> bool:
        fv = self.fields.get(name)
        return bool(fv and fv.available and fv.value is not None)

    def is_available(self, name: str) -> bool:
        fv = self.fields.get(name)
        return bool(fv and fv.available)

    def age_s(self, name: str, now: float) -> Optional[float]:
        fv = self.fields.get(name)
        if fv is None or not fv.available or fv.updated_at is None:
            return None
        return max(0.0, now - fv.updated_at)

    def newest_update(self) -> Optional[float]:
        """The newest time the car reported anything (an integration's optimistic write after a command does not count)."""
        stamps = [f.updated_at for f in self.fields.values() if f.available and f.updated_at and not f.assumed]
        return max(stamps) if stamps else None

    def oldest_update(self, names: Optional[Iterable[str]] = None) -> Optional[float]:
        pool = names if names is not None else self.fields.keys()
        stamps = []
        for n in pool:
            f = self.fields.get(n)
            if f and f.available and f.updated_at:
                stamps.append(f.updated_at)
        return min(stamps) if stamps else None

    def set(self, name: str, value: Any, updated_at: Optional[float] = None) -> None:
        self.fields[name] = FieldValue(value=value, updated_at=updated_at if updated_at else self.captured_at)

    def copy(self) -> "VehicleState":
        return VehicleState(
            name=self.name, source=self.source, captured_at=self.captured_at,
            fields={k: FieldValue(v.value, v.updated_at, v.available, v.changed_at, v.assumed)
                    for k, v in self.fields.items()},
            simulated=self.simulated, hints=dict(self.hints),
        )

    # -- derived --------------------------------------------------------------------------
    @property
    def is_charging(self) -> bool:
        return self.value("charging_state") in ACTIVELY_CHARGING

    def motion(self, now: Optional[float] = None, max_age_s: Optional[float] = None) -> Optional[bool]:
        """True = moving, False = parked, None = cannot tell.

        Indicators: ``moving``, ``speed_mph``, ``shift_state`` and (as a parked indicator) ``plugged_in``.
        Any indicator saying "moving" wins. "Parked" needs at least one indicator that says so **and is fresh**:
        when ``now`` and ``max_age_s`` are given, an indicator older than ``max_age_s`` is ignored, so a stale
        reading can never prove the car is parked (it yields ``None``).
        """
        moving_votes: List[bool] = []
        parked_fresh = False

        def fresh(name: str) -> bool:
            if now is None or max_age_s is None:
                return True
            age = self.age_s(name, now)
            return age is not None and age <= max_age_s

        def vote(name: str, is_moving: bool) -> None:
            nonlocal parked_fresh
            if is_moving:
                moving_votes.append(True)  # conservative: even a stale "moving" blocks
            elif fresh(name):
                parked_fresh = True

        if self.has("moving"):
            vote("moving", bool(self.value("moving")))
        if self.has("speed_mph"):
            vote("speed_mph", float(self.value("speed_mph")) > 1.0)
        shift = self.value("shift_state")
        if isinstance(shift, str) and shift:
            vote("shift_state", shift.upper() in ("D", "R", "N"))
        if self.value("plugged_in") is True:
            vote("plugged_in", False)
        if moving_votes:
            return True
        return False if parked_fresh else None

    def at_zone(self) -> Optional[str]:
        zone = self.value("location_zone")
        return str(zone).lower() if zone else None

    def stale_fields(self, now: float, stale_after_s: float) -> List[str]:
        out = []
        for n, f in self.fields.items():
            if f.available and f.updated_at is not None and now - f.updated_at > stale_after_s:
                out.append(n)
        return out

    def freshness_age_s(self, now: float, names: Optional[Iterable[str]] = None) -> Optional[float]:
        """Age of the *oldest* of the given (default: all available) fields, in seconds."""
        oldest = self.oldest_update(names)
        return None if oldest is None else max(0.0, now - oldest)

    # -- serialization --------------------------------------------------------------------
    def to_public(self, now: Optional[float] = None, stale_after_s: float = 900.0, tz: Any = None) -> Dict[str, Any]:
        """JSON-ready dict for tool output / dashboard. Never contains coordinates. Times are in ``tz``."""
        now = time.time() if now is None else now
        v = self.value

        def grp(*names: str) -> Dict[str, Any]:
            return {n: v(n) for n in names if self.has(n)}

        battery = grp("battery_pct", "range_mi")
        charging = grp("charging_state", "plugged_in", "charge_power_kw", "charge_limit_pct",
                       "charge_eta_min", "charge_current_a", "charge_current_max_a", "charger_voltage_v",
                       "charge_port_open")
        eta = v("charge_eta_min")
        if isinstance(eta, (int, float)) and eta > 0 and self.is_charging:
            charging["done_at"] = iso(now + eta * 60, tz)
        security = grp("locked", "doors_open", "windows_open", "trunk_open", "frunk_open", "sentry_on",
                       "user_present")
        climate = grp("climate_on", "cabin_temp_f", "outside_temp_f", "target_temp_f", "defrost_on")
        location = grp("location_zone")
        motion = grp("shift_state", "speed_mph")
        mv = self.motion()
        if mv is not None:
            motion["moving"] = mv
        software = grp("software_update_available", "software_version", "odometer_mi")
        tires = {label: round(float(v(f)), 1) for f, label in TIRE_PUBLIC.items() if self.has(f)}
        tires.update({label: bool(v(f)) for f, label in TIRE_WARN_PUBLIC.items() if self.has(f)})
        status = grp("asleep")

        stale = {n: int(a) for n in FIELD_NAMES
                 if (a := self.age_s(n, now)) is not None and a > stale_after_s}
        unavailable_fields = [n for n, f in self.fields.items() if not f.available]
        assumed = [n for n, f in self.fields.items() if f.available and f.assumed]
        newest = self.newest_update()
        return {
            "vehicle": {"name": self.name, "source": self.source, "simulated": self.simulated},
            "as_of": iso(self.captured_at, tz),
            "newest_update": iso(newest, tz),
            "battery": battery,
            "charging": charging,
            "security": security,
            "climate": climate,
            "location": location,
            "motion": motion,
            "software": software,
            "tires": tires,
            "status": status,
            "freshness": {
                "newest_update_age_s": None if newest is None else int(max(0.0, now - newest)),
                "stale_fields": stale,
                "stale_after_s": int(stale_after_s),
                "unavailable_fields": unavailable_fields,
                # values the integration set itself after a command; the car has not re-reported them yet
                "assumed_after_command": assumed,
            },
        }

    def to_storage(self) -> Dict[str, Any]:
        return {
            "name": self.name, "source": self.source, "captured_at": self.captured_at,
            "simulated": self.simulated,
            "fields": {k: [f.value, f.updated_at, f.available] for k, f in self.fields.items()},
        }

    @classmethod
    def from_storage(cls, data: Mapping[str, Any]) -> "VehicleState":
        fields = {k: FieldValue(*v) for k, v in dict(data.get("fields", {})).items()}
        return cls(name=data["name"], source=data["source"], captured_at=data["captured_at"],
                   fields=fields, simulated=bool(data.get("simulated")))


def describe_state(state: VehicleState, now: Optional[float] = None) -> str:
    """One plain-language, US-units sentence/paragraph about the car (no model turn needed)."""
    now = time.time() if now is None else now
    v = state.value
    parts: List[str] = []
    label = plain(state.name, 40) or "The car"
    if state.has("battery_pct"):
        bit = f"{label} is at {v('battery_pct'):.0f}% battery"
        if state.has("range_mi"):
            bit += f" ({v('range_mi'):.0f} mi of range)"
        parts.append(bit)
    else:
        parts.append(f"{label}: battery level unknown")
    cs = v("charging_state")
    if cs == "charging":
        bit = "charging"
        if state.has("charge_power_kw"):
            bit += f" at {v('charge_power_kw'):.1f} kW"
        if state.has("charge_limit_pct"):
            bit += f" toward {v('charge_limit_pct'):.0f}%"
        eta = v("charge_eta_min")
        if isinstance(eta, (int, float)) and eta > 0:
            bit += f", about {int(eta // 60)}h {int(eta % 60):02d}m to go"
        parts.append(bit)
    elif cs == "complete":
        parts.append("charging is complete")
    elif cs == "stopped" and v("plugged_in"):
        parts.append("plugged in but not charging")
    elif cs == "disconnected" or v("plugged_in") is False:
        parts.append("not plugged in")
    if state.has("locked"):
        parts.append("locked" if v("locked") else "UNLOCKED")
    if state.has("location_zone"):
        parts.append(f"at {plain(v('location_zone'), 40)}")
    openings = []
    for key, label_ in (("doors_open", "door"), ("windows_open", "window")):
        val = v(key)
        if isinstance(val, list) and val:
            openings.append(f"{label_}(s) open: {', '.join(val)}")
        elif val is True:
            openings.append(f"{label_}(s) open")
    if v("trunk_open"):
        openings.append("trunk open")
    if v("frunk_open"):
        openings.append("frunk open")
    parts.extend(openings)
    if state.has("cabin_temp_f"):
        parts.append(f"cabin {v('cabin_temp_f'):.0f}°F")
    return "; ".join(parts) + "."
