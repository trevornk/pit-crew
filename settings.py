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
    "live_mode": "on_demand",        # on_demand: practice until `/car live` / Go live (timed) | always: live until /car stop
    "trusted_zones": ["home"],
    "zone_rule": "trusted_zones",    # unlock/trunk/frunk: trusted_zones (only in those places) | anywhere
    "wake_for_commands": "ask",      # a command for a sleeping car: ask (agent asks first) | auto (the asked-for command wakes it)
    "max_state_age_s": 600,          # risky commands are refused when the car's state is older than this
    "stale_after_s": 900,            # fields older than this are labelled stale
    "allow_unknown_motion": False,   # parked-ness unprovable (no FRESH speed/gear/driving data): refuse (fail closed)
    "verify_timeout_s": 20,
    "refresh_after_command": True,   # slow-reporting HA profiles (tesla_fleet): ask HA to re-read the car once after a command
    "verify_interval_s": 2.0,
    "rate_limit_commands": 10,       # live commands allowed ...
    "rate_limit_window_s": 600,      # ... per this many seconds
    "dedupe_window_s": 60,           # identical live command inside this window is skipped
    "require_human_for": [],         # approval-class commands that ALWAYS need a person, even if Hermes approvals are off
    "live_default_hours": 1,         # how long Go live / `/car live` (no number) lasts; 1-24, never "forever"
    # --- watcher -------------------------------------------------------------------------
    "watcher_enabled": True,
    "poll_interval_s": 120,
    "asleep_poll_interval_s": 900,
    "alert_cooldown_min": 240,       # re-notify about a still-true condition no sooner than this
    "alert_push": True,              # push new alerts to the delivery target
    "alert_target": "",              # Hermes delivery target (telegram, discord:#car, matrix:!room:server); wins over `/car notify`
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

# The commands the old ``approval_confirmation: strict`` covered; an explicit ``strict`` migrates to exactly these.
LEGACY_STRICT_COMMANDS = ("unlock", "open_trunk", "open_frunk", "vent_windows", "sentry_off")
# The default live-mode length is a whole number of hours in this range (an explicit `/car live N` keeps 15 min - 72 h).
LIVE_DEFAULT_HOURS_MIN = 1
LIVE_DEFAULT_HOURS_MAX = 24

# Every approval-class command; the only values ``require_human_for`` accepts.
APPROVAL_COMMANDS = ("unlock", "open_trunk", "open_frunk", "vent_windows", "sentry_off", "honk", "flash")

# Owner-adjustable choices: allowed values, the first is the conservative default for a fresh install.
CHOICES: Dict[str, Tuple[str, ...]] = {
    "live_mode": ("on_demand", "always"),
    "zone_rule": ("trusted_zones", "anywhere"),
    "wake_for_commands": ("ask", "auto"),
}
# Numeric protections an owner may loosen or tighten, but never switch off: (min, max). Out-of-range values are clamped.
BOUNDS: Dict[str, Tuple[float, float]] = {
    "max_state_age_s": (60, 3600),        # freshness: 1 min .. 1 h (can be loosened, not disabled)
    "rate_limit_commands": (1, 60),       # live commands per window
    "rate_limit_window_s": (60, 86400),   # 1 min .. 1 day
    "dedupe_window_s": (5, 600),          # repeated identical command ignored for 5 s .. 10 min
}
# The settings' labels as the Settings form shows them (refusal messages name the exact setting that would allow it).
LABELS: Dict[str, str] = {
    "live_mode": "Safety · Live mode",
    "zone_rule": "Safety · Unlock/trunk/frunk zone",
    "wake_for_commands": "Safety · Wake a sleeping car for a command",
    "trusted_zones": "Safety · Places where unlock is allowed",
    "max_state_age_s": "Safety · Refuse risky commands when data is older than (s)",
    "rate_limit_commands": "Safety · Max commands per window",
    "rate_limit_window_s": "Safety · Rate-limit window (s)",
    "dedupe_window_s": "Safety · Ignore a repeated command for (s)",
    "require_human_for": "Advanced · Always ask a person for these, even if Hermes approvals are off",
    "allow_unknown_motion": "Advanced · Allow risky commands when I can't tell if the car is parked",
}


def clean_choice(key: str, value: Any) -> str:
    """One of the allowed values for ``key`` (case/space tolerant); anything else is the conservative default."""
    allowed = CHOICES[key]
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return text if text in allowed else allowed[0]


def clean_bounded(key: str, value: Any) -> int:
    lo, hi = BOUNDS[key]
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = float(DEFAULTS[key])
    if v != v:  # NaN
        v = float(DEFAULTS[key])
    return int(max(lo, min(hi, round(v))))


def clean_live_default_hours(value: Any) -> int:
    """A whole number of hours from 1 to 24 (bad values fall back to 1). Live mode always ends by itself."""
    try:
        h = float(value)
    except (TypeError, ValueError):
        return DEFAULTS["live_default_hours"]
    if h != h:  # NaN
        return DEFAULTS["live_default_hours"]
    return int(max(LIVE_DEFAULT_HOURS_MIN, min(LIVE_DEFAULT_HOURS_MAX, round(h))))


def clean_require_human_for(value: Any) -> List[str]:
    """Only known approval-class command names, lower-cased, de-duplicated, in a stable order."""
    if isinstance(value, str):
        value = [p for p in value.replace(";", ",").split(",")]
    try:
        wanted = {str(v).strip().lower() for v in (value or [])}
    except TypeError:
        wanted = set()
    return [c for c in APPROVAL_COMMANDS if c in wanted]


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
        # Migration: the old ``approval_confirmation: strict`` (only if the user set it explicitly) becomes the opt-in list.
        if raw.get("require_human_for") is None and str(raw.get("approval_confirmation") or "").strip().lower() == "strict":
            merged["require_human_for"] = list(LEGACY_STRICT_COMMANDS)
        # Migration: the old ``arm_default_hours`` setting is the default live length unless the new one is set.
        if raw.get("live_default_hours") is None and raw.get("arm_default_hours") is not None:
            merged["live_default_hours"] = _coerce("live_default_hours", raw["arm_default_hours"])
        # Migration: the old standing ``armed: true`` ("Advanced · Always live") is live_mode "always" unless live_mode is set.
        if raw.get("live_mode") is None and _is_true(raw.get("armed")):
            merged["live_mode"] = "always"
        merged.update({k: v for k, v in self._overrides.items() if k != "armed"})
        if "live_mode" not in self._overrides and _is_true(self._overrides.get("armed")):
            merged["live_mode"] = "always"
        merged["require_human_for"] = clean_require_human_for(merged["require_human_for"])
        merged["live_default_hours"] = clean_live_default_hours(merged["live_default_hours"])
        for key in CHOICES:
            merged[key] = clean_choice(key, merged[key])
        for key in BOUNDS:
            merged[key] = clean_bounded(key, merged[key])
        return merged

    def get(self, key: str) -> Any:
        return self.snapshot()[key]

    def set_override(self, key: str, value: Any) -> None:
        """In-process override (used by tests and by `/car arm` for the runtime armed flag)."""
        self._overrides[key] = value

    def save_rule(self, rule_id: str, changes: Mapping[str, Any], ctx: Any = None) -> None:
        """Write rule edits into the plugin's own ``rules`` setting (the single source of truth, the same value Settings ->
        Plugins -> Pit Crew shows). In-process uses ``ctx.set_config``; the dashboard backend (no ctx) uses the writer Hermes'
        own settings form uses (``hermes_cli.plugins_state.save_plugin_setting``). Outside Hermes (tests) it is held in memory.
        Raises on a real failure (managed install, unwritable config): a rule edit is never silently dropped."""
        for key, value in changes.items():
            path = f"rules.{rule_id}.{key}"
            setter = getattr(ctx, "set_config", None)
            if callable(setter):
                setter(path, value)
                continue
            try:
                from hermes_cli.plugins_state import save_plugin_setting
            except ImportError:
                try:
                    import hermes_cli  # noqa: F401
                except ImportError:
                    pass       # not running inside Hermes (tests, standalone): hold the edit in memory
                else:
                    raise RuntimeError("this Hermes version has no settings writer the page can use; "
                                       "change the rule in Settings -> Plugins -> Pit Crew instead") from None
                merged = {r: dict(v) for r, v in (self.get("rules") or {}).items() if isinstance(v, dict)}
                merged.setdefault(rule_id, {})[key] = value
                self._overrides["rules"] = merged
                continue
            save_plugin_setting(PLUGIN_ID, ("rules", rule_id, key), value)

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


def _is_true(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return value is True


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
