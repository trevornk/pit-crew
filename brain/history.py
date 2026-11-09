"""History aggregation. All numbers are computed in code from normalized records (US units)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..adapters.base import BatteryHealth, ChargeRecord, DriveRecord, HistoryProvider

TEMP_BUCKETS: Tuple[Tuple[str, float, float], ...] = (
    ("under 32°F", -100.0, 32.0), ("32–50°F", 32.0, 50.0), ("50–70°F", 50.0, 70.0),
    ("70–85°F", 70.0, 85.0), ("over 85°F", 85.0, 200.0))
MIN_EFFICIENCY_MILES = 1.0


def _r(x: Optional[float], n: int = 1) -> Optional[float]:
    return None if x is None else round(x, n)


def _eff_rows(drives: Iterable[DriveRecord]) -> List[DriveRecord]:
    # wh_per_mi None = distance was inferred from the rated-range drop (circular), so it says nothing about efficiency (R9)
    return [d for d in drives if d.kwh is not None and d.wh_per_mi is not None and d.miles >= MIN_EFFICIENCY_MILES]


def weighted_wh_per_mi(drives: Iterable[DriveRecord]) -> Optional[float]:
    rows = _eff_rows(drives)
    miles = sum(d.miles for d in rows)
    kwh = sum(d.kwh for d in rows if d.kwh is not None)
    return kwh * 1000.0 / miles if miles >= 5.0 else None


def efficiency_by_temperature(drives: Sequence[DriveRecord]) -> List[Dict[str, Any]]:
    out = []
    for label, lo, hi in TEMP_BUCKETS:
        rows = [d for d in _eff_rows(drives) if d.outside_temp_f is not None and lo <= d.outside_temp_f < hi]
        eff = weighted_wh_per_mi(rows)
        if eff is not None:
            out.append({"temp_range": label, "trips": len(rows), "miles": _r(sum(d.miles for d in rows)),
                        "wh_per_mi": _r(eff)})
    return out


def efficiency_near_temperature(drives: Sequence[DriveRecord], temp_f: float, window_f: float = 12.0,
                                min_trips: int = 3) -> Tuple[Optional[float], int]:
    rows = [d for d in _eff_rows(drives) if d.outside_temp_f is not None and abs(d.outside_temp_f - temp_f) <= window_f]
    if len(rows) < min_trips:
        return None, len(rows)
    return weighted_wh_per_mi(rows), len(rows)


def summarize_drives(drives: Sequence[DriveRecord]) -> Dict[str, Any]:
    real = [d for d in drives if d.miles >= 0.1]
    if not real:
        return {"trips": 0}
    miles = sum(d.miles for d in real)
    hours = sum(d.duration_min for d in real) / 60.0
    kwh = sum(d.kwh for d in real if d.kwh is not None)
    longest = max(real, key=lambda d: d.miles)
    return {
        "trips": len(real), "miles": _r(miles), "avg_trip_mi": _r(miles / len(real)),
        "driving_hours": _r(hours), "avg_speed_mph": _r(miles / hours) if hours else None,
        "kwh_used": _r(kwh, 1) if kwh else None, "wh_per_mi": _r(weighted_wh_per_mi(real)),
        "longest_trip_mi": _r(longest.miles), "efficiency_by_temperature": efficiency_by_temperature(real),
    }


def summarize_charges(charges: Sequence[ChargeRecord]) -> Dict[str, Any]:
    if not charges:
        return {"sessions": 0}
    added = sum(c.kwh_added for c in charges)
    used = sum(c.kwh_used for c in charges if c.kwh_used is not None)
    cost = sum(c.cost for c in charges if c.cost is not None)
    with_cost = [c for c in charges if c.cost is not None and c.kwh_used]
    with_used = [c for c in charges if c.kwh_used]          # efficiency compares like with like (mixed sources otherwise exceed 300%)
    out: Dict[str, Any] = {
        "sessions": len(charges), "kwh_added": _r(added, 1), "kwh_from_wall": _r(used, 1) if used else None,
        "avg_session_kwh": _r(added / len(charges), 1),
        "charging_efficiency_pct": _r(sum(c.kwh_added for c in with_used) / used * 100.0, 0) if used else None,
        "total_cost_usd": _r(cost, 2) if with_cost else None,
        "avg_cost_per_kwh_usd": _r(sum(c.cost for c in with_cost) / sum(c.kwh_used for c in with_cost), 3) if with_cost else None,
        "avg_start_pct": _r(_avg([c.start_pct for c in charges]), 0),
        "avg_end_pct": _r(_avg([c.end_pct for c in charges]), 0),
    }
    return out


def _avg(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def bounds(now: float, days: Optional[int], start: Optional[float], end: Optional[float]) -> Tuple[float, float]:
    if start is not None:
        return start, (end if end is not None else now)
    d = max(1, min(int(days or 30), 3650))
    return now - d * 86400.0, now


def efficiency_trend(drives: Sequence[DriveRecord], now: float) -> Optional[Dict[str, Any]]:
    recent = [d for d in drives if d.start >= now - 7 * 86400]
    prior = [d for d in drives if now - 28 * 86400 <= d.start < now - 7 * 86400]
    a, b = weighted_wh_per_mi(recent), weighted_wh_per_mi(prior)
    if a is None or b is None:
        return None
    return {"last_7_days_wh_per_mi": _r(a), "previous_21_days_wh_per_mi": _r(b),
            "change_pct": _r((a - b) / b * 100.0)}


def history_report(provider: Optional[HistoryProvider], kind: str, now: float, *, days: Optional[int] = 30,
                   start: Optional[float] = None, end: Optional[float] = None, limit: int = 500) -> Dict[str, Any]:
    """Build the ``car_history`` payload. ``kind``: summary | trips | charges | efficiency | battery."""
    if provider is None:
        return {"ok": False, "error": "no history source is configured",
                "hint": "Set the 'history' setting to 'teslamate' (and teslamate_url) — or use the simulator."}
    t0, t1 = bounds(now, days, start, end)
    tz = datetime.fromtimestamp(now).astimezone().tzinfo
    window = {"from": datetime.fromtimestamp(t0, tz=tz).date().isoformat(),
              "to": datetime.fromtimestamp(t1, tz=tz).date().isoformat(), "days": round((t1 - t0) / 86400.0, 1)}
    out: Dict[str, Any] = {"ok": True, "kind": kind, "window": window, "source": provider.label}
    cov = getattr(provider, "coverage_start", None)
    first = cov() if callable(cov) else None
    if first:
        out["recording_since"] = datetime.fromtimestamp(first, tz=tz).date().isoformat()
        if first > t0 + 86400:
            out["note"] = ("Pit Crew has only been recording since " + out["recording_since"] + "; earlier trips are not "
                           "available unless you import them (`/car import teslamate`).")
    if kind in ("summary", "trips", "efficiency"):
        drives = provider.drives(t0, t1, limit)
        if kind in ("summary", "efficiency"):
            out["driving"] = summarize_drives(drives)
            trend = efficiency_trend(drives, now)
            if trend:
                out["efficiency_trend"] = trend
        if kind == "trips":
            real = [d for d in drives if d.miles >= 0.1]
            out["trips"] = [{"start": datetime.fromtimestamp(d.start, tz=tz).isoformat(timespec="minutes"),
                             "miles": _r(d.miles), "minutes": _r(d.duration_min, 0), "wh_per_mi": _r(d.wh_per_mi, 0),
                             "battery_used_pct": _r((d.start_pct - d.end_pct) if d.start_pct is not None and d.end_pct is not None else None, 0),
                             "outside_temp_f": _r(d.outside_temp_f, 0)} for d in real[-25:]]
            out["trips_total"] = len(real)
            if len(real) > 25:
                out["note"] = (out.get("note", "") + " Showing the 25 most recent trips; totals are in `driving`.").strip()
            out["driving"] = summarize_drives(drives)
    if kind in ("summary", "charges"):
        charges = provider.charges(t0, t1, limit)
        out["charging"] = summarize_charges(charges)
        if kind == "charges":
            out["sessions"] = [{"start": datetime.fromtimestamp(c.start, tz=tz).isoformat(timespec="minutes"),
                                "minutes": _r(c.duration_min, 0), "kwh_added": _r(c.kwh_added, 1),
                                "from_pct": _r(c.start_pct, 0), "to_pct": _r(c.end_pct, 0), "cost_usd": _r(c.cost, 2)}
                               for c in charges[-25:]]
    if kind in ("summary", "battery"):
        out["battery_health"] = battery_health_dict(provider.battery_health())
    return out


def battery_health_dict(bh: Optional[BatteryHealth]) -> Optional[Dict[str, Any]]:
    if bh is None:
        return None
    return {k: _r(v, 1) for k, v in {
        "health_pct": bh.health_pct, "usable_capacity_kwh": bh.current_capacity_kwh,
        "original_capacity_kwh": bh.max_capacity_kwh, "current_full_range_mi": bh.current_range_mi,
        "original_full_range_mi": bh.max_range_mi}.items() if v is not None}
