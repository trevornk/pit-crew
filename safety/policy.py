"""Pre-flight policy checks run before *any* command, in dry-run and live mode alike.

The policy is deterministic code: it looks only at the command spec, the validated arguments, the
car's current state and the user's settings. It returns every check it ran so the decision can be
shown to the user (tool result, Desktop page, audit log) rather than being a black box.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from ..brain.commands import CHARGE_AMPS_MAX, CHARGE_AMPS_MIN, CHARGE_LIMIT_MAX, CHARGE_LIMIT_MIN, TEMP_MAX_F, TEMP_MIN_F
from ..brain.commands import CommandSpec
from ..brain.models import VehicleState
from ..settings import zones_list


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"check": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class PolicyResult:
    checks: List[Check] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def denials(self) -> List[str]:
        return [c.detail for c in self.checks if not c.ok]

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))

    def to_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed, "checks": [c.to_dict() for c in self.checks], "warnings": self.warnings}


DEFAULT_MOTION_HINT = "Give Pit Crew a fresh motion source (speed, gear, or a 'driving' state)."
OPT_OUT = (" (Only if you accept acting without knowing, you can set allow_unknown_motion to true — not recommended.)")


def _age_text(seconds: Optional[float]) -> str:
    if seconds is None:
        return "an unknown time"
    if seconds < 90:
        return f"{int(seconds)} s"
    if seconds < 5400:
        return f"{int(seconds // 60)} min"
    return f"{seconds / 3600:.1f} h"


def evaluate(spec: CommandSpec, args: Mapping[str, Any], state: VehicleState, settings: Mapping[str, Any],
             now: float, motion_hint: str = "") -> PolicyResult:
    res = PolicyResult()
    override_zone = bool(args.get("override_zone"))
    wake_ok = bool(args.get("wake_ok"))

    # -- bounded value ranges (second line of defence after argument validation) ---------------
    if "temp_f" in args:
        t = float(args["temp_f"])
        ok = TEMP_MIN_F <= t <= TEMP_MAX_F
        res.add("temperature_range", ok,
                f"{t:g}°F is within the allowed {TEMP_MIN_F:g}–{TEMP_MAX_F:g}°F" if ok
                else f"target {t:g}°F is outside the allowed {TEMP_MIN_F:g}–{TEMP_MAX_F:g}°F")
    if "percent" in args:
        p = int(args["percent"])
        ok = CHARGE_LIMIT_MIN <= p <= CHARGE_LIMIT_MAX
        res.add("charge_limit_range", ok,
                f"charge limit {p}% is within the allowed {CHARGE_LIMIT_MIN}–{CHARGE_LIMIT_MAX}%" if ok
                else f"charge limit {p}% is outside the allowed {CHARGE_LIMIT_MIN}–{CHARGE_LIMIT_MAX}%")
        if p > 90:
            res.warnings.append(f"charging to {p}% regularly shortens battery life; fine for a trip")
    if "amps" in args:
        a = int(args["amps"])
        ceiling = CHARGE_AMPS_MAX
        cap = state.value("charge_current_max_a")
        if isinstance(cap, (int, float)) and cap > 0:
            ceiling = min(ceiling, int(cap))
        ok = CHARGE_AMPS_MIN <= a <= ceiling
        res.add("charge_amps_range", ok,
                f"{a} A is within the allowed {CHARGE_AMPS_MIN}–{ceiling} A for this charger" if ok
                else f"{a} A is outside the allowed {CHARGE_AMPS_MIN}–{ceiling} A for this charger")

    # -- sleeping car: don't wake it for convenience ---------------------------------------
    if spec.wakes_car and state.value("asleep") is True and not wake_ok:
        res.add("car_awake", False,
                "the car is asleep and this command would wake it (that costs battery). Ask the user first, "
                "then retry with wake_ok=true")
    elif state.value("asleep") is True:
        res.warnings.append("the car is asleep; sending this will wake it")

    # -- state freshness ----------------------------------------------------------------------
    if spec.needs_fresh_state:
        newest = state.newest_update()
        limit = float(settings.get("max_state_age_s", 600))
        age = None if newest is None else max(0.0, now - newest)
        ok = age is not None and age <= limit
        res.add("state_fresh", ok,
                f"the car's data is {_age_text(age)} old (limit {_age_text(limit)}); refresh status before "
                "a security command")

    # -- parked -------------------------------------------------------------------------------
    if spec.needs_parked:
        motion = state.motion(now, float(settings.get("max_state_age_s", 600)))
        if motion is True:
            res.add("parked", False, "the car is moving; this command is refused while driving")
        elif motion is None:
            why = ("cannot confirm the car is parked: it reports no fresh speed, gear or driving state, and it is not "
                   "plugged in. " + (motion_hint or DEFAULT_MOTION_HINT) + OPT_OUT)
            if settings.get("allow_unknown_motion", False):
                res.add("parked", True, "could not confirm the car is parked (allowed by allow_unknown_motion)")
                res.warnings.append(why)
            else:
                res.add("parked", False, why)
        else:
            res.add("parked", True, "car is parked")

    # -- trusted zone ---------------------------------------------------------------------------
    if spec.needs_trusted_zone:
        trusted = zones_list(settings.get("trusted_zones"))
        zone = state.at_zone()
        zage = state.age_s("location_zone", now)
        zlimit = float(settings.get("max_state_age_s", 600))
        if zone is not None and (zage is None or zage > zlimit) and not override_zone:
            # R2: one fresh sensor must not vouch for a stale location
            res.add("trusted_zone", False,
                    f"the car's location reading is {_age_text(zage)} old (limit {_age_text(zlimit)}), so it cannot be "
                    "trusted to still be at a trusted zone; refresh status first")
        elif zone is not None and zone in trusted:
            res.add("trusted_zone", True, f"car is at trusted zone '{zone}'")
        elif override_zone:
            res.add("trusted_zone", True, "zone check overridden by explicit request")
            res.warnings.append(
                f"the car is NOT at a trusted location ({zone or 'location unknown'}); "
                "approving this overrides the zone protection")
        else:
            where = f"at '{zone}'" if zone else "at an unknown location"
            res.add("trusted_zone", False,
                    f"the car is {where}, not at a trusted zone ({', '.join(trusted) or 'none set'}). "
                    "If the user really wants this, retry with override_zone=true — they will be asked to confirm",
                    )

    # -- command-specific preconditions ---------------------------------------------------------
    if spec.name == "charge_start":
        if state.value("plugged_in") is False:
            res.add("plugged_in", False, "the car is not plugged in, so charging cannot start")
    return res
