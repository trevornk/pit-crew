"""Entity-mapping profiles for the Home Assistant adapter.

A profile is JSON (or YAML when PyYAML is importable) describing, for one integration, which HA
entity feeds each normalized state field and which HA service performs each command. Bundled
profiles live in ``adapters/profiles/``; users can point ``ha_profile`` at their own file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..brain.commands import COMMANDS
from ..brain.models import FIELD_NAMES
from .base import AdapterError

PROFILE_DIR = Path(__file__).resolve().parent / "profiles"
FIELD_KINDS = {"number", "attr", "on_off", "enum", "timestamp_minutes", "cover_open", "lock_locked",
               "open_list", "climate_on", "zone", "shift", "attr_open_list", "attr_open"}


def bundled_profiles() -> List[str]:
    return sorted(p.stem for p in PROFILE_DIR.glob("*.json"))


def _read(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # optional; Hermes ships PyYAML
        except ImportError:
            raise AdapterError("YAML profiles need PyYAML; use a .json profile instead") from None
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise AdapterError(f"profile {path.name} must be a JSON/YAML object")
    return data


def load_profile(name_or_path: str) -> Dict[str, Any]:
    """Load a bundled profile by name or a custom profile from a file path."""
    ref = (name_or_path or "").strip()
    if not ref:
        raise AdapterError("no Home Assistant profile configured (ha_profile)")
    path: Optional[Path] = None
    if ref in bundled_profiles():
        path = PROFILE_DIR / f"{ref}.json"
    else:
        candidate = Path(ref).expanduser()
        if candidate.is_file():
            path = candidate
    if path is None:
        raise AdapterError(f"unknown HA profile '{ref}'. Bundled: {', '.join(bundled_profiles())}")
    try:
        profile = _read(path)
    except (OSError, ValueError) as exc:
        raise AdapterError(f"could not read profile {path.name}: {exc}") from None
    problems = validate_profile(profile)
    if problems:
        raise AdapterError(f"profile {path.name} is invalid: " + "; ".join(problems[:5]))
    return profile


def validate_profile(profile: Dict[str, Any]) -> List[str]:
    problems: List[str] = []
    fields = profile.get("fields")
    if not isinstance(fields, dict) or not fields:
        problems.append("missing 'fields'")
        fields = {}
    for name, spec in fields.items():
        if name not in FIELD_NAMES:
            problems.append(f"unknown field '{name}'")
            continue
        if not isinstance(spec, dict) or spec.get("kind") not in FIELD_KINDS:
            problems.append(f"field '{name}' has an unknown kind")
            continue
        if spec.get("disabled_by_default") and not isinstance(spec.get("enable_as"), str):
            problems.append(f"field '{name}' is disabled_by_default and needs 'enable_as' (the entity's name in Home Assistant)")
        alts = spec.get("alt_entities")
        if alts is not None and not (isinstance(alts, list) and all(isinstance(a, str) for a in alts)):
            problems.append(f"field '{name}': 'alt_entities' must be a list of entity ids")
        if spec["kind"] == "open_list":
            if not isinstance(spec.get("entities"), dict) or not spec["entities"]:
                problems.append(f"field '{name}' needs 'entities'")
        elif "entity" not in spec:
            problems.append(f"field '{name}' needs 'entity'")
        elif spec["kind"] == "attr_open_list" and not (isinstance(spec.get("attrs"), dict) and spec["attrs"]):
            problems.append(f"field '{name}' needs 'attrs' (opening name -> attribute name)")
        elif spec["kind"] in ("attr", "attr_open") and not isinstance(spec.get("attr"), str):
            problems.append(f"field '{name}' needs 'attr'")
    for item in profile.get("disabled_command_entities") or []:
        if not (isinstance(item, dict) and isinstance(item.get("entity"), str) and isinstance(item.get("enable_as"), str)
                and item.get("for") in COMMANDS):
            problems.append("'disabled_command_entities' items need entity, enable_as and a valid 'for' command")
    commands = profile.get("commands") or {}
    if not isinstance(commands, dict):
        problems.append("'commands' must be an object")
        commands = {}
    for name, spec in commands.items():
        if name not in COMMANDS:
            problems.append(f"unknown command '{name}'")
            continue
        steps = (spec or {}).get("steps")
        if not isinstance(steps, list) or not steps:
            problems.append(f"command '{name}' needs 'steps'")
            continue
        for step in steps:
            if not isinstance(step, dict) or not ("service" in step or "service_by_arg" in step):
                problems.append(f"command '{name}' has a step without a service")
            elif not ("entity" in step or "entity_by_arg" in step):
                problems.append(f"command '{name}' has a step without an entity")
    return problems


def entity_ids_for_field(spec: Dict[str, Any], slug: str, override: Optional[str] = None) -> List[str]:
    """Entity ids that can feed a field, best first. ``alt_entities`` are the names older versions of the
    integration used; an explicit user override replaces all of them."""
    if spec["kind"] == "open_list":
        return [e.replace("{slug}", slug) for e in spec["entities"].values()]
    if override:
        return [override.replace("{slug}", slug)]
    return [e.replace("{slug}", slug) for e in [spec["entity"], *(spec.get("alt_entities") or [])]]


def disabled_field_names(profile: Dict[str, Any], fields: Optional[Any] = None) -> List[str]:
    """Fields whose entities the integration ships disabled in Home Assistant (so they only exist once enabled)."""
    wanted = set(fields) if fields is not None else None
    return [n for n, spec in profile["fields"].items()
            if spec.get("disabled_by_default") and (wanted is None or n in wanted)]


def enable_instructions(profile: Dict[str, Any], missing: List[str], extra_titles: Optional[List[str]] = None) -> str:
    """Plain-language steps to enable disabled-by-default entities in Home Assistant."""
    titles = [str(profile["fields"][n].get("enable_as") or n) for n in missing] + list(extra_titles or [])
    where = str(profile.get("integration_title") or profile.get("integration") or "your car's integration")
    return (f"Enable {', '.join(titles)} in Home Assistant (the integration ships "
            f"{'it' if len(titles) == 1 else 'them'} disabled): Settings → Devices & services → {where} → "
            "(your car) → entities → show disabled entities → open each one → turn on Enabled. "
            "They appear within a minute or so.")
