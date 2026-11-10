"""State sources: the command connection's own reading plus the optional TeslaMate overlay, merged per field.

Pit Crew separates three roles (docs/DECISIONS.md, "Connections: one command route, optional extra state source"):

* **Commands** go through exactly ONE writable connection (the ``adapter`` setting), or none (``adapter: teslamate``).
  There is no fallback or rerouting to another provider: a command whose outcome is unknown must never be resent another
  way (duplicate unlock / trunk risk).
* **State** is that connection's reading plus, optionally, TeslaMate (``teslamate_url``), merged here: per field the
  eligible newest observation wins, with the rules of :meth:`TeslaMateOverlay.merge` (an optimistic write counts as of the
  last real re-read; motion from either source fails closed; TeslaMate's estimated stamps never prove "already" or
  "confirmed"; offline is unknown).
* **History** is separate (own recording / TeslaMate / import) and untouched here.

This is the ONE implementation used by every real command adapter (Home Assistant with every profile, direct Teslemetry,
direct Tessie). The simulator never gets it (its state is fiction and must stay isolated from real-car data), nor does
``adapter: teslamate`` itself (it already is TeslaMate).

Also here, for every real adapter whether or not TeslaMate is configured: a timestamp more than :data:`FUTURE_SKEW_S`
in the future is not believed (the field is ignored, except a "moving" reading, which is kept: fail closed); a smaller
skew is clamped to now.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from ..brain.models import FieldValue, VehicleState
from ..redact import scrub

logger = logging.getLogger(__name__)

#: clocks of Home Assistant / TeslaMate / the service and this host may differ by this much; a stamp further ahead is
#: implausible (a broken clock or sensor) and is quarantined
FUTURE_SKEW_S = 120.0
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


def normalize_vin(value: Any) -> Optional[str]:
    """A plausible 17-character VIN in upper case, else None (``REDACTED``, empty, garbage: identity unknown)."""
    text = str(value or "").strip().upper()
    return text if _VIN_RE.match(text) else None


def _says_moving(name: str, value: Any) -> bool:
    from .teslamate import _says_moving as says
    return says(name, value)


def quarantine_future(fields: Dict[str, FieldValue], now: float, skew_s: float = FUTURE_SKEW_S) -> List[str]:
    """Clamp stamps up to ``skew_s`` ahead of ``now`` to ``now``; a field stamped further ahead is ignored (unavailable),
    except a reading that says the car is moving, which is kept (stamped now, marked estimated) because motion fails
    closed. Returns the names quarantined. Idempotent."""
    out: List[str] = []
    for name, fv in list(fields.items()):
        if fv is None or not fv.available:
            continue
        if fv.changed_at is not None and fv.changed_at > now:
            fv.changed_at = now if fv.changed_at <= now + skew_s else None
        if fv.updated_at is None or fv.updated_at <= now:
            continue
        if fv.updated_at <= now + skew_s:
            fv.updated_at = now
            continue
        out.append(name)
        if name in ("moving", "speed_mph", "shift_state") and _says_moving(name, fv.value):
            fv.updated_at, fv.estimated = now, True
        else:
            fields[name] = FieldValue(None, None, False)
    return out


class StateSources:
    """The state side of one real command adapter: ``primary`` (e.g. ``home_assistant``, ``teslemetry``, ``tessie``) and
    the optional TeslaMate overlay (:class:`adapters.teslamate.TeslaMateOverlay`, GET only)."""

    def __init__(self, primary: str, overlay: Any = None) -> None:
        self.primary = primary
        self.overlay = overlay

    # -- what is configured ---------------------------------------------------------------------------------------
    def names(self) -> List[str]:
        return [self.primary] + (["teslamate"] if self.overlay is not None else [])

    @property
    def overlay_ok(self) -> bool:
        """TeslaMate is configured, was readable at the last look and is the same car."""
        return self.overlay is not None and self.overlay.last_error is None and not self.overlay.mismatch

    def describe(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"state_sources": self.names()}
        if self.overlay is not None:
            out["fresher_state_from"] = "teslamate (read-only)"
            if self.overlay.mismatch:
                out["state_sources_note"] = self.overlay.mismatch
        return out

    # -- per read ---------------------------------------------------------------------------------------------------
    def apply(self, state: VehicleState, now: float, vehicle_id: Optional[str] = None) -> VehicleState:
        """Quarantine future stamps, then merge TeslaMate into ``state`` in place (when configured, readable and the same
        car). ``vehicle_id``: the primary's VIN, when it knows one (a mismatch with TeslaMate's VIN stops the merge)."""
        fields = state.fields
        hints = state.hints
        held = quarantine_future(fields, now)
        cr = hints.get("car_reported_at")
        if isinstance(cr, (int, float)) and cr > now:
            hints["car_reported_at"] = now if cr <= now + FUTURE_SKEW_S else None
        notes: List[str] = []
        if held:
            notes.append(f"{self._label()} reported times more than {int(FUTURE_SKEW_S)} s in the future for "
                         f"{', '.join(sorted(held))}; those readings are ignored (check the clocks).")
        if self.overlay is not None:
            notes.append(self._merge_overlay(fields, hints, now, vehicle_id))
        if notes:
            hints["source_note"] = " ".join(n for n in notes if n)
        return state

    def baseline(self, now: float) -> None:
        if self.overlay is not None and not self.overlay.mismatch:
            self.overlay.baseline(now)

    def _label(self) -> str:
        return {"home_assistant": "Home Assistant", "teslemetry": "Teslemetry", "tessie": "Tessie"}.get(
            self.primary, self.primary)

    def _mark_primary(self, fields: Dict[str, FieldValue]) -> None:
        from .teslamate import OVERLAY_FIELDS
        for n in OVERLAY_FIELDS:
            if n in fields and fields[n].available:
                fields[n].source = self.primary

    def _merge_overlay(self, fields: Dict[str, FieldValue], hints: Dict[str, Any], now: float,
                       vehicle_id: Optional[str]) -> str:
        from .teslamate import MOTION_FIELDS, SOURCE_NOTE, TM_AWAKE_AGE_S
        ov = self.overlay
        mine = normalize_vin(vehicle_id)
        if mine is not None:
            theirs = ov.vehicle_id(now)
            if theirs is not None and theirs != mine:
                ov.mismatch = (f"TeslaMate car {ov.car_id} is a different car from the one {self._label()} controls "
                               "(their VINs differ), so its readings are ignored. Set “TeslaMate · Car id” "
                               "(teslamate_car_id) to the matching car's id (TeslaMateApi lists them at /api/v1/cars).")
                self._mark_primary(fields)
                return ov.mismatch
            ov.mismatch = None
        tm = ov.read(now)
        if tm is None:
            self._mark_primary(fields)
            return (f"TeslaMate could not be read ({scrub(str(ov.last_error))}); these readings are "
                    f"{self._label()}'s only.")
        ov.merge(fields, tm, hints.get("car_reported_at"), primary=self.primary)
        # any source saying moving wins (fail closed), whatever its age
        moving = [fields[n] for n in MOTION_FIELDS if fields.get(n) is not None and fields[n].available
                  and _says_moving(n, fields[n].value)]
        if moving:
            newest = max(moving, key=lambda f: f.updated_at or now)
            fields["moving"] = FieldValue(True, newest.updated_at or now, source=newest.source, estimated=newest.estimated)
        note = SOURCE_NOTE.format(age=int(TM_AWAKE_AGE_S))
        if ov.future_note:
            note += " " + ov.future_note
        return note


def build_state_sources(ctx: Any, primary: str) -> StateSources:
    """The state sources of a real command adapter: always ``primary``; TeslaMate too when ``teslamate_url`` is set."""
    from .teslamate import build_teslamate_overlay
    return StateSources(primary, build_teslamate_overlay(ctx))
