"""Plugin settings: defaults, typed access and the live read-through to Hermes config.

Values live under ``plugins.entries.pit-crew.settings`` in the user's ``config.yaml`` (the
Desktop settings form writes there). They are re-read on every access so a change made in the
form applies without a restart. Secrets never appear here — adapters get them from Hermes'
secret scope.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .constants import PLUGIN_ID

logger = logging.getLogger(__name__)

DEFAULTS: Dict[str, Any] = {
    # --- vehicle -------------------------------------------------------------------------
    "adapter": "simulator",          # simulator | homeassistant | teslamate | <third-party>
    "history": "own",                # own (recorded by Pit Crew) | teslamate | simulator | none
    "car_name": "",
    "electricity_rate_usd_kwh": 0.0, # optional: price per kWh, used to estimate charging cost (0 = unknown)
    "battery_kwh": 75.0,             # usable pack size used by readiness math when history is silent
    "sim_seed": 42,                  # simulated car: seed for its random events and history
    "sim_events": True,              # simulated car: random-but-seeded events (door left open, ...)
    # --- Home Assistant ------------------------------------------------------------------
    "ha_url": "",
    "ha_profile": "tesla_fleet",     # bundled profile name, or a path to your own JSON/YAML profile
    "ha_slug": "",                   # entity slug, e.g. 'my_car' for sensor.my_car_battery_level
    "ha_entity_overrides": {},       # {"battery_pct": "sensor.other_entity", ...}
    "ha_verify_tls": True,
    # --- direct Tesla (tesla-fleet-api) ---------------------------------------------------
    "tesla_mode": "teslemetry",      # teslemetry | tessie
    "tesla_vin": "",
    "tesla_min_poll_s": 120,         # at most one vehicle-data call per this many seconds
    "tesla_home_latitude": 0.0,      # optional: lets the adapter name zones; coordinates never leave the adapter
    "tesla_home_longitude": 0.0,
    "tesla_home_radius_m": 150,
    "tesla_zones": {},               # extra zones: {"work": [lat, lon, radius_m]}
    # --- TeslaMate (read-only) -----------------------------------------------------------
    "teslamate_url": "",
    "teslamate_car_id": 1,
    # --- safety --------------------------------------------------------------------------
    "armed": False,                  # False = dry-run: commands are validated + logged, car never contacted
    "trusted_zones": ["home"],
    "max_state_age_s": 600,          # risky commands are refused when the car's state is older than this
    "stale_after_s": 900,            # fields older than this are labelled stale
    "allow_unknown_motion": False,   # parked-ness unprovable (no FRESH speed/gear/driving data): refuse (fail closed)
    "verify_timeout_s": 20,
    "verify_interval_s": 2.0,
    "rate_limit_commands": 10,       # live commands allowed ...
    "rate_limit_window_s": 600,      # ... per this many seconds
    "dedupe_window_s": 60,           # identical live command inside this window is skipped
    "approval_confirmation": "strict",  # strict | hermes_gate (see docs/SAFETY.md)
    "arm_default_hours": 1,          # how long `/car arm` stays armed
    # --- watcher -------------------------------------------------------------------------
    "watcher_enabled": True,
    "poll_interval_s": 120,
    "asleep_poll_interval_s": 900,
    "alert_cooldown_min": 240,       # re-notify about a still-true condition no sooner than this
    "alert_push": True,              # push new alerts to the chat bound with `/car notify here`
    "alert_context": True,           # also hand unseen alerts to the model on the next turn
    "night_start": "22:00",
    "night_end": "06:00",
    "rules": {},                     # per-rule overrides: {"left_unlocked_away": {"enabled": false, "minutes": 20}}
    # --- readiness -----------------------------------------------------------------------
    "charge_schedule_start": "",      # optional HH:MM your car's scheduled/off-peak charging begins (readiness respects it)
    "default_departure": "07:30",
    "default_trip_mi": 30,
    "reserve_pct": 10,
    "timezone": "",
}

# Settings whose change requires rebuilding the adapter objects.
ADAPTER_KEYS = ("adapter", "history", "car_name", "tesla_mode", "tesla_vin", "tesla_min_poll_s", "tesla_home_latitude",
                "tesla_home_longitude", "tesla_home_radius_m", "tesla_zones", "ha_url", "ha_profile", "ha_slug",
                "ha_entity_overrides", "ha_verify_tls", "teslamate_url", "teslamate_car_id")

SettingsProvider = Callable[[], Mapping[str, Any]]


def hermes_settings_provider(plugin_id: str = PLUGIN_ID) -> SettingsProvider:
    """Read ``plugins.entries.<id>.settings`` through the documented ``load_config_readonly``."""

    def provider() -> Mapping[str, Any]:
        try:
            from hermes_cli.config import load_config_readonly

            cfg = load_config_readonly() or {}
        except Exception as exc:  # config unreadable: run on defaults, but say so
            logger.warning("could not read Hermes config (%s); using plugin defaults", exc)
            return {}
        entry = ((cfg.get("plugins") or {}).get("entries") or {}).get(plugin_id) or {}
        settings = entry.get("settings")
        return settings if isinstance(settings, dict) else {}

    return provider


class Settings:
    """Typed, always-fresh view of the plugin settings with defaults."""

    def __init__(self, provider: Optional[SettingsProvider] = None, overrides: Optional[Mapping[str, Any]] = None):
        self._provider = provider or (lambda: {})
        self._overrides: Dict[str, Any] = dict(overrides or {})

    # -- raw ------------------------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        merged = dict(DEFAULTS)
        try:
            raw = dict(self._provider() or {})
        except Exception as exc:
            logger.warning("settings provider failed (%s); using defaults", exc)
            raw = {}
        for key, val in raw.items():
            if key in DEFAULTS and val is not None:
                merged[key] = _coerce(key, val)
        merged.update(self._overrides)
        return merged

    def get(self, key: str) -> Any:
        return self.snapshot()[key]

    def set_override(self, key: str, value: Any) -> None:
        """In-process override (used by tests and by `/car arm` for the runtime armed flag)."""
        self._overrides[key] = value

    def adapter_fingerprint(self) -> Tuple[Any, ...]:
        snap = self.snapshot()
        return tuple(repr(snap[k]) for k in ADAPTER_KEYS)

    # -- rules ----------------------------------------------------------------------------
    def rule_config(self, rule: str, defaults: Mapping[str, Any]) -> Dict[str, Any]:
        merged = dict(defaults)
        over = (self.get("rules") or {}).get(rule)
        if isinstance(over, dict):
            for k, v in over.items():
                if k in defaults:
                    merged[k] = v
        return merged


def _coerce(key: str, value: Any) -> Any:
    default = DEFAULTS[key]
    try:
        if isinstance(default, bool):
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if isinstance(default, int):
            return int(value)
        if isinstance(default, float):
            return float(value)
        if isinstance(default, list):
            if isinstance(value, str):
                return [p.strip() for p in value.split(",") if p.strip()]
            return list(value)
        if isinstance(default, dict):
            return dict(value)
        return str(value)
    except (TypeError, ValueError):
        logger.warning("setting %r has an invalid value %r; using default", key, value)
        return default


def parse_hhmm(text: str, fallback: Tuple[int, int] = (0, 0)) -> Tuple[int, int]:
    try:
        hh, mm = str(text).strip().split(":")
        h, m = int(hh), int(mm)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except ValueError:
        pass
    return fallback


def zones_list(value: Any) -> List[str]:
    return [str(z).strip().lower() for z in (value or []) if str(z).strip()]
