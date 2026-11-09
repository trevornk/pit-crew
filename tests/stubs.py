"""A scriptable adapter for safety/policy/watcher tests: you set the car's state directly."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from pit_crew.adapters.base import AdapterError, CarAdapter, CommandResult
from pit_crew.brain.commands import COMMANDS, Capabilities
from pit_crew.brain.models import FIELD_NAMES, FieldValue, VehicleState

BASE: Dict[str, Any] = dict(
    battery_pct=70.0, range_mi=200.0, charging_state="complete", plugged_in=True, charge_power_kw=0.0,
    charge_limit_pct=80, charge_eta_min=0, charge_current_a=0, charge_current_max_a=32, charger_voltage_v=240,
    locked=True, doors_open=[], windows_open=[], trunk_open=False, frunk_open=False, sentry_on=True,
    user_present=False, climate_on=False, cabin_temp_f=68.0, outside_temp_f=60.0, target_temp_f=70.0,
    defrost_on=False, location_zone="home", shift_state="P", speed_mph=0.0, moving=False, asleep=False,
    software_update_available=False, software_version="1.0", odometer_mi=1000.0)


class StubAdapter(CarAdapter):
    kind = "stub"
    label = "Stub car"
    simulated = False

    def __init__(self, clock):
        self._clock = clock
        self.values: Dict[str, Any] = dict(BASE)
        self.ages: Dict[str, float] = {}      # per-field age in seconds (default 0 = fresh)
        self.sent: List[Tuple[str, Dict[str, Any]]] = []
        self.apply_commands = True            # False: car ignores commands (read-back must time out)
        self.fail_with: Optional[str] = None
        self.removed_fields: set = set()
        self.commands = frozenset(COMMANDS)
        self.hints: Dict[str, Any] = {}         # VehicleState.hints (model defaults, e.g. tire_min_psi)

    def capabilities(self) -> Capabilities:
        return Capabilities(frozenset(FIELD_NAMES) - self.removed_fields, self.commands)

    def fetch_state(self, now: float) -> VehicleState:
        fields = {}
        for name in FIELD_NAMES:
            if name in self.values and name not in self.removed_fields:
                fields[name] = FieldValue(self.values[name], now - self.ages.get(name, self.ages.get("*", 0.0)))
            else:
                fields[name] = FieldValue(None, None, False)
        return VehicleState(name="Stub", source="stub", captured_at=now, fields=fields, hints=dict(self.hints))

    def send_command(self, command: str, args: Mapping[str, Any]) -> CommandResult:
        if self.fail_with:
            raise AdapterError(self.fail_with)
        self.sent.append((command, dict(args)))
        if self.apply_commands:
            v = self.values
            if command == "lock":
                v["locked"] = True
            elif command == "unlock":
                v["locked"] = False
            elif command == "open_frunk":
                v["frunk_open"] = True
            elif command == "open_trunk":
                v["trunk_open"] = True
            elif command == "vent_windows":
                v["windows_open"] = ["front_driver"]
            elif command == "close_windows":
                v["windows_open"] = []
            elif command == "sentry_off":
                v["sentry_on"] = False
            elif command == "sentry_on":
                v["sentry_on"] = True
            elif command == "set_temp":
                v["target_temp_f"] = args["temp_f"]
            elif command == "climate_on":
                v["climate_on"] = True
                if "temp_f" in args:
                    v["target_temp_f"] = args["temp_f"]
            elif command == "climate_off":
                v["climate_on"] = False
            elif command == "charge_limit":
                v["charge_limit_pct"] = args["percent"]
            elif command == "charge_start":
                v["charging_state"] = "charging"
            elif command == "charge_stop":
                v["charging_state"] = "stopped"
        return CommandResult(True, "stub accepted")
