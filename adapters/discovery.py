"""Auto-discovery: propose an entity mapping from the entities a Home Assistant instance exposes.

Two passes:

1. **Profile match** — for each bundled profile, derive the car "slug" from the entities that
   match the profile's patterns (e.g. ``lock.my_car_lock`` -> slug ``my_car``) and score how many
   of the profile's fields are present.
2. **Generic proposal** — when no profile fits well, propose a mapping from entity domains and
   device classes (lock.*, climate.*, battery sensors, door/window binary sensors, device
   trackers…). The result is a ready-to-save custom profile *skeleton*; the user reviews it.

Nothing here writes to Home Assistant or changes settings; it only reads ``/api/states``.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .profile import bundled_profiles, disabled_field_names, enable_instructions, entity_ids_for_field, load_profile


def _pattern_for(template: str) -> "re.Pattern[str]":
    head, _, tail = template.partition("{slug}")
    return re.compile("^" + re.escape(head) + r"(?P<slug>[a-z0-9_]+)" + re.escape(tail) + "$")


def candidate_slugs(profile: Mapping[str, Any], entity_ids: Iterable[str]) -> List[str]:
    """Slugs implied by entities that match any of the profile's single-entity field templates."""
    ids = list(entity_ids)
    found: Dict[str, int] = defaultdict(int)
    templates = [spec["entity"] for spec in profile["fields"].values() if "entity" in spec]
    for tpl in templates:
        pat = _pattern_for(tpl)
        for eid in ids:
            m = pat.match(eid)
            if m:
                found[m.group("slug")] += 1
    return [s for s, _ in sorted(found.items(), key=lambda kv: -kv[1])]


def score_profile(profile: Mapping[str, Any], slug: str, entity_ids: set) -> Dict[str, Any]:
    matched: List[str] = []
    missing: List[str] = []
    optional_missing: List[str] = []
    for name, spec in profile["fields"].items():
        ids = entity_ids_for_field(dict(spec), slug)
        if any(i in entity_ids for i in ids):
            matched.append(name)
        elif spec.get("optional"):
            optional_missing.append(name)
        else:
            missing.append(name)
    required = len(matched) + len(missing)
    cmds_ok = []
    for cname, cspec in (profile.get("commands") or {}).items():
        ok = True
        for step in cspec["steps"]:
            if "entity" in step:
                ok = ok and step["entity"].replace("{slug}", slug) in entity_ids
            else:
                ok = ok and any(e.replace("{slug}", slug) in entity_ids for e in step["entity_by_arg"]["map"].values())
        if ok:
            cmds_ok.append(cname)
    warnings: List[str] = []
    motion = [f for f in ("shift_state", "speed_mph") if f in profile["fields"] and f in optional_missing]
    drv = profile.get("moving_state_entity")
    has_driving = bool(drv and drv.replace("{slug}", slug) in entity_ids)
    if motion and not has_driving:
        names = ", ".join(profile["fields"][f]["entity"].replace("{slug}", slug) for f in motion)
        warnings.append("Motion entities are missing, so unlock/trunk/frunk/vent will be refused. "
                        + (profile.get("motion_hint") or "Map a speed/gear/driving entity.") + f" Missing: {names}.")
    # Entities the integration ships disabled never show in /api/states; say which ones to enable (motion ones were
    # explained above). Odometer and tire pressure are the usual surprises.
    off = [n for n in disabled_field_names(profile) if n in optional_missing and n not in ("shift_state", "speed_mph")]
    if off:
        warnings.append(enable_instructions(profile, off))
    return {"profile": profile["name"], "slug": slug, "tested": bool(profile.get("tested")), "warnings": warnings,
            "coverage": round(len(matched) / required, 2) if required else 0.0,
            "fields_matched": matched, "fields_missing": missing, "commands_available": cmds_ok}


def discover(states: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Return ranked profile matches plus (if needed) a generic skeleton. Pure; no network."""
    entity_ids = {str(s.get("entity_id")) for s in states if s.get("entity_id")}
    state_list = [s for s in states]
    proposals: List[Dict[str, Any]] = []
    for pname in bundled_profiles():
        profile = load_profile(pname)
        for slug in candidate_slugs(profile, entity_ids)[:5]:
            proposals.append(score_profile(profile, slug, entity_ids))
    proposals.sort(key=lambda p: (-p["coverage"], not p["tested"]))
    best = proposals[0] if proposals else None
    out: Dict[str, Any] = {"entities_seen": len(entity_ids), "proposals": proposals[:5]}
    if best and best["coverage"] >= 0.6:
        out["warnings"] = best["warnings"]
        out["recommended"] = {"ha_profile": best["profile"], "ha_slug": best["slug"],
                              "note": ("tested profile" if best["tested"] else
                                       "UNTESTED starter profile — verify the values before trusting commands")}
    else:
        out["generic_proposal"] = propose_generic(state_list)
        out["recommended"] = None
    return out


def propose_generic(states: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Guess entity -> field assignments from domains/device classes. Returns ``{field: entity_id}``
    plus ``confidence`` notes; usable directly as ``ha_entity_overrides`` with ``ha_profile`` set to
    a profile whose slug is irrelevant, or as the basis of a custom profile file."""
    by_domain: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for s in states:
        eid = str(s.get("entity_id", ""))
        by_domain[eid.split(".", 1)[0]].append(s)

    def attrs(s: Mapping[str, Any]) -> Mapping[str, Any]:
        return s.get("attributes") or {}

    mapping: Dict[str, str] = {}
    notes: Dict[str, str] = {}

    def pick(field: str, candidates: List[Mapping[str, Any]], why: str) -> None:
        if candidates and field not in mapping:
            mapping[field] = str(candidates[0]["entity_id"])
            notes[field] = why + (f" ({len(candidates)} candidates)" if len(candidates) > 1 else "")

    sensors = by_domain.get("sensor", [])
    bsensors = by_domain.get("binary_sensor", [])
    pick("battery_pct", [s for s in sensors if attrs(s).get("device_class") == "battery"
                         and attrs(s).get("unit_of_measurement") == "%"
                         and not re.search(r"(phone|remote|key|tpms)", str(s["entity_id"]))],
         "battery-class sensor in %")
    pick("range_mi", [s for s in sensors if attrs(s).get("device_class") == "distance"
                      and re.search(r"range", str(s["entity_id"]))], "distance sensor named *range*")
    pick("odometer_mi", [s for s in sensors if "odometer" in str(s["entity_id"])], "sensor named *odometer*")
    pick("locked", by_domain.get("lock", []), "lock entity")
    pick("climate_on", by_domain.get("climate", []), "climate entity")
    pick("location_zone", by_domain.get("device_tracker", []), "device tracker")
    pick("plugged_in", [s for s in bsensors if attrs(s).get("device_class") == "plug"
                        or re.search(r"(plugged|charge_cable|charger_connected)", str(s["entity_id"]))],
         "plug-class binary sensor")
    pick("charge_power_kw", [s for s in sensors if attrs(s).get("device_class") == "power"
                             and re.search(r"charg", str(s["entity_id"]))], "power sensor named *charg*")
    pick("charging_state", [s for s in sensors if re.search(r"charging(_state|_status)?$", str(s["entity_id"]))],
         "sensor named *charging*")
    pick("cabin_temp_f", [s for s in sensors if attrs(s).get("device_class") == "temperature"
                          and re.search(r"(inside|cabin|interior)", str(s["entity_id"]))], "inside temperature")
    pick("outside_temp_f", [s for s in sensors if attrs(s).get("device_class") == "temperature"
                            and re.search(r"(outside|outdoor|exterior)", str(s["entity_id"]))], "outside temperature")
    pick("sentry_on", [s for s in by_domain.get("switch", []) if "sentry" in str(s["entity_id"])], "switch named *sentry*")
    pick("software_update_available", by_domain.get("update", []), "update entity")
    doors = [s for s in bsensors if attrs(s).get("device_class") == "door"]
    windows = [s for s in bsensors if attrs(s).get("device_class") == "window"]
    return {"entity_overrides": mapping, "doors": [str(s["entity_id"]) for s in doors],
            "windows": [str(s["entity_id"]) for s in windows], "notes": notes,
            "how_to_use": ("These are guesses from entity types. Put the entity_overrides into the "
                           "'ha_entity_overrides' setting, review `/car` status for sanity, and keep the car "
                           "disarmed until the values look right. Commands need a profile with service steps; "
                           "copy adapters/profiles/tesla_fleet.json as a starting point.")}
