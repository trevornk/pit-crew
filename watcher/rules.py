"""Watch rules: pure functions from (state, settings, clock) to *findings*.

A rule says "this condition is true right now"; the engine (``engine.py``) handles how long it must
persist (``hold_s``), de-duplication, cooldowns and resolution. Every rule is individually
toggleable and its thresholds are configurable (``rules`` setting, `car_rules` tool, `/car rules`,
Desktop page). Alert text is plain language with exactly one suggested action.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from ..brain.models import ACTIVELY_CHARGING, VehicleState
from ..brain import tires
from ..brain.timeparse import fmt_clock
from ..settings import parse_hhmm, zones_list


@dataclass
class Finding:
    key: str
    title: str
    body: str
    suggestion: str
    hold_s: float = 0.0
    severity: str = "medium"
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RuleContext:
    state: VehicleState
    now: float
    local: datetime
    cfg: Mapping[str, Any]            # this rule's effective config
    settings: Mapping[str, Any]       # global plugin settings snapshot
    can_do: frozenset
    memo: Dict[str, Any]              # this rule's persistent scratch memory (engine saves it between polls)
    readiness: Optional[Callable[..., Dict[str, Any]]] = None
    last_moving_ts: Optional[float] = None   # when the car was last seen moving (the engine keeps it across polls)

    @property
    def trusted(self) -> List[str]:
        return zones_list(self.settings.get("trusted_zones")) or ["home"]

    def at_trusted(self) -> Optional[bool]:
        zone = self.state.at_zone()
        if zone is None:
            return None
        return zone in self.trusted

    def can(self, command: str) -> bool:
        return command in self.can_do

    def offer(self, command: str, text: str) -> str:
        """The single suggested action, phrased as an offer only if the car supports the command."""
        return text if self.can(command) else "Check on it when you can."


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    description: str
    defaults: Mapping[str, Any]
    requires: Tuple[str, ...]
    check: Callable[[RuleContext], List[Finding]]
    #: the rule can run when at least one of these is available (e.g. any one of four tire sensors)
    requires_any: Tuple[str, ...] = ()


def _mins(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m} min" if m < 90 else f"{m / 60:.1f} h"


def _in_night(local: datetime, start: str, end: str) -> bool:
    sh, sm = parse_hhmm(start, (22, 0))
    eh, em = parse_hhmm(end, (6, 0))
    cur = local.hour * 60 + local.minute
    a, b = sh * 60 + sm, eh * 60 + em
    return cur >= a or cur < b if a > b else a <= cur < b


# friendly names for rule settings (used by the pages and /car rules)
RULE_LABELS = {"minutes": "after (min)", "cooldown_min": "don't repeat for (min)", "hot_f": "hotter than (°F)",
               "cold_f": "colder than (°F)", "below_pct": "below (%)", "trip_mi": "trip (mi)", "within_hours": "within (h)",
               "min_psi": "low below (psi, 0 = auto)", "drop_psi": "loses at least (psi)", "window_min": "within (min)"}

#: The smallest sensible value of a numeric rule setting. Below these a rule misfires (a 0 psi drop is "any two samples",
#: a 0 minute window has no samples, a 0 mi trip needs no charge); set_rule refuses them and rule_config raises a
#: configured value to the floor.
RULE_MINIMUMS: Dict[str, float] = {"drop_psi": 0.5, "window_min": 1, "trip_mi": 1, "within_hours": 1, "below_pct": 1}

_SAFE = re.compile(r"[^A-Za-z0-9 _-]")


def _place(zone: Any) -> str:
    """Words for where the car is, safe to put in an alert the model will read: a zone name comes from Home
    Assistant state, so keep it to plain characters and a short length."""
    z = _SAFE.sub("", str(zone or "")).strip()[:40]
    if not z or z.lower() in ("away", "not_home", "not home"):
        return "away from home"
    if z.lower() in ("unknown", "unavailable", "none"):
        return "at an unknown place"
    return f"at {z}"


# ---------------------------------------------------------------------------------------------
def _left_unlocked_away(c: RuleContext) -> List[Finding]:
    st = c.state
    if st.motion() is True:
        return []                      # a car being driven is unlocked and away on purpose
    if st.value("locked") is False and c.at_trusted() is False:
        mins = float(c.cfg["minutes"])
        zone = st.at_zone()
        return [Finding("unlocked_away", "Your car is unlocked and away from home",
                        f"Still unlocked at {fmt_clock(c.local)}, parked {_place(zone)} (it stayed that way for at least {mins:g} min).",
                        c.offer("lock", "Want me to lock it? (lock needs no approval)"),
                        hold_s=mins * 60, severity="high")]
    return []


def _unlocked_at_home_night(c: RuleContext) -> List[Finding]:
    st = c.state
    if st.value("locked") is False and c.at_trusted() is True and _in_night(c.local, c.settings["night_start"], c.settings["night_end"]):
        mins = float(c.cfg["minutes"])
        return [Finding("unlocked_home_night", "Your car is unlocked overnight",
                        f"Still unlocked at {fmt_clock(c.local)}, parked at home overnight.",
                        c.offer("lock", "Want me to lock it?"), hold_s=mins * 60, severity="medium")]
    return []


def _opening_left_open(c: RuleContext) -> List[Finding]:
    st = c.state
    if st.motion() is True:
        return []
    items: List[str] = []
    doors = st.value("doors_open") or []
    wins = st.value("windows_open") or []
    if isinstance(doors, list) and doors:
        items.append("door(s): " + ", ".join(d.replace("_", " ") for d in doors))
    elif doors is True:
        items.append("a door")
    if isinstance(wins, list) and wins:
        items.append("window(s): " + ", ".join(w.replace("_", " ") for w in wins))
    elif wins is True:
        items.append("a window")
    if st.value("trunk_open"):
        items.append("the trunk")
    if st.value("frunk_open"):
        items.append("the frunk")
    if not items:
        return []
    mins = float(c.cfg["minutes"])
    only_windows = bool(wins) and not doors and not st.value("trunk_open") and not st.value("frunk_open")
    suggestion = (c.offer("close_windows", "Want me to close the windows?") if only_windows
                  else "Walk out and close it — I can't close doors or the trunk for you.")
    return [Finding("open:" + "|".join(items), "Something on your car is open",
                    "Left open: " + "; ".join(items) + f". Seen at {fmt_clock(c.local)}, still that way after {mins:g} min.",
                    suggestion, hold_s=mins * 60, severity="medium")]


def _plugged_not_charging(c: RuleContext) -> List[Finding]:
    st = c.state
    soc, limit = st.value("battery_pct"), st.value("charge_limit_pct")
    if (st.value("plugged_in") is True and st.value("charging_state") in ("stopped", "no_power")
            and c.at_trusted() is True and isinstance(soc, (int, float))
            and soc < float(c.cfg["below_pct"]) and (limit is None or soc < float(limit) - 3)):
        mins = float(c.cfg["minutes"])
        why = "the charger has no power" if st.value("charging_state") == "no_power" else "charging is stopped"
        return [Finding("plugged_not_charging", "Plugged in but not charging",
                        f"At {soc:.0f}% and plugged in at home, but {why} (seen at {fmt_clock(c.local)}).",
                        c.offer("charge_start", "Want me to start charging?") if st.value("charging_state") == "stopped"
                        else "Check the outlet or breaker.",
                        hold_s=mins * 60, severity="medium")]
    return []


def _charging_stopped_unexpectedly(c: RuleContext) -> List[Finding]:
    st = c.state
    soc, limit = st.value("battery_pct"), st.value("charge_limit_pct")
    now_state = st.value("charging_state")
    if now_state in ACTIVELY_CHARGING:
        c.memo["was_charging"] = True
    elif now_state in ("complete", "disconnected"):
        c.memo["was_charging"] = False
    stopped = now_state in ("stopped", "no_power") and st.value("plugged_in") is True
    if (c.memo.get("was_charging") and stopped and isinstance(soc, (int, float))
            and (limit is None or soc < float(limit) - 2)):
        return [Finding("charge_interrupted", "Charging stopped before it finished",
                        f"The car was charging but stopped at {soc:.0f}% (limit {limit if limit is not None else '?'}%)"
                        + (" — the charger lost power." if now_state == "no_power" else "."),
                        c.offer("charge_start", "Want me to restart charging?") if now_state == "stopped"
                        else "Check the outlet or breaker.", severity="high")]
    return []


def _soc_below_tomorrow(c: RuleContext) -> List[Finding]:
    if c.readiness is None:
        return []
    res = c.readiness(None, float(c.cfg["trip_mi"]))
    if not res.get("ok") or res.get("verdict") != "not_ready":
        return []
    hrs = res.get("hours_until_departure", 99)
    if hrs > float(c.cfg["within_hours"]) or hrs < 0.5:
        return []
    rec = res["recommendations"][0] if res.get("recommendations") else None
    sug = {"raise_charge_limit": "Raise the charge limit?", "start_charging": "Start charging?",
           "plug_in": "Plug the car in.", "charge_elsewhere_or_leave_later": "Charge faster, or leave later.",
           "plan_charging_stop": "Plan a charging stop, or shorten the trip."}.get(
        rec["action"] if rec else "", "Check the charging plan.")
    return [Finding("tomorrow:" + str(res["departure"])[:10], "The car may not be ready for tomorrow",
                    res["summary"], sug, severity="high", data={"readiness": {k: res[k] for k in
                                                                              ("verdict", "needed_pct", "projected_at_departure_pct")}})]


def _sentry_off_away(c: RuleContext) -> List[Finding]:
    st = c.state
    if st.motion() is True:
        return []                      # Sentry is off while driving; the rule is about a PARKED car
    if st.value("sentry_on") is False and c.at_trusted() is False:
        mins = float(c.cfg["minutes"])
        return [Finding("sentry_off_away", "Sentry mode is off away from home",
                        f"Seen parked {_place(st.at_zone())} with Sentry mode off at {fmt_clock(c.local)}.",
                        c.offer("sentry_on", "Want me to turn Sentry on? (no approval needed)"),
                        hold_s=mins * 60, severity="low")]
    return []


def _cabin_extreme(c: RuleContext) -> List[Finding]:
    st = c.state
    temp = st.value("cabin_temp_f")
    if st.value("user_present") is True and isinstance(temp, (int, float)):
        hot, cold = float(c.cfg["hot_f"]), float(c.cfg["cold_f"])
        if temp >= hot or temp <= cold:
            word = "dangerously hot" if temp >= hot else "very cold"
            return [Finding("cabin_extreme", f"The car's cabin is {word} with someone inside",
                            f"The cabin was {temp:.0f}°F at {fmt_clock(c.local)} and the car reported someone in it.",
                            c.offer("climate_on", "Want me to start the climate control?"), severity="high")]
    return []


def _software_update(c: RuleContext) -> List[Finding]:
    if c.state.value("software_update_available") is True:
        return [Finding("update", "A software update is available",
                        "The car has a new software version ready to install.",
                        "Schedule it from the car or the Tesla/maker app when convenient.", severity="info")]
    return []


def _tire_low(c: RuleContext) -> List[Finding]:
    stale_after = float(c.settings.get("stale_after_s") or 900)
    low = tires.low_tires(c.state, c.now, stale_after, c.cfg.get("min_psi"))
    if not low:
        return []
    mins = float(c.cfg["minutes"])
    worst = min(low, key=lambda t: t[1] / t[2])
    parts = ", ".join(f"{pos} {psi:.0f} psi" for pos, psi, _ in low)
    key = "tire_low:" + "|".join(sorted(pos.replace(" ", "_") for pos, _, _ in low))
    return [Finding(key, "A tire is low on air",
                    f"Low tire pressure: {parts} (below {worst[2]:g} psi), seen at {fmt_clock(c.local)}.",
                    "Add air soon (or look the tire over); I can't do that from here.",
                    hold_s=mins * 60, severity="high" if worst[1] < worst[2] * 0.8 else "medium",
                    data={"tires": {pos: round(psi, 1) for pos, psi, _ in low}})]


def _tire_car_warning(c: RuleContext) -> List[Finding]:
    stale_after = float(c.settings.get("stale_after_s") or 900)
    warned = tires.car_warnings(c.state, c.now, stale_after)
    if not warned:
        return []
    key = "tire_warn:" + "|".join(sorted(p.replace(" ", "_") for p in warned))
    return [Finding(key, "The car is warning about a tire",
                    f"The car's own tire-pressure warning is on for: {', '.join(warned)} (seen at {fmt_clock(c.local)}).",
                    "Check that tire's pressure and condition before a long drive; I can't add air from here.",
                    hold_s=float(c.cfg["minutes"]) * 60, severity="high", data={"tires": warned})]


def _tire_fast_loss(c: RuleContext) -> List[Finding]:
    window_s = float(c.cfg["window_min"]) * 60.0
    stale_after = float(c.settings.get("stale_after_s") or 900)
    tires.track(c.memo, c.state, c.now, window_s, c.last_moving_ts, stale_after)
    lost = tires.fast_losses(c.memo, c.now, window_s, float(c.cfg["drop_psi"]), stale_after)
    if not lost:
        return []
    parts = ", ".join(f"{pos} fell {drop:g} psi ({start:g} to {end:g})" for pos, drop, start, end in lost)
    key = "tire_loss:" + "|".join(sorted(pos.replace(" ", "_") for pos, *_ in lost))
    return [Finding(key, "A tire is losing air fast",
                    f"{parts.capitalize()} within {c.cfg['window_min']:g} min, seen at {fmt_clock(c.local)}.",
                    "Check that tire before you drive: it may have a puncture or a leaking valve.",
                    severity="high", data={"tires": {pos: end for pos, _, _, end in lost}})]


RULES: List[Rule] = [
    Rule("left_unlocked_away", "Left unlocked away from home",
         "Alert when the car has been unlocked outside a trusted zone for N minutes.",
         {"enabled": True, "minutes": 10, "cooldown_min": 60}, ("locked", "location_zone"), _left_unlocked_away),
    Rule("unlocked_at_home_night", "Unlocked at home at night",
         "Alert when the car is unlocked at home during the night window.",
         {"enabled": True, "minutes": 5, "cooldown_min": 240}, ("locked", "location_zone"), _unlocked_at_home_night),
    Rule("opening_left_open", "Window / door / trunk left open",
         "Alert when a door, window, trunk or frunk stays open while parked.",
         {"enabled": True, "minutes": 5, "cooldown_min": 120}, (), _opening_left_open),
    Rule("plugged_not_charging", "Plugged in but not charging",
         "Alert when the car is plugged in at home, below the threshold, and charging is stopped.",
         {"enabled": True, "minutes": 30, "below_pct": 60, "cooldown_min": 360},
         ("plugged_in", "charging_state", "battery_pct"), _plugged_not_charging),
    Rule("charging_stopped_unexpectedly", "Charging stopped unexpectedly",
         "Alert when charging stops by itself before reaching the limit.",
         {"enabled": True, "cooldown_min": 180}, ("charging_state", "battery_pct"), _charging_stopped_unexpectedly),
    Rule("soc_below_tomorrow", "Not enough charge for tomorrow",
         "Alert when the projected battery at your usual departure time is below what your usual trip needs.",
         {"enabled": True, "trip_mi": 30, "within_hours": 16, "cooldown_min": 240}, ("battery_pct",), _soc_below_tomorrow),
    Rule("sentry_off_away", "Sentry off away from home",
         "Alert when the car is away from a trusted zone with Sentry mode off.",
         {"enabled": True, "minutes": 10, "cooldown_min": 360}, ("sentry_on", "location_zone"), _sentry_off_away),
    Rule("cabin_extreme", "Cabin very hot or cold with someone inside",
         "Alert when the cabin is dangerously hot or cold and the car reports an occupant.",
         {"enabled": True, "hot_f": 100, "cold_f": 25, "cooldown_min": 30}, ("user_present", "cabin_temp_f"), _cabin_extreme),
    Rule("tire_pressure_low", "Low tire pressure",
         "Alert when a tire is below the low-pressure threshold (yours, or 90% of what the car recommends, "
         "or the model's default: 38 psi for a Tesla, otherwise 30 psi).",
         {"enabled": True, "min_psi": 0, "minutes": 10, "cooldown_min": 720}, (), _tire_low,
         requires_any=tuple(tires.TIRES)),
    Rule("tire_fast_loss", "Tire losing air fast",
         "Alert when a tire drops by N psi within a short window (a puncture or leaking valve).",
         {"enabled": True, "drop_psi": 3, "window_min": 30, "cooldown_min": 120}, (), _tire_fast_loss,
         requires_any=tuple(tires.TIRES)),
    Rule("tire_car_warning", "Car reports a tire warning",
         "Alert when the car's own tire-pressure warning (TPMS) is on for a tire.",
         {"enabled": True, "minutes": 5, "cooldown_min": 720}, (), _tire_car_warning,
         requires_any=tuple(tires.WARNINGS)),
    Rule("software_update", "Software update available",
         "Gentle heads-up when the car has a software update waiting.",
         {"enabled": True, "cooldown_min": 10080}, ("software_update_available",), _software_update),
]

RULES_BY_ID: Dict[str, Rule] = {r.id: r for r in RULES}
