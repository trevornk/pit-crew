"""Simulated car — the default adapter, so everything works without owning a supported car.

State evolves with (simulated) time: charging adds energy up to the limit, the cabin temperature
moves toward the climate target or the outside temperature, a drive uses energy, and a few
random-but-seeded events happen (a door left ajar, a window left open, charging interrupted, a
software update appearing). The simulator keeps its own clock so demos and tests can fast-forward
(``control("advance", minutes=90)``). State is persisted in the plugin store so the agent process
and the Desktop backend always see the same car.
"""

from __future__ import annotations

import math
import random
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional

from ..brain.commands import COMMANDS, Capabilities
from ..brain.models import FIELD_NAMES, FieldValue, VehicleState
from .base import (AdapterError, BatteryHealth, BuildContext, CarAdapter, ChargeRecord, CommandResult,
                   DriveRecord, HistoryProvider)

KV_KEY = "sim_state"
DOORS = ("front_driver", "front_passenger", "rear_driver", "rear_passenger")
WINDOWS = ("front_driver", "front_passenger", "rear_driver", "rear_passenger")
EVENT_KINDS = ("door_ajar", "window_open", "charge_interrupted", "software_update")
CHARGE_EFFICIENCY = 0.92
EFFICIENCY_WH_MI = 265.0


def _default_state(seed: int, now: float) -> Dict[str, Any]:
    rng = random.Random(seed)
    return {
        "seed": seed,
        "created": now,
        "last_tick": now,
        "offset_s": 0.0,
        "battery_kwh": 75.0,
        "energy_kwh": 75.0 * 0.58,
        "charge_limit": 80,
        "plugged_in": True,
        "charging_enabled": True,
        "charge_amps": 30,
        "charge_amps_max": 32,
        "voltage": 240.0,
        "locked": True,
        "doors": {d: False for d in DOORS},
        "windows": {w: False for w in WINDOWS},
        "trunk_open": False,
        "frunk_open": False,
        "sentry_on": False,
        "climate_on": False,
        "target_temp_f": 70.0,
        "cabin_temp_f": 66.0,
        "defrost_on": False,
        "seats": {},
        "zone": "home",
        "asleep": False,
        "odometer_mi": 12480.0 + rng.randint(0, 400),
        "sw_version": "2026.1.1",
        "update_available": False,
        "base_outside_f": 52.0 + rng.uniform(-6, 6),
        "cold_override_f": None,
        "events": [],  # filled in below
        "fired_events": 0,
        "last_report": now,
        "trips": [],
    }


def _schedule_events(state: Dict[str, Any], rng: random.Random, start: float, hours: float = 96.0) -> None:
    n = rng.randint(3, 5)
    events = []
    for _ in range(n):
        events.append({"at": start + rng.uniform(1.5, hours) * 3600, "kind": rng.choice(EVENT_KINDS)})
    events.sort(key=lambda e: e["at"])
    state["events"] = events


class SimulatorAdapter(CarAdapter):
    kind = "simulator"
    label = "Simulated car"
    simulated = True

    def __init__(self, store: Any = None, seed: int = 42, events_enabled: bool = True,
                 name: str = "Simulated car", wall_clock: Callable[[], float] = time.time):
        self._store = store
        self._seed = int(seed)
        self._events_enabled = bool(events_enabled)
        self._name = name or "Simulated car"
        self._wall = wall_clock
        self._lock = threading.RLock()
        self._state: Optional[Dict[str, Any]] = None
        self._saved_json: Optional[str] = None

    # -- persistence ----------------------------------------------------------------------
    def _load(self) -> Dict[str, Any]:
        if self._store is not None:
            saved = self._store.kv_get(KV_KEY)
            if isinstance(saved, dict) and saved.get("seed") == self._seed:
                self._state = saved
                return saved
        if self._state is not None and self._store is None:
            return self._state
        wall = self._wall()
        state = _default_state(self._seed, wall)
        if self._events_enabled:
            _schedule_events(state, random.Random(self._seed * 7919 + 13), wall)
        self._state = state
        self._save()
        return state

    def _save(self) -> None:
        if self._store is not None and self._state is not None:
            self._store.kv_set(KV_KEY, self._state)

    # -- clock ----------------------------------------------------------------------------
    def clock(self) -> float:
        with self._lock:
            st = self._load()
            return self._wall() + float(st["offset_s"])

    # -- physics --------------------------------------------------------------------------
    def _outside_f(self, st: Dict[str, Any], sim_now: float) -> float:
        if st.get("cold_override_f") is not None:
            return float(st["cold_override_f"])
        hour = datetime.fromtimestamp(sim_now).hour + datetime.fromtimestamp(sim_now).minute / 60.0
        return st["base_outside_f"] + 11.0 * math.sin((hour - 9.0) / 24.0 * 2 * math.pi)

    def _tick(self, st: Dict[str, Any], sim_now: float) -> None:
        dt = sim_now - st["last_tick"]
        if dt <= 0:
            return
        # Fire scheduled events that fall inside (last_tick, sim_now].
        remaining = []
        for ev in st["events"]:
            if ev["at"] <= sim_now:
                self._apply_event(st, ev["kind"])
            else:
                remaining.append(ev)
        st["events"] = remaining
        # Step in <=5 min slices so thresholds (limit reached) are handled accurately.
        t = st["last_tick"]
        while t < sim_now:
            step = min(300.0, sim_now - t)
            self._step(st, step, t + step)
            t += step
        st["last_tick"] = sim_now
        if not st["asleep"]:
            st["last_report"] = sim_now

    def _step(self, st: Dict[str, Any], dt: float, t_end: float) -> None:
        outside = self._outside_f(st, t_end)
        cap = st["battery_kwh"]
        # charging
        if st["plugged_in"] and st["charging_enabled"] and st["zone"] in ("home", "work"):
            limit_kwh = cap * st["charge_limit"] / 100.0
            if st["energy_kwh"] < limit_kwh - 1e-6:
                p_kw = st["voltage"] * st["charge_amps"] / 1000.0
                if st["energy_kwh"] / cap > 0.9:
                    p_kw *= 0.6
                st["energy_kwh"] = min(limit_kwh, st["energy_kwh"] + p_kw * CHARGE_EFFICIENCY * dt / 3600.0)
                st["asleep"] = False
        # drains
        drain_kw = 0.03
        if st["climate_on"]:
            drain_kw += 1.4
        if st["sentry_on"]:
            drain_kw += 0.3
        st["energy_kwh"] = max(0.0, st["energy_kwh"] - drain_kw * dt / 3600.0)
        # cabin temperature
        if st["climate_on"]:
            diff = st["target_temp_f"] - st["cabin_temp_f"]
            move = max(-2.0 * dt / 60.0, min(2.0 * dt / 60.0, diff))
            st["cabin_temp_f"] += move
        else:
            tau = 90.0 * 60.0
            st["cabin_temp_f"] += (outside - st["cabin_temp_f"]) * (1 - math.exp(-dt / tau))

    def _apply_event(self, st: Dict[str, Any], kind: str) -> None:
        if kind == "door_ajar" and st["zone"] == "home":
            st["doors"][random.Random(st["created"]).choice(DOORS)] = True
        elif kind == "window_open":
            st["windows"][random.Random(st["created"] + 1).choice(WINDOWS)] = True
        elif kind == "charge_interrupted" and st["plugged_in"]:
            st["charging_enabled"] = False
        elif kind == "software_update":
            st["update_available"] = True
        st["asleep"] = False

    def _charging_state(self, st: Dict[str, Any]) -> str:
        if not st["plugged_in"]:
            return "disconnected"
        limit_kwh = st["battery_kwh"] * st["charge_limit"] / 100.0
        if not st["charging_enabled"]:
            return "stopped"
        if st["energy_kwh"] >= limit_kwh - 0.05:
            return "complete"
        return "charging"

    # -- state ----------------------------------------------------------------------------
    def fetch_state(self, now: float) -> VehicleState:
        with self._lock:
            st = self._load()
            sim_now = self._wall() + float(st["offset_s"])
            self._tick(st, sim_now)
            self._save()
            return self._to_vehicle_state(st, sim_now)

    def _to_vehicle_state(self, st: Dict[str, Any], sim_now: float) -> VehicleState:
        report = st["last_report"]
        cap = st["battery_kwh"]
        pct = st["energy_kwh"] / cap * 100.0
        cs = self._charging_state(st)
        charging = cs == "charging"
        power = st["voltage"] * st["charge_amps"] / 1000.0 * (0.6 if pct > 90 else 1.0) if charging else 0.0
        eta = None
        if charging:
            need = cap * st["charge_limit"] / 100.0 - st["energy_kwh"]
            eta = round(max(0.0, need / max(0.1, power * CHARGE_EFFICIENCY)) * 60.0, 1)
        outside = self._outside_f(st, sim_now)
        open_doors = [d for d, o in st["doors"].items() if o]
        open_windows = [w for w, o in st["windows"].items() if o]
        vals: Dict[str, Any] = {
            "battery_pct": round(pct, 1),
            "range_mi": round(st["energy_kwh"] * 1000.0 / EFFICIENCY_WH_MI * 0.97, 1),
            "charging_state": cs,
            "plugged_in": st["plugged_in"],
            "charge_power_kw": round(power, 2),
            "charge_limit_pct": st["charge_limit"],
            "charge_eta_min": eta if eta is not None else 0,
            "charge_current_a": st["charge_amps"] if charging else 0,
            "charge_current_max_a": st["charge_amps_max"],
            "charger_voltage_v": st["voltage"] if st["plugged_in"] else 0,
            "charge_port_open": st["plugged_in"],
            "locked": st["locked"],
            "doors_open": open_doors,
            "windows_open": open_windows,
            "trunk_open": st["trunk_open"],
            "frunk_open": st["frunk_open"],
            "sentry_on": st["sentry_on"],
            "user_present": False,
            "climate_on": st["climate_on"],
            "cabin_temp_f": round(st["cabin_temp_f"], 1),
            "outside_temp_f": round(outside, 1),
            "target_temp_f": st["target_temp_f"],
            "defrost_on": st["defrost_on"],
            "location_zone": st["zone"],
            "shift_state": "P",
            "speed_mph": 0.0,
            "moving": False,
            "asleep": st["asleep"],
            "software_update_available": st["update_available"],
            "software_version": st["sw_version"],
            "odometer_mi": round(st["odometer_mi"], 1),
        }
        fields = {name: FieldValue(vals[name], report if name != "asleep" else sim_now) for name in vals}
        for name in FIELD_NAMES:
            fields.setdefault(name, FieldValue(None, None, False))
        return VehicleState(name=self._name, source=self.kind, captured_at=sim_now, fields=fields, simulated=True)

    def capabilities(self) -> Capabilities:
        return Capabilities(frozenset(FIELD_NAMES), frozenset(COMMANDS), history=True,
                            notes="Simulated car: state and commands are fake but behave realistically.")

    # -- commands -------------------------------------------------------------------------
    def send_command(self, command: str, args: Mapping[str, Any]) -> CommandResult:
        with self._lock:
            st = self._load()
            sim_now = self._wall() + float(st["offset_s"])
            self._tick(st, sim_now)
            st["asleep"] = False  # any command wakes the (simulated) car
            st["last_report"] = sim_now
            detail = self._apply_command(st, command, args)
            self._save()
            return CommandResult(accepted=True, detail=detail)

    def _apply_command(self, st: Dict[str, Any], command: str, args: Mapping[str, Any]) -> str:
        if command == "climate_on":
            st["climate_on"] = True
            if "temp_f" in args:
                st["target_temp_f"] = float(args["temp_f"])
        elif command == "climate_off":
            st["climate_on"] = False
        elif command == "set_temp":
            st["target_temp_f"] = float(args["temp_f"])
        elif command == "defrost":
            st["defrost_on"] = bool(args.get("on", True))
            if st["defrost_on"]:
                st["climate_on"] = True
        elif command == "seat_heat":
            st["seats"][args["seat"]] = int(args["level"])
        elif command == "charge_limit":
            st["charge_limit"] = int(args["percent"])
        elif command == "charge_start":
            if not st["plugged_in"]:
                raise AdapterError("the car is not plugged in")
            st["charging_enabled"] = True
        elif command == "charge_stop":
            st["charging_enabled"] = False
        elif command == "charge_amps":
            st["charge_amps"] = min(int(args["amps"]), st["charge_amps_max"])
        elif command == "lock":
            st["locked"] = True
        elif command == "unlock":
            st["locked"] = False
        elif command == "open_trunk":
            st["trunk_open"] = True
        elif command == "open_frunk":
            st["frunk_open"] = True
        elif command == "vent_windows":
            st["windows"] = {w: True for w in WINDOWS}
        elif command == "close_windows":
            st["windows"] = {w: False for w in WINDOWS}
        elif command == "sentry_on":
            st["sentry_on"] = True
        elif command == "sentry_off":
            st["sentry_on"] = False
        elif command in ("honk", "flash"):
            return f"simulated {command}"
        else:
            raise AdapterError(f"simulator does not implement '{command}'")
        return "simulated command applied"

    # -- demo / test controls ---------------------------------------------------------------
    def control(self, action: str, **kw: Any) -> Dict[str, Any]:
        """Scenario controls for demos and tests (also exposed in the Desktop page)."""
        with self._lock:
            st = self._load()
            sim_now = self._wall() + float(st["offset_s"])
            self._tick(st, sim_now)
            note = self._control(st, action, kw)
            if action != "sleep":
                st["asleep"] = False
                st["last_report"] = st["last_tick"]
            self._save()
            return {"ok": True, "action": action, "note": note, "sim_time": self._wall() + float(st["offset_s"])}

    def _control(self, st: Dict[str, Any], action: str, kw: Mapping[str, Any]) -> str:
        if action == "advance":
            minutes = float(kw.get("minutes", 30))
            if minutes <= 0 or minutes > 60 * 24 * 7:
                raise AdapterError("advance minutes must be between 0 and 10080")
            st["offset_s"] += minutes * 60.0
            sim_now = self._wall() + float(st["offset_s"])
            self._tick(st, sim_now)
            return f"fast-forwarded {minutes:g} minutes"
        if action == "drive":
            miles = float(kw.get("miles", 10))
            dest = str(kw.get("to", "away")).lower()
            temp = self._outside_f(st, st["last_tick"])
            factor = 1.0 + max(0.0, (50.0 - temp)) * 0.006
            kwh = miles * EFFICIENCY_WH_MI * factor / 1000.0
            st["energy_kwh"] = max(0.0, st["energy_kwh"] - kwh)
            st["odometer_mi"] += miles
            st["zone"] = dest
            st["plugged_in"] = False  # you unplug to drive; plug back in with the plug_in control
            st["charging_enabled"] = True
            st["trips"] = (st["trips"] + [{"at": st["last_tick"], "miles": miles, "kwh": round(kwh, 2)}])[-20:]
            return f"drove {miles:g} mi to {dest}"
        if action == "park_home":
            st["zone"] = "home"
            return "parked at home"
        if action == "plug_in":
            st["plugged_in"] = True
            st["charging_enabled"] = True
            return "plugged in"
        if action == "unplug":
            st["plugged_in"] = False
            return "unplugged"
        if action == "leave_unlocked":
            st["locked"] = False
            return "left the car unlocked"
        if action == "open_door":
            st["doors"][str(kw.get("door", "rear_driver"))] = True
            return "a door is open"
        if action == "close_doors":
            st["doors"] = {d: False for d in DOORS}
            st["windows"] = {w: False for w in WINDOWS}
            st["trunk_open"] = False
            st["frunk_open"] = False
            return "all closed"
        if action == "event":
            kind = str(kw.get("kind", "door_ajar"))
            if kind not in EVENT_KINDS:
                raise AdapterError(f"unknown event '{kind}'")
            self._apply_event(st, kind)
            return f"event: {kind}"
        if action == "cold":
            st["cold_override_f"] = float(kw.get("temp_f", 18))
            return f"outside temperature pinned to {st['cold_override_f']:g}°F"
        if action == "normal_weather":
            st["cold_override_f"] = None
            return "weather back to normal"
        if action == "set_soc":
            st["energy_kwh"] = st["battery_kwh"] * max(1.0, min(100.0, float(kw.get("percent", 40)))) / 100.0
            return f"battery set to {float(kw.get('percent', 40)):g}%"
        if action == "sleep":
            if self._charging_state(st) == "charging":
                raise AdapterError("a charging car stays awake; stop charging or unplug first")
            st["asleep"] = True
            return "the car fell asleep (state is no longer updating)"
        if action == "reset":
            fresh = _default_state(self._seed, self._wall())
            if self._events_enabled:
                _schedule_events(fresh, random.Random(self._seed * 7919 + 13), self._wall())
            st.clear()
            st.update(fresh)
            return "simulator reset"
        raise AdapterError(f"unknown simulator action '{action}'")


class SimulatorHistory(HistoryProvider):
    """Seeded synthetic history (60 days) so history and efficiency features work in simulator mode."""

    kind = "simulator"
    label = "Simulated history"

    def __init__(self, seed: int = 42, days: int = 60, now: Callable[[], float] = time.time):
        self._seed = int(seed)
        self._days = int(days)
        self._now = now

    def _generate(self) -> tuple:
        rng = random.Random(self._seed * 31 + 5)
        end_day = datetime.fromtimestamp(self._now()).replace(hour=0, minute=0, second=0, microsecond=0)
        drives: List[DriveRecord] = []
        charges: List[ChargeRecord] = []
        soc = 70.0
        for d in range(self._days, 0, -1):
            day = end_day - timedelta(days=d)
            temp_base = 48 + 14 * math.sin((day.timetuple().tm_yday - 100) / 365 * 2 * math.pi) + rng.uniform(-6, 6)
            n_trips = 0 if day.weekday() == 6 and rng.random() < 0.3 else rng.randint(1, 3)
            for i in range(n_trips):
                start = day + timedelta(hours=7 + i * 4 + rng.uniform(0, 1.5))
                miles = round(rng.choice([4, 6, 9, 12, 18, 25, 38]) * rng.uniform(0.8, 1.2), 1)
                temp = temp_base + rng.uniform(-3, 3)
                eff = 255 + max(0.0, 55 - temp) * 2.4 + rng.uniform(-18, 18)
                kwh = miles * eff / 1000.0
                dur = miles / rng.uniform(22, 42) * 60
                s0 = soc
                soc = max(8.0, soc - kwh / 75.0 * 100.0)
                drives.append(DriveRecord(
                    start=start.timestamp(), end=(start + timedelta(minutes=dur)).timestamp(), miles=miles,
                    duration_min=round(dur, 1), start_pct=round(s0, 1), end_pct=round(soc, 1),
                    kwh=round(kwh, 2), wh_per_mi=round(eff, 1), outside_temp_f=round(temp, 1),
                    speed_avg_mph=round(miles / dur * 60, 1), speed_max_mph=round(rng.uniform(35, 72), 1)))
            if soc < 62 or rng.random() < 0.2:
                start = day + timedelta(hours=21, minutes=rng.randint(0, 90))
                target = rng.choice([78, 80, 80, 82])
                add_kwh = max(0.5, (target - soc) / 100 * 75)
                dur = add_kwh / 6.9 * 60
                charges.append(ChargeRecord(
                    start=start.timestamp(), end=(start + timedelta(minutes=dur)).timestamp(),
                    kwh_added=round(add_kwh, 2), kwh_used=round(add_kwh / 0.9, 2),
                    cost=round(add_kwh / 0.9 * 0.11, 2), start_pct=round(soc, 1), end_pct=float(target),
                    duration_min=round(dur, 1), outside_temp_f=round(temp_base - 4, 1)))
                soc = float(target)
        return drives, charges

    def drives(self, start: float, end: float, limit: int = 500) -> List[DriveRecord]:
        drives, _ = self._generate()
        return [d for d in drives if start <= d.start <= end][-limit:]

    def charges(self, start: float, end: float, limit: int = 500) -> List[ChargeRecord]:
        _, charges = self._generate()
        return [c for c in charges if start <= c.start <= end][-limit:]

    def battery_health(self) -> Optional[BatteryHealth]:
        return BatteryHealth(max_capacity_kwh=77.0, current_capacity_kwh=75.0, health_pct=97.4,
                             max_range_mi=300.0, current_range_mi=292.0)


def build_simulator(ctx: BuildContext) -> SimulatorAdapter:
    s = ctx.settings
    return SimulatorAdapter(store=ctx.store, seed=int(s.get("sim_seed", 42)),
                            events_enabled=bool(s.get("sim_events", True)),
                            name=(s.get("car_name") or "Simulated car"))


def build_simulator_history(ctx: BuildContext) -> SimulatorHistory:
    return SimulatorHistory(seed=int(ctx.settings.get("sim_seed", 42)))
