"""Tire-pressure logic shared by the watcher rules and the status surfaces.

Pressures are psi everywhere. Two ways a tire becomes worth an alert:

* **Low**: below a threshold. The threshold is the rule's ``min_psi`` when set (> 0). Otherwise it is 90 % of the
  pressure the car itself recommends for that axle (BMW and the Tesla API report it). When the car does not say, the
  adapter may supply a model default in ``state.hints["tire_min_psi"]`` (Home Assistant's Tesla Fleet integration has no
  recommended-pressure entity, so its profile says 38, which is 90 % of a Model 3/Y's 42 psi placard); otherwise it is
  :data:`DEFAULT_MIN_PSI`, a conservative 30 psi that no passenger-car placard falls below.
* **Fast loss**: the pressure fell by at least ``drop_psi`` within ``window_min`` minutes (a puncture or a failing
  valve, long before the absolute value looks low).
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .models import VehicleState

DEFAULT_MIN_PSI = 30.0
#: Tires run hot after a drive and shed 3-5 psi over the next 20-40 minutes: no fast-loss samples for this long after the
#: car last moved (a real puncture is still caught by the low-pressure rule and by any loss after the cool-down).
COOLDOWN_AFTER_DRIVE_S = 60 * 60.0
RECOMMENDED_FRACTION = 0.90
#: pressure fields in display order -> (spoken position, axle)
TIRES: Dict[str, Tuple[str, str]] = {
    "tire_psi_fl": ("front left", "front"), "tire_psi_fr": ("front right", "front"),
    "tire_psi_rl": ("rear left", "rear"), "tire_psi_rr": ("rear right", "rear"),
}
#: Anything outside this is a sensor/units fault, not a tire: never alert on it.
PLAUSIBLE_PSI = (5.0, 120.0)


def plausible(psi: Any) -> bool:
    return isinstance(psi, (int, float)) and not isinstance(psi, bool) and PLAUSIBLE_PSI[0] <= float(psi) <= PLAUSIBLE_PSI[1]


def recommended_psi(state: VehicleState, axle: str) -> Optional[float]:
    v = state.value("tire_rec_front_psi" if axle == "front" else "tire_rec_rear_psi")
    return float(v) if plausible(v) else None


def low_threshold_psi(state: VehicleState, axle: str, min_psi: Any) -> float:
    """The psi below which a tire on ``axle`` counts as low (see the module docstring)."""
    try:
        explicit = float(min_psi or 0)
    except (TypeError, ValueError):
        explicit = 0.0
    if explicit > 0:
        return explicit
    rec = recommended_psi(state, axle)
    if rec:
        return round(rec * RECOMMENDED_FRACTION, 1)
    hint = (getattr(state, "hints", None) or {}).get("tire_min_psi")
    return float(hint) if plausible(hint) else DEFAULT_MIN_PSI


def readings(state: VehicleState, now: float, stale_after_s: float) -> Tuple[Dict[str, float], bool]:
    """``({field: psi}, any_stale)`` for the plausible, available tire readings. ``any_stale`` is True when a reading
    exists but is older than ``stale_after_s``: callers must then say nothing (a day-old pressure is not news)."""
    out: Dict[str, float] = {}
    stale = False
    for f in TIRES:
        if not state.has(f):
            continue
        age = state.age_s(f, now)
        if age is not None and age > stale_after_s:
            stale = True
            continue
        psi = state.value(f)
        if plausible(psi):
            out[f] = float(psi)
    return out, stale


def low_tires(state: VehicleState, now: float, stale_after_s: float, min_psi: Any) -> List[Tuple[str, float, float]]:
    """``[(position, psi, threshold)]`` for fresh readings below their threshold."""
    got, _ = readings(state, now, stale_after_s)
    out = []
    for f, psi in got.items():
        pos, axle = TIRES[f]
        limit = low_threshold_psi(state, axle, min_psi)
        if psi < limit:
            out.append((pos, psi, limit))
    return out


def stale_fields(state: VehicleState, now: float, stale_after_s: float) -> List[str]:
    """Tire fields that report a value older than ``stale_after_s`` (judged per tire, never for all four at once)."""
    out = []
    for f in TIRES:
        if state.has(f):
            age = state.age_s(f, now)
            if age is not None and age > stale_after_s:
                out.append(f)
    return out


def key_tires(key: str) -> List[str]:
    """The tire fields named in an alert key such as ``tire_low:front_left|rear_right``."""
    names = set(key.partition(":")[2].split("|"))
    return [f for f, (pos, _) in TIRES.items() if pos.replace(" ", "_") in names]


def track(memo: Dict[str, Any], state: VehicleState, now: float, window_s: float,
          last_moving_ts: Optional[float] = None, stale_after_s: Optional[float] = None) -> None:
    """Record one sample per tire per *new reading* into ``memo['tires']`` and drop samples older than the window.

    Nothing is recorded while the car is moving or within :data:`COOLDOWN_AFTER_DRIVE_S` of it last moving (hot tires
    cooling is not a leak); the samples taken before are discarded then, since they describe a different thermal state."""
    book: Dict[str, List[List[float]]] = memo.setdefault("tires", {})
    if state.motion() is True or (last_moving_ts is not None and 0 <= now - last_moving_ts < COOLDOWN_AFTER_DRIVE_S):
        book.clear()
        return
    stale = set(stale_fields(state, now, stale_after_s)) if stale_after_s is not None else set()
    for f in TIRES:
        fv = state.fields.get(f)
        if fv is None or not fv.available or not plausible(fv.value) or f in stale:
            continue
        ts = fv.updated_at if fv.updated_at is not None else now
        samples = [s for s in book.get(f, []) if now - s[0] <= window_s]
        if not samples or samples[-1][0] != ts:
            samples.append([float(ts), float(fv.value)])
        book[f] = samples[-60:]
    for f in list(book):
        if f not in TIRES:
            del book[f]


def fast_losses(memo: Mapping[str, Any], now: float, window_s: float, drop_psi: float,
                stale_after_s: Optional[float] = None) -> List[Tuple[str, float, float, float]]:
    """``[(position, drop_psi, from_psi, to_psi)]`` for tires whose pressure fell by >= ``drop_psi`` inside the window."""
    out = []
    for f, samples in (memo.get("tires") or {}).items():
        if f not in TIRES:
            continue
        recent = [s for s in samples if now - s[0] <= window_s]
        if len(recent) < 2:
            continue
        if stale_after_s is not None and now - recent[-1][0] > stale_after_s:
            continue            # the newest reading of this tire is old: it says nothing about now
        latest = recent[-1][1]
        peak = max(s[1] for s in recent[:-1])
        if peak - latest >= drop_psi:
            out.append((TIRES[f][0], round(peak - latest, 1), round(peak, 1), round(latest, 1)))
    return out


def summary_line(state: VehicleState, min_psi: Any = 0) -> Optional[str]:
    """One line for the status card, e.g. ``Tires (psi): FL 42 · FR 42 · RL 41 · RR 41`` with ``LOW`` on a low one."""
    bits = []
    for f, (pos, axle) in TIRES.items():
        v = state.value(f)
        if not plausible(v):
            continue
        short = "".join(w[0] for w in pos.split()).upper()
        low = float(v) < low_threshold_psi(state, axle, min_psi)
        bits.append(f"{short} {float(v):.0f}" + (" LOW" if low else ""))
    return ("Tires (psi): " + " · ".join(bits)) if bits else None
