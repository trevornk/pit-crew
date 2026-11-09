"""Readiness engine: "will the car be ready for X?"

Pure functions over a :class:`VehicleState`, a departure time and a trip distance. No I/O.

Model
-----
needed % = trip_energy% * (1 + safety_factor) + reserve%
  trip_energy% = miles * Wh/mi / 1000 / usable_kWh * 100
  Wh/mi = recent history near the expected temperature when available, else a default scaled by a
          weather factor.
projected % at departure = current % (+ what charging will add before departure, capped at the limit)
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..adapters.base import DriveRecord
from .history import efficiency_near_temperature, weighted_wh_per_mi
from .models import ACTIVELY_CHARGING, VehicleState
from .timeparse import fmt_clock, fmt_day_clock

DEFAULT_WH_PER_MI = 270.0
SAFETY_FACTOR = 0.15
CHARGE_EFFICIENCY = 0.92
TIGHT_MARGIN_PCT = 4.0
DEFAULT_AC_VOLTS = 240.0


def weather_factor(temp_f: Optional[float]) -> float:
    """Multiplier on baseline consumption for cold (battery + cabin heat) or very hot (A/C) weather."""
    if temp_f is None:
        return 1.0
    if temp_f <= 15:
        return 1.35
    if temp_f < 32:
        return 1.25
    if temp_f < 45:
        return 1.12
    if temp_f < 60:
        return 1.04
    if temp_f <= 80:
        return 1.0
    if temp_f <= 90:
        return 1.05
    return 1.10


def estimate_efficiency(drives: Sequence[DriveRecord], temp_f: Optional[float]) -> Dict[str, Any]:
    """Wh/mi estimate and where it came from."""
    if temp_f is not None:
        eff, n = efficiency_near_temperature(drives, temp_f)
        if eff is not None:
            return {"wh_per_mi": round(eff, 0), "source": f"your last {n} trips in similar temperatures (~{temp_f:.0f}°F)"}
    overall = weighted_wh_per_mi(drives)
    if overall is not None:
        factor = weather_factor(temp_f)
        # History already averages mixed weather; apply only the extreme-weather part.
        extra = 1.0 if factor < 1.12 else factor / 1.04
        return {"wh_per_mi": round(overall * extra, 0),
                "source": "your trip history average" + (" adjusted for extreme weather" if extra != 1.0 else "")}
    base = DEFAULT_WH_PER_MI * weather_factor(temp_f)
    return {"wh_per_mi": round(base, 0), "source": "a default estimate (no trip history available)"}


def charge_rate_kw(state: VehicleState, last_charge_volts: Optional[float] = None) -> Dict[str, Any]:
    """Rate that *would* reach the battery if charging were running, with how it was derived.

    R10: an idle charger reports ~0-2 V, so a plugged-in-but-idle car must not be rated at 240 V. Use the voltage
    last seen while charging; if there is none, the rate is unknown (kw 0) rather than a flattering guess."""
    power = state.value("charge_power_kw")
    if state.value("charging_state") in ACTIVELY_CHARGING and isinstance(power, (int, float)) and power > 0.3:
        return {"kw": power * CHARGE_EFFICIENCY, "basis": f"current charging power ({power:.1f} kW)"}
    amps = state.value("charge_current_max_a")
    volts = state.value("charger_voltage_v")
    if isinstance(amps, (int, float)) and amps > 0:
        if isinstance(volts, (int, float)) and volts > 50:
            return {"kw": volts * amps / 1000.0 * CHARGE_EFFICIENCY, "basis": f"{amps:.0f} A at ~{volts:.0f} V"}
        if isinstance(last_charge_volts, (int, float)) and last_charge_volts > 50:
            return {"kw": last_charge_volts * amps / 1000.0 * CHARGE_EFFICIENCY,
                    "basis": f"{amps:.0f} A at ~{last_charge_volts:.0f} V (voltage last seen while charging)"}
        if volts is None:   # the car never reports voltage: say so, assume a standard 240 V circuit
            return {"kw": DEFAULT_AC_VOLTS * amps / 1000.0 * CHARGE_EFFICIENCY,
                    "basis": f"{amps:.0f} A, assuming a 240 V circuit (the car doesn't report voltage)"}
        return {"kw": 0.0, "basis": "unknown charging speed (the charger is idle, so its voltage isn't known yet)"}
    return {"kw": 0.0, "basis": "unknown charging speed"}


def evaluate(state: VehicleState, departure: datetime, now: datetime, *, trip_mi: float, round_trip: bool = False,
             expected_temp_f: Optional[float] = None, drives: Sequence[DriveRecord] = (), battery_kwh: float = 75.0,
             battery_kwh_source: str = "setting", reserve_pct: float = 10.0, safety_factor: float = SAFETY_FACTOR,
             can_do: Optional[Iterable[str]] = None, home_zones: Sequence[str] = ("home",),
             last_charge_volts: Optional[float] = None, scheduled_start: Optional[datetime] = None) -> Dict[str, Any]:
    """Compute the readiness verdict for a departure. ``can_do`` limits suggested commands."""
    can = set(can_do) if can_do is not None else None
    if not battery_kwh or battery_kwh <= 0:   # R18: never divide by a bad pack size
        return {"ok": False, "error": "the usable battery size must be greater than 0 kWh"}

    def allowed(cmd: str) -> bool:
        return can is None or cmd in can

    if not state.has("battery_pct"):
        return {"ok": False, "error": "the car did not report its battery level, so readiness cannot be computed"}
    soc = float(state.value("battery_pct"))
    hours = (departure.timestamp() - now.timestamp()) / 3600.0   # real elapsed time (aware-datetime '-' ignores DST)
    if hours <= 0:
        return {"ok": False, "error": "the departure time is not in the future"}

    temp = expected_temp_f if expected_temp_f is not None else state.value("outside_temp_f")
    temp_src = "your forecast input" if expected_temp_f is not None else ("the car's current outside temperature" if temp is not None else "unknown")
    eff = estimate_efficiency(drives, temp)
    distance = trip_mi * (2 if round_trip else 1)
    trip_pct = distance * eff["wh_per_mi"] / 1000.0 / battery_kwh * 100.0
    required = min(100.0, trip_pct * (1.0 + safety_factor) + reserve_pct)

    limit = state.value("charge_limit_pct")
    limit_f = float(limit) if isinstance(limit, (int, float)) else 100.0
    plugged = state.value("plugged_in") is True or state.value("charging_state") in ("charging", "starting", "complete", "stopped")
    charging_now = state.value("charging_state") in ACTIVELY_CHARGING
    zone = state.at_zone()
    at_home = zone is not None and zone in [z.lower() for z in home_zones]
    rate = charge_rate_kw(state, last_charge_volts)

    def project(rate_kw: float, hrs: float = hours) -> float:
        added = rate_kw * hrs / battery_kwh * 100.0
        return min(max(soc, limit_f), soc + added) if soc < limit_f else soc

    # What happens if nothing changes vs. if charging runs the whole time.
    # R11: a schedule (off-peak start) means a plugged-in idle car WILL charge; don't call it not-ready or start it early.
    scheduled = bool(plugged and not charging_now and scheduled_start is not None and scheduled_start < departure
                     and soc < limit_f)
    sched_hours = max(0.0, (departure.timestamp() - max(scheduled_start, now).timestamp()) / 3600.0) if scheduled else 0.0
    if charging_now and plugged:
        projected_now = project(rate["kw"])
    elif scheduled:
        projected_now = project(rate["kw"], sched_hours)
    else:
        projected_now = soc
    best_case = project(rate["kw"]) if plugged else soc
    max_reachable = min(best_case, limit_f if soc < limit_f else soc)

    margin = projected_now - required
    needed_kwh = max(0.0, (required - soc) / 100.0 * battery_kwh)
    ready_by: Optional[datetime] = None
    if plugged and rate["kw"] > 0.3 and limit_f >= required and needed_kwh > 0:
        ready_by = now + timedelta(hours=needed_kwh / rate["kw"])

    recs: List[Dict[str, Any]] = []
    issues: List[str] = []

    if projected_now >= required + TIGHT_MARGIN_PCT:
        verdict = "ready"
    elif projected_now >= required:
        verdict = "tight"
    else:
        verdict = "not_ready"

    if verdict != "ready":
        if limit_f < required and (plugged or soc < required):
            target = int(min(100, math.ceil(required + 2)))
            issues.append(f"the charge limit ({limit_f:.0f}%) is below the {required:.0f}% this trip needs")
            if allowed("charge_limit"):
                recs.append({"action": "raise_charge_limit", "command": "charge_limit", "args": {"percent": target},
                             "why": f"needs about {required:.0f}%; limit is {limit_f:.0f}%"})
        if not plugged and soc < required:
            if at_home:
                issues.append("the car is not plugged in")
                recs.append({"action": "plug_in", "command": None, "args": {},
                             "why": f"at {soc:.0f}% it is {required - soc:.0f} points short and charging can't start by itself"})
            else:
                issues.append(f"the car is at {soc:.0f}% and away from home; it needs a charge stop before the trip")
        if plugged and not charging_now and not scheduled and soc < min(limit_f, required + TIGHT_MARGIN_PCT):
            issues.append("it is plugged in but not charging")
            if allowed("charge_start"):
                recs.append({"action": "start_charging", "command": "charge_start", "args": {},
                             "why": "plugged in with room below the limit, but not charging"})
        if plugged and limit_f >= required and max_reachable < required:
            hrs = needed_kwh / rate["kw"] if rate["kw"] > 0.3 else None
            if hrs:
                issues.append(f"charging speed ({rate['basis']}) is too slow: it needs about {hrs:.1f} h but only "
                              f"{hours:.1f} h remain")
            recs.append({"action": "charge_elsewhere_or_leave_later", "command": None, "args": {},
                         "why": "use a faster charger, leave later, or shorten the trip"
                                + (f"; at the current speed it would be ready around {fmt_clock(ready_by)}" if ready_by else "")})
    else:
        if charging_now and limit_f > required + 10 and limit_f > 85:
            recs.append({"action": "optional_lower_limit", "command": "charge_limit" if allowed("charge_limit") else None,
                         "args": {"percent": int(min(90, math.ceil(required + 8)))},
                         "why": f"the trip only needs about {required:.0f}%; a lower daily limit is easier on the battery"})

    # Preconditioning suggestion (comfort): only if the cabin target is useful.
    lead = 10 + (10 if temp is not None and (temp < 40 or temp > 85) else 0) + (10 if temp is not None and temp < 20 else 0)
    if allowed("climate_on") and plugged and temp is not None and (temp < 50 or temp > 80):
        recs.append({"action": "precondition", "command": "climate_on", "args": {"temp_f": 70},
                     "at": (departure - timedelta(minutes=lead)).isoformat(timespec="minutes"),
                     "why": f"{'warm' if temp < 50 else 'cool'} the cabin on wall power about {lead} min before leaving"})

    return {
        "ok": True,
        "verdict": verdict,
        "ready": verdict in ("ready", "tight"),
        "departure": departure.isoformat(timespec="minutes"),
        "hours_until_departure": round(hours, 1),
        "battery_now_pct": round(soc, 0),
        "projected_at_departure_pct": round(projected_now, 0),
        "needed_pct": round(required, 0),
        "margin_pct": round(margin, 0),
        "trip": {"miles": round(distance, 1), "round_trip": round_trip, "energy_pct": round(trip_pct, 0),
                 "wh_per_mi": eff["wh_per_mi"]},
        "charging": {"plugged_in": bool(plugged), "charging_now": bool(charging_now), "limit_pct": limit_f if limit is not None else None,
                     "rate_basis": rate["basis"],
                     "scheduled_start": scheduled_start.isoformat(timespec="minutes") if scheduled else None, "ready_by": ready_by.isoformat(timespec="minutes") if ready_by else None},
        "assumptions": {"efficiency": eff["source"], "temperature_f": None if temp is None else round(temp, 0),
                        "temperature_from": temp_src, "usable_battery_kwh": round(battery_kwh, 1),
                        "battery_size_from": battery_kwh_source, "safety_margin": f"{int(safety_factor * 100)}% on trip energy",
                        "reserve_pct": reserve_pct},
        "issues": issues,
        "recommendations": recs,
        "summary": _summary(verdict, soc, projected_now, required, departure, issues, recs),
    }


def _summary(verdict: str, soc: float, projected: float, required: float, departure: datetime,
             issues: List[str], recs: List[Dict[str, Any]]) -> str:
    when = fmt_day_clock(departure)
    if verdict == "ready":
        return f"Ready for {when}: expected {projected:.0f}% at departure vs about {required:.0f}% needed."
    if verdict == "tight":
        return f"Tight for {when}: expected {projected:.0f}% vs about {required:.0f}% needed — little room for error."
    why = "; ".join(issues) if issues else "not enough charge by then"
    first = f" Suggested: {recs[0]['action'].replace('_', ' ')}." if recs else ""
    return f"NOT ready for {when}: {soc:.0f}% now, {projected:.0f}% expected, about {required:.0f}% needed — {why}.{first}"
