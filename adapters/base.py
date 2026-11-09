"""Adapter contract and registry.

An *adapter* connects the brain to one kind of car/data source. Anyone can ship one without
touching the brain: subclass :class:`CarAdapter`, register a factory with
:func:`register_adapter` (or expose a ``pit_crew.adapters`` entry point) — see
``docs/ADAPTERS.md``.
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

from ..brain.commands import Capabilities
from ..brain.models import FieldValue, VehicleState
from ..constants import ADAPTER_ENTRY_POINT_GROUP

logger = logging.getLogger(__name__)

# Third-party adapters receive THIS module from the entry-point hook and need nothing else from the plugin.
# Everything they use is exposed here: CarAdapter, HistoryProvider, record types, Capabilities, VehicleState,
# FieldValue, CommandResult, the error classes and register_adapter / register_history.
__all__ = [
    "AdapterError", "AdapterUnavailable", "BatteryHealth", "BuildContext", "Capabilities", "CarAdapter",
    "ChargeRecord", "CommandNotSupported", "CommandResult", "DriveRecord", "FieldValue", "HistoryProvider",
    "VehicleState", "SecretScopeUnavailable", "register_adapter", "register_history",
]


class AdapterError(Exception):
    """An adapter problem whose message is safe to show to the user (no secrets, no coordinates)."""


class AdapterUnavailable(AdapterError):
    """The data source could not be reached (network down, bad URL, auth failure)."""


CREDENTIAL_BG_TEXT = ("Background alerts need a Hermes version where plugin background tasks can read plugin secrets; "
                      "alerts are checked during chats until then.")


class SecretScopeUnavailable(AdapterUnavailable):
    """The secret EXISTS but this execution context has no Hermes secret scope (e.g. a background task under
    ``gateway.multiplex_profiles``). Deliberately distinct from "secret not set": do not tell the user the token is missing."""

    def __init__(self, message: str = CREDENTIAL_BG_TEXT) -> None:
        super().__init__(message)


class CommandNotSupported(AdapterError):
    """The adapter has no way to perform this command on this car."""


@dataclass
class CommandResult:
    """What an adapter reports after *sending* a command (not yet verified by read-back)."""

    accepted: bool
    detail: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


class CarAdapter(ABC):
    """Live state + commands for one car.

    Contract
    --------
    * ``fetch_state`` must be **cheap and must never wake a sleeping car** by itself.
    * ``send_command`` is only ever called by the safety engine, after policy, approval and
      armed checks. It must not retry in a way that could double-fire a command.
    * Raise :class:`AdapterError` (never leak tokens/URLs with secrets into messages).
    """

    kind: str = "base"
    label: str = "Car"
    simulated: bool = False

    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    def fetch_state(self, now: float) -> VehicleState: ...

    def send_command(self, command: str, args: Mapping[str, Any]) -> CommandResult:
        raise CommandNotSupported(f"{self.label} cannot perform '{command}'")

    def clock(self) -> Optional[float]:
        """Adapters that run on their own clock (the simulator) return it; others return None."""
        return None

    def report_latency_s(self) -> Optional[float]:
        """Roughly how long after a command the car's OWN report of the result can reach Pit Crew (seconds), or None when
        unknown. When this is far longer than the read-back wait, the safety engine reads once and answers right away
        instead of making the user wait for a report that cannot arrive in time."""
        return None

    def service_name(self) -> str:
        """Who accepts commands on the car's behalf, for messages ("Tesla accepted it"). Default: generic wording."""
        return "the car's service"

    def refresh_plan(self) -> Optional[Dict[str, Any]]:
        """``{"settle_s": float, "cost_note": str}`` when the adapter can ask its integration to re-read the car ONCE
        right after a command (see :meth:`request_refresh`), else None (the default). Only the safety engine uses it,
        and only after a live command whose result can be observed; status reads never refresh."""
        return None

    def request_refresh(self, timeout_s: float) -> str:
        """Ask the integration to re-read the car now (one request; must never wake a sleeping car). Returns what was
        refreshed, raises :class:`AdapterError` when the request failed."""
        raise CommandNotSupported(f"{self.label} cannot request a re-read")

    def after_send(self, now: float) -> None:
        """Called right after a command was accepted (before the read-back), with the current time. Adapters with a second state source take a
        baseline there, so a later change can be proven to have been reported after the command. Default: nothing."""

    def describe(self) -> Dict[str, Any]:
        return {"kind": self.kind, "label": self.label, "simulated": self.simulated}

    def close(self) -> None:
        """Release threads / sessions. Called when the adapter is replaced or the plugin unloads. Default: nothing."""


class HistoryProvider(ABC):
    """Read-only trip / charge history. Must never call anything that changes the car."""

    kind: str = "history"
    label: str = "History"

    @abstractmethod
    def drives(self, start: float, end: float, limit: int = 500) -> List["DriveRecord"]: ...

    @abstractmethod
    def charges(self, start: float, end: float, limit: int = 500) -> List["ChargeRecord"]: ...

    def battery_health(self) -> Optional["BatteryHealth"]:
        return None


@dataclass
class DriveRecord:
    start: float
    end: float
    miles: float
    duration_min: float
    start_pct: Optional[float] = None
    end_pct: Optional[float] = None
    kwh: Optional[float] = None
    wh_per_mi: Optional[float] = None
    outside_temp_f: Optional[float] = None
    speed_avg_mph: Optional[float] = None
    speed_max_mph: Optional[float] = None


@dataclass
class ChargeRecord:
    start: float
    end: float
    kwh_added: float
    kwh_used: Optional[float] = None
    cost: Optional[float] = None
    start_pct: Optional[float] = None
    end_pct: Optional[float] = None
    duration_min: float = 0.0
    outside_temp_f: Optional[float] = None


@dataclass
class BatteryHealth:
    max_capacity_kwh: Optional[float] = None
    current_capacity_kwh: Optional[float] = None
    health_pct: Optional[float] = None
    max_range_mi: Optional[float] = None
    current_range_mi: Optional[float] = None


# --------------------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------------------
# A factory receives a BuildContext and returns a CarAdapter.

@dataclass
class BuildContext:
    """Everything a factory may need; secrets arrive through ``secret`` (never ``os.environ``)."""

    settings: Mapping[str, Any]
    secret: Callable[[str], Optional[str]]
    data_dir: Optional[str] = None
    store: Any = None
    clock: Callable[[], float] = None  # type: ignore[assignment]


AdapterFactory = Callable[[BuildContext], CarAdapter]
HistoryFactory = Callable[[BuildContext], Optional[HistoryProvider]]

_ADAPTERS: Dict[str, AdapterFactory] = {}
_HISTORY: Dict[str, HistoryFactory] = {}
_entry_points_loaded = False
_entry_points_lock = threading.Lock()


def register_adapter(kind: str, factory: AdapterFactory, *, replace: bool = False) -> None:
    """Register a live-state adapter factory under ``kind`` (the value of the ``adapter`` setting)."""
    key = kind.strip().lower()
    if key in _ADAPTERS and not replace:
        raise ValueError(f"adapter '{key}' is already registered")
    _ADAPTERS[key] = factory


def register_history(kind: str, factory: HistoryFactory, *, replace: bool = False) -> None:
    """Register a history provider factory under ``kind`` (the value of the ``history`` setting)."""
    key = kind.strip().lower()
    if key in _HISTORY and not replace:
        raise ValueError(f"history provider '{key}' is already registered")
    _HISTORY[key] = factory


def load_entry_point_adapters() -> List[str]:
    """Load third-party adapters advertised via the ``pit_crew.adapters`` entry-point group.

    Each entry point must resolve to a callable ``register(registry)`` that receives this module
    (so it can call ``register_adapter`` / ``register_history``). A broken third-party adapter is
    logged and skipped; it can never break the plugin.
    """
    global _entry_points_loaded
    with _entry_points_lock:
        if _entry_points_loaded:
            return []
        loaded = _load_entry_point_adapters()
        _entry_points_loaded = True            # only once the registry is complete: another thread waits on the lock
    return loaded


def _load_entry_point_adapters() -> List[str]:
    loaded: List[str] = []
    try:
        from importlib.metadata import entry_points
        eps = entry_points()
        group = eps.select(group=ADAPTER_ENTRY_POINT_GROUP) if hasattr(eps, "select") else eps.get(
            ADAPTER_ENTRY_POINT_GROUP, [])  # pragma: no cover - py<3.10
    except Exception as exc:  # metadata machinery failure must not break the plugin
        logger.warning("could not enumerate pit_crew adapter entry points: %s", exc)
        return loaded
    import sys
    for ep in group:
        try:
            hook = ep.load()
            hook(sys.modules[__name__])
            loaded.append(ep.name)
        except Exception as exc:
            logger.warning("adapter entry point %r failed to load: %s", ep.name, exc)
    return loaded


def available_adapters() -> List[str]:
    return sorted(_ADAPTERS)


def available_history() -> List[str]:
    return sorted(_HISTORY)


def build_adapter(kind: str, ctx: BuildContext) -> CarAdapter:
    load_entry_point_adapters()
    key = (kind or "").strip().lower()
    if key in ("teslemetry", "tessie") and key not in _ADAPTERS:
        # "Connection: teslemetry" is what people naturally type; it means the direct Tesla adapter in that mode.
        key = "tesla"
        ctx = BuildContext(settings={**dict(ctx.settings), "tesla_mode": (kind or "").strip().lower()}, secret=ctx.secret,
                           data_dir=ctx.data_dir, store=ctx.store, clock=ctx.clock)
    factory = _ADAPTERS.get(key)
    if factory is None:
        raise AdapterError(f"unknown adapter '{kind}'. Available: {', '.join(available_adapters()) or 'none'}")
    return factory(ctx)


def build_history(kind: str, ctx: BuildContext) -> Optional[HistoryProvider]:
    key = (kind or "").strip().lower()
    if key in ("", "none", "off"):
        return None
    load_entry_point_adapters()
    factory = _HISTORY.get(key)
    if factory is None:
        raise AdapterError(f"unknown history provider '{kind}'. Available: {', '.join(available_history()) or 'none'}")
    return factory(ctx)
