"""Home Assistant adapter: live state + commands over HA's REST API.

``GET /api/states`` is read (free — it does not contact the car) and mapped to
:class:`VehicleState` through an entity-mapping *profile*. Commands go out as
``POST /api/services/<domain>/<service>``. A long-lived access token is required, supplied
through Hermes' secret scope.
"""

from __future__ import annotations

import logging
import math
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..brain.commands import COMMANDS, Capabilities
from ..brain.models import FIELD_NAMES, FieldValue, VehicleState
from ..brain.units import convert_by_dimension
from ..constants import ENV_HA_TOKEN
from ..redact import scrub
from .base import AdapterError, AdapterUnavailable, BuildContext, CarAdapter, CommandNotSupported, CommandResult
from .http import json_request, normalize_base_url
from .profile import disabled_field_names, enable_instructions, entity_ids_for_field, load_profile

logger = logging.getLogger(__name__)

BAD_STATES = ("unavailable", "unknown", "none", "")
OPEN_WORDS = frozenset({"OPEN", "OPEN_TILT", "TILT", "INTERMEDIATE", "ON", "TRUE", "OPENING"})
CLOSED_WORDS = frozenset({"CLOSED", "OFF", "FALSE", "CLOSING"})
STATE_FETCH_TTL_S = 3.0
# TeslaMate states that positively mean "not driving". Everything else (offline, start, unknown strings) is NO vote.
PARKED_STATES = frozenset({"asleep", "online", "charging", "suspended", "updating"})
#: fields whose value HA does not re-write while unchanged (a shut door, a plugged cable); they are confirmed by the car's live
#: fields while it is online. Charging state/power/current, motion, location and the live readings are deliberately absent.
CONFIRMABLE_FIELDS = frozenset({"locked", "doors_open", "windows_open", "trunk_open", "frunk_open", "sentry_on",
                                "charge_port_open", "plugged_in", "charge_limit_pct", "charge_current_max_a",
                                "software_update_available", "software_version", "target_temp_f",
                                "tire_rec_front_psi", "tire_rec_rear_psi", "asleep"})
#: derived/odd fields that are not evidence of a recent poll
NEVER_EVIDENCE = frozenset({"moving"})
#: the evidence of a live poll must be this fresh (the default stale limit) for any promotion to happen
CONFIRM_MAX_AGE_S = 900.0
#: a coordinator refresh writes every entity of the car in the same event-loop pass; stamps this close count as that pass
REFRESH_TOLERANCE_S = 2.0


def _finite(value: float) -> float:
    """A reading that is NaN or infinite is a broken sensor, not data: it would defeat every comparison downstream
    (``nan > 1`` is False, so a NaN speed used to read as 'parked')."""
    if not math.isfinite(value):
        raise ValueError("non-finite reading")
    return value


def parse_ha_time(text: Optional[str]) -> Optional[float]:
    if not text or not isinstance(text, str):
        return None
    try:
        s = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


class HomeAssistantAdapter(CarAdapter):
    kind = "homeassistant"

    def __init__(self, base_url: str, token: Optional[str], profile: Dict[str, Any], slug: str,
                 name: str = "", overrides: Optional[Mapping[str, str]] = None, verify_tls: bool = True,
                 timeout: float = 10.0) -> None:
        self._base = normalize_base_url(base_url, "Home Assistant")
        if not token:
            raise AdapterError("Home Assistant access token is missing. Paste a long-lived token into Settings → Plugins → Pit Crew → “Home Assistant · Access token” (or set "+ENV_HA_TOKEN+" in the profile .env)")
        slug = (slug or "").strip()
        if not slug:
            raise AdapterError("Pit Crew doesn't know which Home Assistant car to use yet. Run `/car discover` to find it, then enter it as “Home Assistant · Car name”")
        self._token = token
        self._profile = profile
        self._slug = slug
        self._name = name or self._slug.replace("_", " ").title()
        self._overrides = dict(overrides or {})
        self._verify = verify_tls
        self._timeout = timeout
        self._lock = threading.RLock()
        self._states: Dict[str, Dict[str, Any]] = {}
        self._have_states = False
        self.label = profile.get("label") or "Home Assistant car"
        self.tested = bool(profile.get("tested"))
        #: integrations that write the commanded entity's state themselves as soon as the API call returns
        self.optimistic = bool(profile.get("optimistic_commands"))
        self._targets = self._command_targets(profile, self._slug)

    @staticmethod
    def _command_targets(profile: Mapping[str, Any], slug: str) -> frozenset:
        """Every entity a profile command acts on: the entities an optimistic integration writes itself."""
        out = set()
        for cspec in (profile.get("commands") or {}).values():
            for step in cspec.get("steps") or []:
                if "entity" in step:
                    out.add(str(step["entity"]).replace("{slug}", slug))
                elif "entity_by_arg" in step:
                    out.update(str(e).replace("{slug}", slug) for e in (step["entity_by_arg"].get("map") or {}).values())
        return frozenset(out)

    # -- HTTP -----------------------------------------------------------------------------
    def _get(self, path: str) -> Any:
        return json_request("GET", self._base + path, token=self._token, timeout=self._timeout,
                            verify_tls=self._verify, what="Home Assistant")

    def _post(self, path: str, body: Dict[str, Any]) -> Any:
        return json_request("POST", self._base + path, token=self._token, body=body, timeout=self._timeout,
                            verify_tls=self._verify, what="Home Assistant")

    def _refresh_states(self) -> Dict[str, Dict[str, Any]]:
        data = self._get("/api/states")
        if not isinstance(data, list):
            raise AdapterError("Home Assistant /api/states did not return a list")
        states = {s["entity_id"]: s for s in data
                  if isinstance(s, dict) and isinstance(s.get("entity_id"), str)}
        for s in states.values():                       # tolerate odd shapes instead of crashing a whole poll
            if not isinstance(s.get("attributes"), dict):
                s["attributes"] = {}
        with self._lock:
            self._states = states
            self._have_states = True
        return states

    # -- field mapping --------------------------------------------------------------------
    def _entity(self, field: str, spec: Dict[str, Any]) -> List[str]:
        return entity_ids_for_field(spec, self._slug, self._overrides.get(field))

    @staticmethod
    def _stamp(obj: Mapping[str, Any]) -> Optional[float]:
        return parse_ha_time(obj.get("last_reported")) or parse_ha_time(obj.get("last_updated"))

    def _map_field(self, field: str, spec: Dict[str, Any], states: Mapping[str, Dict[str, Any]],
                   now: float) -> FieldValue:
        kind = spec["kind"]
        ids = self._entity(field, spec)
        objs = [states.get(i) for i in ids]
        if kind == "open_list":
            present = [(n, o) for n, o in zip(spec["entities"].keys(), objs) if o is not None]
            if not present:
                return FieldValue(None, None, False)
            names = [n for n, o in present if str(o.get("state")).lower() == "on"]
            usable = [o for _, o in present if str(o.get("state")).lower() not in BAD_STATES]
            if not usable or (len(usable) < len(present) and not names):
                # R14: nothing open among the sensors we CAN read, but some are unavailable: that is "unknown", not "all closed"
                return FieldValue(None, None, False)
            stamps = [self._stamp(o) for o in usable]
            return FieldValue(names, min(s for s in stamps if s) if any(stamps) else now)
        obj = next((o for o in objs if o is not None), None)      # first of the entity and its older-version aliases
        if obj is None:
            return FieldValue(None, None, False)
        raw = str(obj.get("state", "")).strip()
        low = raw.lower()
        attrs = obj.get("attributes") or {}
        stamp = self._stamp(obj) or now
        try:
            value: Any
            if kind == "timestamp_minutes":
                if low in BAD_STATES:
                    return FieldValue(0, stamp) if spec.get("none_is_zero") else FieldValue(None, None, False)
                ts = parse_ha_time(raw)
                if ts is None:
                    return FieldValue(None, None, False)
                value = max(0.0, round((ts - now) / 60.0, 1))
                return FieldValue(value, stamp)
            if low in BAD_STATES:
                return FieldValue(None, None, False)
            if kind == "number":
                value = _finite(convert_by_dimension(_finite(float(raw)), spec.get("dimension", ""),
                                                     attrs.get("unit_of_measurement")))
            elif kind == "attr":
                attr = attrs.get(spec["attr"])
                if attr is None:
                    return FieldValue(None, None, False)
                if isinstance(attr, (int, float)) and spec.get("dimension"):
                    unit = attrs.get("temperature_unit") or attrs.get("unit_of_measurement")
                    value = _finite(convert_by_dimension(_finite(float(attr)), spec["dimension"], unit))
                else:
                    value = attr
            elif kind in ("attr_open_list", "attr_open"):
                return self._map_openings(spec, obj, low, attrs, stamp)
            elif kind == "on_off":
                if low not in ("on", "off"):
                    return FieldValue(None, None, False)
                value = (low == "on") != bool(spec.get("invert"))
            elif kind == "enum":
                value = spec.get("map", {}).get(low, "unknown")
            elif kind == "cover_open":
                if low in ("open", "opening"):
                    value = True
                elif low in ("closed", "closing"):
                    value = False
                else:
                    return FieldValue(None, None, False)
            elif kind == "lock_locked":
                if low in ("locked", "locking"):
                    value = True
                elif low in ("unlocked", "unlocking", "open", "opening"):
                    value = False
                else:  # jammed etc.
                    return FieldValue(None, None, False)
            elif kind == "climate_on":
                value = low != "off"
            elif kind == "zone":
                value = "away" if low == "not_home" else low.replace(" ", "_")
            elif kind == "shift":
                value = raw[:1].upper() if raw else None
                if value not in ("P", "D", "R", "N"):
                    return FieldValue(None, None, False)
            else:  # pragma: no cover - guarded by profile validation
                return FieldValue(None, None, False)
        except (TypeError, ValueError, AttributeError, KeyError):
            return FieldValue(None, None, False)
        return FieldValue(value, stamp)

    @staticmethod
    def _apply_untrusted(spec: Mapping[str, Any], fv: FieldValue) -> FieldValue:
        """Integrations that fill in a parked-looking default when the car sent nothing (Tessie / Teslemetry turn a missing gear
        into "P" and a missing speed into 0, and serve cached or restored values with fresh timestamps) cannot prove the car
        is parked. A field with ``untrusted_values`` is unavailable when it reads one of them; other readings (D/R/N, a real
        speed) pass through, so "moving" is still detected."""
        bad = spec.get("untrusted_values")
        if not bad or not fv.available or isinstance(fv.value, bool):
            return fv
        for b in bad:
            if fv.value == b or (isinstance(b, str) and isinstance(fv.value, str) and fv.value.upper() == b.upper()):
                return FieldValue(None, None, False)
        return fv

    def _apply_data_time(self, spec: Mapping[str, Any], fv: FieldValue, states: Mapping[str, Dict[str, Any]]) -> FieldValue:
        """Cloud-polled integrations (kia_uvo) write the same cached copy into Home Assistant every few minutes, so HA's own
        stamp says "fresh" for data that may be hours old. A field with ``data_time_entity`` takes the car's data time from
        that timestamp sensor instead; with ``data_time_required`` it is unavailable when that time cannot be read (fail
        closed: a parked-by-gear claim never rests on data of unknown age)."""
        tmpl = spec.get("data_time_entity")
        if not tmpl or not fv.available:
            return fv
        obj = states.get(str(tmpl).replace("{slug}", self._slug))
        ts = parse_ha_time(obj.get("state")) if obj is not None else None
        if ts is None:
            return FieldValue(None, None, False) if spec.get("data_time_required") else fv
        fv.updated_at = min(fv.updated_at, ts) if fv.updated_at is not None else ts
        return fv

    @staticmethod
    def _map_openings(spec: Dict[str, Any], obj: Mapping[str, Any], low: str, attrs: Mapping[str, Any],
                      stamp: float) -> FieldValue:
        """Openings reported as attributes of one entity (BMW's ``Lids`` / ``Windows``): ``{"leftFront": "CLOSED", ...}``."""
        wanted = spec["attrs"] if spec["kind"] == "attr_open_list" else {"_": spec["attr"]}
        seen = {name: str(attrs[a]).strip().upper() for name, a in wanted.items() if attrs.get(a) is not None}
        if not seen:
            return FieldValue(None, None, False)
        opened = [n for n, v in seen.items() if v in OPEN_WORDS]
        unsure = [n for n, v in seen.items() if v not in OPEN_WORDS and v not in CLOSED_WORDS]
        if not opened and unsure:
            return FieldValue(None, None, False)              # R14: "nothing open" is only claimed when every reading says closed
        if spec["kind"] == "attr_open":
            return FieldValue(bool(opened), stamp)
        return FieldValue(opened, stamp)

    def _moving_state_entity(self) -> Optional[str]:
        ent = self._overrides.get("moving_state_entity") or self._profile.get("moving_state_entity")
        return str(ent).replace("{slug}", self._slug) if ent else None

    def _driving_state(self, states: Mapping[str, Dict[str, Any]], now: float) -> Optional[Tuple[bool, Optional[float]]]:
        """A TeslaMate-style state entity (``driving`` / ``online`` / ``asleep`` / ``charging``…): driving ⇒ moving."""
        eid = self._moving_state_entity()
        obj = states.get(eid) if eid else None
        if obj is None:
            return None
        low = str(obj.get("state", "")).strip().lower()
        if low in BAD_STATES:
            return None
        if low == "driving":
            return True, self._stamp(obj) or now
        if low in PARKED_STATES:
            return False, self._stamp(obj) or now
        return None  # offline (could be driving through a dead zone), start, or any unknown value: no vote

    def _mark_assumed(self, fields: Dict[str, FieldValue], states: Mapping[str, Dict[str, Any]]) -> Optional[float]:
        """Optimistic integrations (tesla_fleet, teslemetry, tessie: ``self._attr_is_locked = False;
        self.async_write_ha_state()`` right after the API call) make a commanded entity *look* like the car reported the
        new value, a second after the command, although the car was never re-read. Only a coordinator refresh re-reads
        the car, and a refresh re-writes every entity of the car in one pass, including the sensors no command touches.

        So: the newest stamp among the mapped entities that no command writes is when the car was last re-read
        (returned). A field read from a command-target entity whose own stamp is newer than that was written by the
        integration itself, not by a re-read: it is marked ``assumed``. Errs towards "assumed" (e.g. a streamed update on
        teslemetry): under-claiming is the safe direction."""
        reread: List[float] = []
        for name, spec in self._profile["fields"].items():
            if name in NEVER_EVIDENCE or name not in fields or not fields[name].available:
                continue
            ids = self._entity(name, spec) if spec.get("kind") != "open_list" else \
                [str(e).replace("{slug}", self._slug) for e in spec["entities"].values()]
            for eid in ids:
                obj = states.get(eid)
                if obj is None or eid in self._targets:
                    continue
                st = self._stamp(obj)
                if st is not None:
                    reread.append(st)
                break
        last_reread = max(reread) if reread else None
        for name, spec in self._profile["fields"].items():
            fv = fields.get(name)
            if fv is None or not fv.available or spec.get("kind") == "open_list":
                continue
            eid = next((i for i in self._entity(name, spec) if i in states), None)
            if eid is None or eid not in self._targets:
                continue
            own = self._stamp(states[eid])
            if own is not None and (last_reread is None or own > last_reread + REFRESH_TOLERANCE_S):
                fv.assumed = True
        return last_reread

    @staticmethod
    def _confirm_while_online(fields: Dict[str, FieldValue], now: float) -> None:
        """R1 (revised after the 2026-11-09 real-car test): HA only moves an entity's ``last_reported`` when the integration
        writes it, and tesla_fleet does not re-write an unchanged door/lock/window/port value, so a shut door looks days old
        although the car is fine. While the car is online, the *slow-changing* fields in :data:`CONFIRMABLE_FIELDS` are
        therefore confirmed as of the newest stamp among the car's *live* fields (battery, range, temperatures...) -- proof the
        integration is polling right now -- and ``changed_at`` keeps the real stamp. That proof must itself be no older than
        :data:`CONFIRM_MAX_AGE_S`.
        Everything else keeps its own honest ``last_reported`` age. In particular charging state / power / current are NEVER
        promoted: the real test showed "charging" for 36 min after the car had stopped, because HA had not re-written those
        sensors. An awake car does not mean HA has fresh values."""
        asleep = fields.get("asleep")
        if not (asleep and asleep.available and asleep.value is False):
            return
        live = [f.updated_at for n, f in fields.items()
                if n not in CONFIRMABLE_FIELDS and n not in NEVER_EVIDENCE and f.available and f.updated_at
                and not f.assumed]
        if not live:
            return
        newest = max(live)
        if now - newest > CONFIRM_MAX_AGE_S:
            return
        for name in CONFIRMABLE_FIELDS:
            f = fields.get(name)
            if f is not None and f.available and not f.assumed and f.updated_at is not None and f.updated_at < newest:
                f.changed_at = f.updated_at
                f.updated_at = newest

    def motion_hint(self) -> str:
        return str(self._profile.get("motion_hint") or (
            "Map a speed or gear entity (profile fields speed_mph / shift_state), or a state entity whose value "
            "'driving' means moving (ha_entity_overrides: moving_state_entity)."))

    def motion_warnings(self) -> List[str]:
        """Actionable warnings when no motion source exists in HA (so unlock/trunk/frunk/vent are blocked)."""
        if not self._have_states:
            return []
        fields = self._profile["fields"]
        wanted = [(f, entity_ids_for_field(fields[f], self._slug, self._overrides.get(f))[0])
                  for f in ("shift_state", "speed_mph") if f in fields]
        if not wanted:
            return []
        absent = [e for _, e in wanted if e not in self._states]
        driving_eid = self._moving_state_entity()
        have_driving = bool(driving_eid and driving_eid in self._states)
        if not absent or have_driving:
            return []
        return ["Unlock, trunk, frunk and window-vent are blocked until the car's motion can be confirmed. "
                + self.motion_hint() + " Missing entities: " + ", ".join(absent) + "."]

    def _missing_disabled(self, fields: Optional[Any] = None) -> List[str]:
        """Disabled-by-default fields whose entity is absent from Home Assistant (a disabled entity has no state)."""
        if not self._have_states:
            return []
        out = []
        for name in disabled_field_names(self._profile, fields):
            ids = entity_ids_for_field(self._profile["fields"][name], self._slug, self._overrides.get(name))
            if not any(i in self._states for i in ids):
                out.append(name)
        return out

    def _missing_disabled_commands(self) -> List[Dict[str, Any]]:
        if not self._have_states:
            return []
        out = []
        for item in self._profile.get("disabled_command_entities") or []:
            if item["entity"].replace("{slug}", self._slug) not in self._states:
                out.append(item)
        return out

    def disabled_entity_hint(self, fields: Optional[Any] = None) -> str:
        missing = self._missing_disabled(fields)
        return enable_instructions(self._profile, missing) if missing else ""

    def setup_warnings(self) -> List[str]:
        """Entities the profile wants that Home Assistant ships disabled (motion ones are covered by motion_warnings)."""
        missing = [n for n in self._missing_disabled() if n not in ("shift_state", "speed_mph")]
        out: List[str] = []
        if missing:
            titles = ", ".join(str(self._profile["fields"][n].get("enable_as") or n) for n in missing)
            out.append(f"Not reported yet: {titles}. " + enable_instructions(self._profile, missing))
        cmds = self._missing_disabled_commands()
        if cmds:
            names = ", ".join(f"{c['enable_as']} (for {c['for']})" for c in cmds)
            out.append(f"These controls exist in Home Assistant but are disabled, so Pit Crew can't use them yet: {names}. "
                       + enable_instructions(self._profile, [], [c["enable_as"] for c in cmds]))
        return out

    def fetch_state(self, now: float) -> VehicleState:
        states = self._refresh_states()
        fields: Dict[str, FieldValue] = {}
        for name, spec in self._profile["fields"].items():
            fields[name] = self._apply_untrusted(spec, self._apply_data_time(spec, self._map_field(name, spec, states, now), states))
        hints: Dict[str, Any] = {}
        if self.optimistic:
            refreshed = self._mark_assumed(fields, states)
            if refreshed is not None:
                hints["car_reported_at"] = refreshed
        # Derived 'moving' from every motion source the car offers: speed, gear and (optionally) a TeslaMate-style
        # state entity whose value 'driving' means moving. Any source saying moving wins; "not moving" is only claimed
        # from sources that reported, and carries the freshest of their timestamps (policy then checks it is fresh).
        votes: List[Tuple[bool, Optional[float]]] = []
        speed = fields.get("speed_mph")
        shift = fields.get("shift_state")
        if speed and speed.available and speed.value is not None:
            votes.append((float(speed.value) > 1.0, speed.updated_at))
        if shift and shift.available and shift.value:
            votes.append((shift.value in ("D", "R", "N"), shift.updated_at))
        driving = self._driving_state(states, now)
        if driving is not None:
            votes.append(driving)
        if votes:
            moving_ts = [t for v, t in votes if v and t]
            quiet_ts = [t for v, t in votes if not v and t]
            if any(v for v, _ in votes):
                fields["moving"] = FieldValue(True, max(moving_ts) if moving_ts else now)
            else:
                fields["moving"] = FieldValue(False, max(quiet_ts) if quiet_ts else None)
        plug = fields.get("plugged_in")
        cstate = fields.get("charging_state")
        if plug and plug.available and plug.value is False and (
                cstate is None or not cstate.available or cstate.value in ("stopped", "unknown")):
            fields["charging_state"] = FieldValue("disconnected", plug.updated_at)
        self._confirm_while_online(fields, now)
        for name in FIELD_NAMES:
            fields.setdefault(name, FieldValue(None, None, False))
        if self._profile.get("tire_min_psi_default"):
            hints["tire_min_psi"] = float(self._profile["tire_min_psi_default"])
        return VehicleState(name=self._name, source=self.kind, captured_at=now, fields=fields, hints=hints)

    # -- capabilities ---------------------------------------------------------------------
    def capabilities(self) -> Capabilities:
        if not self._have_states:
            try:
                self._refresh_states()
            except AdapterError as exc:  # optimistic fallback: profile's fields/commands
                logger.info("capabilities: HA unreachable (%s); assuming the profile's full set", exc)
        states = self._states if self._have_states else None
        fields = set()
        for name, spec in self._profile["fields"].items():
            ids = self._entity(name, spec)
            if states is None or any(i in states for i in ids):
                fields.add(name)
        eid = self._moving_state_entity()
        if {"speed_mph", "shift_state"} & fields or (states is not None and eid and eid in states):
            fields.add("moving")
        commands = set()
        for cname, cspec in (self._profile.get("commands") or {}).items():
            if states is None or self._command_entities_present(cspec, states):
                commands.add(cname)
        return Capabilities(frozenset(fields), frozenset(commands), history=False,
                            notes=f"Home Assistant profile '{self._profile.get('name')}'"
                                  f"{'' if self.tested else ' (verified against the integration source and fixtures; not run on a real car)' if self._profile.get('verified_against') else ' (UNTESTED starter profile)'}")

    def _command_entities_present(self, cspec: Mapping[str, Any], states: Mapping[str, Any]) -> bool:
        for step in cspec.get("steps", []):
            if "entity" in step:
                if step["entity"].replace("{slug}", self._slug) not in states:
                    return False
            else:
                mapping = step["entity_by_arg"]["map"]
                if not any(e.replace("{slug}", self._slug) in states for e in mapping.values()):
                    return False
        return True

    # -- commands -------------------------------------------------------------------------
    def send_command(self, command: str, args: Mapping[str, Any]) -> CommandResult:
        cspec = (self._profile.get("commands") or {}).get(command)
        if cspec is None:
            raise CommandNotSupported(f"the '{self._profile.get('name')}' profile has no '{command}' command")
        if command not in COMMANDS:  # pragma: no cover - validated at load
            raise CommandNotSupported(f"unknown command {command}")
        calls = self._plan(cspec, args)
        done: List[str] = []
        for service_path, payload in calls:
            try:
                self._post(f"/api/services/{service_path}", payload)
            except AdapterError as exc:
                if done:   # an earlier step already went through: the car may be half-way there
                    err = AdapterError(f"{exc} (after {', '.join(done)} had already been sent)")
                    err.partial = True  # type: ignore[attr-defined]
                    raise err from exc
                raise
            done.append(service_path)
        return CommandResult(accepted=True, detail="sent to Home Assistant: " + ", ".join(done))

    def _plan(self, cspec: Mapping[str, Any], args: Mapping[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
        plan: List[Tuple[str, Dict[str, Any]]] = []
        for step in cspec["steps"]:
            when = step.get("when_arg")
            if when and when not in args:
                continue
            if "service_by_arg" in step:
                sba = step["service_by_arg"]
                service = sba["true"] if bool(args.get(sba["arg"], True)) else sba["false"]
            else:
                service = step["service"]
            if "entity_by_arg" in step:
                eba = step["entity_by_arg"]
                template = eba["map"].get(str(args.get(eba["arg"])))
                if template is None:
                    raise AdapterError(f"no entity mapped for {eba['arg']}={args.get(eba['arg'])!r}")
            else:
                template = step["entity"]
            entity = template.replace("{slug}", self._slug)
            if self._have_states and entity not in self._states:
                raise AdapterError(f"entity {entity} does not exist in Home Assistant; check “Home Assistant · Car name” and “Car brand mapping”")
            payload: Dict[str, Any] = {"entity_id": entity}
            for key, val in (step.get("data") or {}).items():
                payload[key] = self._resolve_data(val, args, entity)
            if "." not in service:
                raise AdapterError(f"bad service '{service}' in profile")
            plan.append((service.replace(".", "/", 1), payload))
        return plan

    def _resolve_data(self, val: Any, args: Mapping[str, Any], entity: str) -> Any:
        if not isinstance(val, dict) or "arg" not in val:
            return val
        raw = args[val["arg"]]
        if "map" in val:
            try:
                return val["map"][str(raw)]
            except KeyError:
                raise AdapterError(f"this car's Home Assistant integration does not accept {val['arg']}={raw!r}; "
                                   f"it accepts {', '.join(val['map'])}") from None
        if val.get("unit_of") == "temperature":
            attrs = (self._states.get(entity) or {}).get("attributes", {})
            unit = str(attrs.get("temperature_unit") or "°F")
            if unit.upper().replace("°", "") == "C":
                return round((float(raw) - 32.0) * 5.0 / 9.0 * 2) / 2.0  # HA climate steps are 0.5 °C
            return raw
        return raw

    def describe(self) -> Dict[str, Any]:
        return {"kind": self.kind, "label": self.label, "simulated": False, "tested_profile": self.tested,
                "profile": self._profile.get("name"), "host": scrub(self._base)}


def build_homeassistant(ctx: BuildContext) -> HomeAssistantAdapter:
    s = ctx.settings
    profile = load_profile(str(s.get("ha_profile") or "tesla_fleet"))
    return HomeAssistantAdapter(
        base_url=str(s.get("ha_url") or ""), token=ctx.secret(ENV_HA_TOKEN), profile=profile,
        slug=str(s.get("ha_slug") or ""), name=str(s.get("car_name") or ""),
        overrides=s.get("ha_entity_overrides") or {}, verify_tls=bool(s.get("ha_verify_tls", True)))
