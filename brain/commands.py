"""Command vocabulary, risk classes and argument validation.

The brain only ever speaks this vocabulary. Adapters translate it to a car's real services.

Risk classes
------------
``read``       no effect on the car.
``comfort``    climate, seat heat, defrost, charge limit, start/stop charging.
``security``   locks, trunk/frunk, windows, sentry. ``lock``, ``close_windows`` and ``sentry_on``
               move the car toward the *safer* state (``safe_direction``) and need no human
               approval; ``unlock``, ``open_trunk``, ``open_frunk``, ``vent_windows`` and
               ``sentry_off`` do.
``attention``  honk, flash (annoy the neighbours / drain a bit of battery) — human approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .models import ACTIVELY_CHARGING, VehicleState

READ = "read"
COMFORT = "comfort"
SECURITY = "security"
ATTENTION = "attention"
RISK_CLASSES = (READ, COMFORT, SECURITY, ATTENTION)

# Bounded value ranges (also enforced again by the policy engine and the adapters).
TEMP_MIN_F = 59.0
TEMP_MAX_F = 82.0
CHARGE_LIMIT_MIN = 50
CHARGE_LIMIT_MAX = 100
CHARGE_AMPS_MIN = 1
CHARGE_AMPS_MAX = 48
SEAT_NAMES = ("driver", "passenger", "rear_left", "rear_center", "rear_right")
SEAT_LEVEL_MAX = 3

Verifier = Callable[[VehicleState, Mapping[str, Any]], Optional[bool]]


@dataclass(frozen=True)
class ArgSpec:
    name: str
    kind: str  # "float" | "int" | "bool" | "enum"
    required: bool = False
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    choices: Tuple[str, ...] = ()
    default: Any = None
    help: str = ""


@dataclass(frozen=True)
class CommandSpec:
    name: str
    risk: str
    summary: str
    args: Tuple[ArgSpec, ...] = ()
    # Human approval is required for live execution of this command.
    requires_approval: bool = False
    # Moves the car toward the safer state (lock, close, sentry on).
    safe_direction: bool = False
    # Policy preconditions.
    needs_parked: bool = False
    needs_fresh_state: bool = False
    needs_trusted_zone: bool = False
    # Executing this on a sleeping car would wake it (drains the battery).
    wakes_car: bool = True
    # State fields the adapter must be able to *read* for read-back verification (informational).
    verify_fields: Tuple[str, ...] = ()
    verify: Optional[Verifier] = None


def _v_locked(want: bool) -> Verifier:
    def check(state: VehicleState, args: Mapping[str, Any]) -> Optional[bool]:
        if not state.has("locked"):
            return None
        return bool(state.value("locked")) is want
    return check


def _v_bool(field_name: str, want: bool) -> Verifier:
    def check(state: VehicleState, args: Mapping[str, Any]) -> Optional[bool]:
        if not state.has(field_name):
            return None
        val = state.value(field_name)
        if isinstance(val, list):
            val = bool(val)
        return bool(val) is want
    return check


def _v_charging(want: bool) -> Verifier:
    def check(state: VehicleState, args: Mapping[str, Any]) -> Optional[bool]:
        if not state.has("charging_state"):
            return None
        return (state.value("charging_state") in ACTIVELY_CHARGING) is want
    return check


def _v_charge_limit(state: VehicleState, args: Mapping[str, Any]) -> Optional[bool]:
    if not state.has("charge_limit_pct"):
        return None
    return abs(float(state.value("charge_limit_pct")) - float(args["percent"])) < 0.5


def _v_target_temp(state: VehicleState, args: Mapping[str, Any]) -> Optional[bool]:
    if not state.has("target_temp_f"):
        return None
    return abs(float(state.value("target_temp_f")) - float(args["temp_f"])) <= 1.0


def _v_climate_on(state: VehicleState, args: Mapping[str, Any]) -> Optional[bool]:
    if not state.has("climate_on"):
        return None
    if not state.value("climate_on"):
        return False
    if "temp_f" in args and state.has("target_temp_f"):
        return abs(float(state.value("target_temp_f")) - float(args["temp_f"])) <= 1.0
    return True


def _v_defrost(state: VehicleState, args: Mapping[str, Any]) -> Optional[bool]:
    if not state.has("defrost_on"):
        return None
    return bool(state.value("defrost_on")) is bool(args.get("on", True))


def _c(name: str, risk: str, summary: str, **kw: Any) -> CommandSpec:
    return CommandSpec(name=name, risk=risk, summary=summary, **kw)


_SPECS: List[CommandSpec] = [
    # comfort ---------------------------------------------------------------------------
    _c("climate_on", COMFORT, "Turn the climate system on (optionally to a target temperature).",
       args=(ArgSpec("temp_f", "float", minimum=TEMP_MIN_F, maximum=TEMP_MAX_F,
                     help="Target cabin temperature in °F"),),
       verify_fields=("climate_on",), verify=_v_climate_on),
    _c("climate_off", COMFORT, "Turn the climate system off.",
       verify_fields=("climate_on",), verify=_v_bool("climate_on", False)),
    _c("set_temp", COMFORT, "Set the cabin target temperature.",
       args=(ArgSpec("temp_f", "float", required=True, minimum=TEMP_MIN_F, maximum=TEMP_MAX_F,
                     help="Target cabin temperature in °F"),),
       verify_fields=("target_temp_f",), verify=_v_target_temp),
    _c("defrost", COMFORT, "Turn max defrost on or off.",
       args=(ArgSpec("on", "bool", default=True),),
       verify_fields=("defrost_on",), verify=_v_defrost),
    _c("seat_heat", COMFORT, "Set a seat heater level (0 = off, 3 = hottest).",
       args=(ArgSpec("seat", "enum", required=True, choices=SEAT_NAMES),
             ArgSpec("level", "int", required=True, minimum=0, maximum=SEAT_LEVEL_MAX))),
    _c("charge_limit", COMFORT, "Set the charge limit (percent).",
       args=(ArgSpec("percent", "int", required=True, minimum=CHARGE_LIMIT_MIN, maximum=CHARGE_LIMIT_MAX),),
       verify_fields=("charge_limit_pct",), verify=_v_charge_limit),
    _c("charge_start", COMFORT, "Start charging (car must be plugged in).",
       verify_fields=("charging_state",), verify=_v_charging(True)),
    _c("charge_stop", COMFORT, "Stop charging.",
       verify_fields=("charging_state",), verify=_v_charging(False)),
    _c("charge_amps", COMFORT, "Set the charging current in amps.",
       args=(ArgSpec("amps", "int", required=True, minimum=CHARGE_AMPS_MIN, maximum=CHARGE_AMPS_MAX),)),
    # security --------------------------------------------------------------------------
    _c("lock", SECURITY, "Lock the car (safe direction — no approval needed).",
       safe_direction=True, verify_fields=("locked",), verify=_v_locked(True)),
    _c("close_windows", SECURITY, "Close all windows (safe direction — no approval needed).",
       safe_direction=True, verify_fields=("windows_open",), verify=_v_bool("windows_open", False)),
    _c("sentry_on", SECURITY, "Turn Sentry mode on (safe direction — no approval needed).",
       safe_direction=True, verify_fields=("sentry_on",), verify=_v_bool("sentry_on", True)),
    _c("unlock", SECURITY, "Unlock the car. Needs human approval.",
       requires_approval=True, needs_parked=True, needs_fresh_state=True, needs_trusted_zone=True,
       verify_fields=("locked",), verify=_v_locked(False)),
    _c("open_trunk", SECURITY, "Open the trunk. Needs human approval.",
       requires_approval=True, needs_parked=True, needs_fresh_state=True, needs_trusted_zone=True,
       verify_fields=("trunk_open",), verify=_v_bool("trunk_open", True)),
    _c("open_frunk", SECURITY, "Open the frunk. Needs human approval.",
       requires_approval=True, needs_parked=True, needs_fresh_state=True, needs_trusted_zone=True,
       verify_fields=("frunk_open",), verify=_v_bool("frunk_open", True)),
    _c("vent_windows", SECURITY, "Vent (crack open) the windows. Needs human approval.",
       requires_approval=True, needs_parked=True, needs_fresh_state=True,
       verify_fields=("windows_open",), verify=_v_bool("windows_open", True)),
    _c("sentry_off", SECURITY, "Turn Sentry mode off. Needs human approval.",
       requires_approval=True, needs_fresh_state=True,
       verify_fields=("sentry_on",), verify=_v_bool("sentry_on", False)),
    # attention -------------------------------------------------------------------------
    _c("honk", ATTENTION, "Honk the horn once. Needs human approval.",
       requires_approval=True, needs_parked=True),
    _c("flash", ATTENTION, "Flash the lights once. Needs human approval.",
       requires_approval=True, needs_parked=True),
]

COMMANDS: Dict[str, CommandSpec] = {c.name: c for c in _SPECS}


class ArgError(ValueError):
    """Raised for an unusable command argument; the message is safe to show the user."""


def get_spec(name: str) -> Optional[CommandSpec]:
    return COMMANDS.get(str(name or "").strip().lower())


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on", "y"):
        return True
    if s in ("0", "false", "no", "off", "n"):
        return False
    raise ArgError(f"expected true/false, got {value!r}")


def validate_args(spec: CommandSpec, raw: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Return a clean, canonical args dict or raise :class:`ArgError`.

    Unknown keys are rejected except the policy flags ``override_zone`` and ``wake_ok`` which are
    handled by the safety engine.
    """
    raw = dict(raw or {})
    flags = {k: raw.pop(k) for k in ("override_zone", "wake_ok") if k in raw}
    known = {a.name for a in spec.args}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ArgError(f"unknown argument(s) for {spec.name}: {', '.join(unknown)}")
    clean: Dict[str, Any] = {}
    for a in spec.args:
        if a.name not in raw or raw[a.name] is None or raw[a.name] == "":
            if a.required:
                raise ArgError(f"{spec.name} needs '{a.name}'" + (f" ({a.help})" if a.help else ""))
            if a.default is not None:
                clean[a.name] = a.default
            continue
        val = raw[a.name]
        try:
            if a.kind == "float":
                num = float(val)
                if a.minimum is not None and num < a.minimum or a.maximum is not None and num > a.maximum:
                    raise ArgError(f"{a.name} must be between {a.minimum:g} and {a.maximum:g}, got {num:g}")
                clean[a.name] = round(num, 1)
            elif a.kind == "int":
                num_i = int(round(float(val)))
                if a.minimum is not None and num_i < a.minimum or a.maximum is not None and num_i > a.maximum:
                    raise ArgError(f"{a.name} must be between {a.minimum:g} and {a.maximum:g}, got {num_i}")
                clean[a.name] = num_i
            elif a.kind == "bool":
                clean[a.name] = _coerce_bool(val)
            elif a.kind == "enum":
                s = str(val).strip().lower().replace(" ", "_").replace("-", "_")
                if s not in a.choices:
                    raise ArgError(f"{a.name} must be one of {', '.join(a.choices)}, got {val!r}")
                clean[a.name] = s
            else:  # pragma: no cover - guarded by specs above
                raise ArgError(f"unsupported argument kind {a.kind}")
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ArgError):
                raise
            raise ArgError(f"{a.name}: {exc}") from exc
    for key, val in flags.items():
        clean[key] = _coerce_bool(val)
    return clean


def canonical_args(args: Mapping[str, Any]) -> str:
    """Stable string form used for de-duplication and approval-ticket matching."""
    import json
    return json.dumps(dict(args), sort_keys=True, separators=(",", ":"), default=str)


def command_catalog() -> List[Dict[str, Any]]:
    """Compact, JSON-ready catalog (for tool descriptions, docs and the Desktop page)."""
    out = []
    for spec in COMMANDS.values():
        out.append({
            "command": spec.name, "risk": spec.risk, "summary": spec.summary,
            "needs_human_approval": spec.requires_approval, "safe_direction": spec.safe_direction,
            "args": [{"name": a.name, "type": a.kind, "required": a.required,
                      **({"min": a.minimum} if a.minimum is not None else {}),
                      **({"max": a.maximum} if a.maximum is not None else {}),
                      **({"choices": list(a.choices)} if a.choices else {})} for a in spec.args],
        })
    return out


@dataclass
class Capabilities:
    """What a particular car/adapter can read and do. The brain never offers anything outside it."""

    state_fields: frozenset = field(default_factory=frozenset)
    commands: frozenset = field(default_factory=frozenset)
    history: bool = False
    notes: str = ""

    def can_read(self, name: str) -> bool:
        return name in self.state_fields

    def can_do(self, command: str) -> bool:
        return command in self.commands

    def to_dict(self) -> Dict[str, Any]:
        return {"state_fields": sorted(self.state_fields), "commands": sorted(self.commands),
                "history": self.history, "notes": self.notes}

    def merged(self, other: "Capabilities") -> "Capabilities":
        return Capabilities(self.state_fields | other.state_fields, self.commands | other.commands,
                            self.history or other.history, (self.notes + " " + other.notes).strip())
