"""Tool schemas — what the model reads to decide when and how to call each tool."""

from __future__ import annotations

CAR_STATUS = {
    "name": "car_status",
    "description": (
        "Get the car's live state: battery %, range (mi), charging status/power/limit/ETA, plugged in, "
        "locked, doors/windows/trunk/frunk open, Sentry, cabin and outside temperature (°F), location "
        "ZONE (home/away/named zone — never coordinates), software update, odometer. Every value has a "
        "freshness age; fields the car/adapter cannot provide are listed as unavailable — never guess them. "
        "Use this first for any question about the car's current condition. It is cheap and never wakes "
        "a sleeping car. If the car is a simulation the result says so (`vehicle.simulated`)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "refresh": {"type": "boolean", "description": "Force a fresh read instead of a few-seconds-old cache."},
        },
        "required": [],
    },
}

CAR_HISTORY = {
    "name": "car_history",
    "description": (
        "Look back at the car's trips, charging sessions, efficiency and battery health over a date range. "
        "All aggregates (miles, kWh, Wh/mi, cost, efficiency by temperature, week-over-week trend) are "
        "computed in code from the car's history — quote them, don't recompute. Use for questions like "
        "'how many miles did I drive last month', 'what does charging cost me', 'how is efficiency in the "
        "cold', 'how healthy is the battery'. Uses the history Pit Crew records itself (TeslaMate import is optional); if it only recently started recording, the result says since when."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["summary", "trips", "charges", "efficiency", "battery"],
                     "description": "summary = everything aggregated (default); trips/charges list recent items too."},
            "days": {"type": "integer", "description": "Look back this many days (default 30). Ignored if start_date is given."},
            "start_date": {"type": "string", "description": "ISO date (YYYY-MM-DD) to start from."},
            "end_date": {"type": "string", "description": "ISO date to end at (default today)."},
        },
        "required": [],
    },
}

CAR_READINESS = {
    "name": "car_readiness",
    "description": (
        "Answer 'will the car be ready for X?' for a departure time and trip distance. Computes the battery % "
        "needed (trip energy from the car's own efficiency history at the expected temperature, a 15% safety "
        "margin and a reserve), the % expected at departure given current charging, and whether charging "
        "can finish in time. Returns a verdict (ready / tight / not_ready) plus concrete recommendations such "
        "as raising the charge limit, starting charging, or preconditioning at T-minus. If the user has a "
        "calendar tool, look up their first appointment and its distance FIRST, then call this with that "
        "departure time and miles; pass `expected_temp_f` if you have a forecast. Recommendations are "
        "suggestions — to act on one, call car_command (which has its own safety checks)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "departure": {"type": "string", "description": "When the user needs to leave, e.g. 'tomorrow 7:30 AM', 'Mon 8am' or an ISO date-time. Defaults to the configured usual departure time."},
            "trip_miles": {"type": "number", "description": "One-way distance in miles (default: the configured typical trip)."},
            "round_trip": {"type": "boolean", "description": "True if the car must also get back (or the destination has no charging)."},
            "expected_temp_f": {"type": "number", "description": "Expected outside temperature in °F during the trip, from a forecast (default: the car's current outside temperature)."},
        },
        "required": [],
    },
}


def _command_help() -> str:
    from .brain.commands import COMMANDS

    groups = {}
    for spec in COMMANDS.values():
        tag = spec.risk + ("" if spec.safe_direction or not spec.requires_approval else ", NEEDS HUMAN APPROVAL")
        groups.setdefault(tag, []).append(spec.name)
    return "; ".join(f"{tag}: {', '.join(names)}" for tag, names in groups.items())


CAR_COMMAND = {
    "name": "car_command",
    "description": (
        "Send ONE command to the car through the safety gate. Commands — " + _command_help() + ". "
        "Arguments (all bounded): set_temp/climate_on {temp_f 59-82}; charge_limit {percent 50-100}; "
        "charge_amps {amps}; defrost {on} (Tesla defrost runs the heat and the fan at MAXIMUM: do not turn it on with anyone or any pet in the car, and say so to the user before you do); seat_heat {seat: driver|passenger|rear_left|rear_center|rear_right, "
        "level 0-3}. Unless the user set live mode to 'always', the car starts in PRACTICE mode: commands are validated and "
        "logged but NOT sent until the user goes live (`/car live`, or Go live on the Pit Crew page); `/car stop` pauses either "
        "mode. The result says which happened, so say it plainly (\"practice run, nothing was sent\"). You cannot go live or "
        "change Pit Crew's safety settings yourself; tell the user the setting a refusal names. Commands marked NEEDS HUMAN APPROVAL go through Hermes' approval gate under the user's own approval setting (Hermes asks them, or not if their approvals are off); they are refused when "
        "nobody can answer (scheduled/unattended runs) and are refused while the car is moving, when its data is "
        "stale, or (unlock/trunk/frunk) when it is away from a trusted zone unless the user allows them anywhere. Never claim a command worked "
        "unless the result's status is `confirmed` (the car's own state showed the change); `sent_unconfirmed` "
        "means sent but NOT verified — say so, and re-check with car_status. Never retry a failed or blocked "
        "command in a loop. Never wake a sleeping car for something the user did not ask for; if a result says waking needs "
        "their OK, ask them first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command name."},
            "args": {"type": "object", "description": "Command arguments, e.g. {\"temp_f\": 70}. Policy flags: "
                     "override_zone=true (only if the user insists on unlocking away from a trusted place), "
                     "wake_ok=true (only after the user agreed to wake a sleeping car)."},
            "reason": {"type": "string", "description": "Short reason / the user's request, shown in the approval prompt and audit log."},
        },
        "required": ["command"],
    },
}

CAR_ALERTS = {
    "name": "car_alerts",
    "description": (
        "Read or manage the car watcher's alerts (left unlocked away from home, window/door/trunk open, plugged in but "
        "not charging, charging stopped, not enough charge for tomorrow, Sentry off away, cabin too hot/cold with someone "
        "inside, software update). Actions: `list` (default) shows open alerts; `ack` acknowledges one alert by id (or "
        "`all`) so it stops nagging; `check_now` polls the car and evaluates every rule immediately — use it in scheduled "
        "security checks and when the user asks 'is anything wrong with the car?'. Alerts are plain-language with one "
        "suggested action; offer that action to the user, don't perform it without their yes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "ack", "check_now"], "description": "What to do (default list)."},
            "alert_id": {"type": "string", "description": "Alert id for `ack`, or `all`."},
            "include_resolved": {"type": "boolean", "description": "With `list`, also show recently resolved alerts."},
        },
        "required": [],
    },
}

CAR_RULES = {
    "name": "car_rules",
    "description": (
        "List, enable/disable or tune the car watcher's rules (e.g. change 'left unlocked away' from 10 to 20 minutes, "
        "or turn off the software-update notice). Actions: `list` (default) shows every rule with its thresholds and "
        "whether this car can support it; `enable` / `disable` toggle a rule; `configure` changes its numeric thresholds "
        "(pass them in `settings`). Changes persist. Only change rules when the user asks."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "enable", "disable", "configure"], "description": "What to do (default list)."},
            "rule": {"type": "string", "description": "Rule id, e.g. left_unlocked_away."},
            "settings": {"type": "object", "description": "For `configure`: threshold names and new values, e.g. {\"minutes\": 20}."},
        },
        "required": [],
    },
}
