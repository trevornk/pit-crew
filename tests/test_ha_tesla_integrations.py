"""Home Assistant's `teslemetry` and `tessie` integrations (profiles teslemetry_ha / tessie_ha).

Checked against fixtures derived from Home Assistant core's own source (entity names from strings.json, the
disabled-by-default flags and value functions from the platform files). They are NOT run on a real car and these
tests do not pretend otherwise."""

import copy
import json
import re
from pathlib import Path

import pytest

from fakes import FakeHA, load_fixture
from pit_crew.adapters.discovery import discover
from pit_crew.adapters.homeassistant import HomeAssistantAdapter, parse_ha_time
from pit_crew.adapters.profile import load_profile, validate_profile
from pit_crew.brain.commands import COMMANDS

FIX = Path(__file__).resolve().parent / "fixtures" / "integrations"
NOW = parse_ha_time("2026-02-01T10:05:00+00:00")
CASES = [("teslemetry_ha", "teslemetry", "demo_teslemetry"), ("tessie_ha", "tessie", "demo_tessie")]
IDS = [c[1] for c in CASES]


def states(brand):
    return json.loads((FIX / brand / "ha_states_us.json").read_text())


def catalog(brand):
    return json.loads((FIX / brand / "entity_catalog.json").read_text())["entities"]


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def fetch(profile, slug, sts, **kw):
    ha = FakeHA(sts, apply_effects=False)
    try:
        a = HomeAssistantAdapter(ha.url, FakeHA.TOKEN, load_profile(profile), slug, **kw)
        return a, a.fetch_state(NOW)
    finally:
        ha.stop()


def templates(profile):
    out = []
    for name, spec in profile["fields"].items():
        out += [(name, e) for e in (spec["entities"].values() if spec["kind"] == "open_list" else [spec["entity"]])]
    for cname, cspec in profile["commands"].items():
        for step in cspec["steps"]:
            ents = step["entity_by_arg"]["map"].values() if "entity_by_arg" in step else [step["entity"]]
            out += [(f"command {cname}", e) for e in ents]
    return out


def catalog_entry(brand, template):
    """The catalogue entry Home Assistant would create for ``<domain>.{slug}_<name slug>``, or None."""
    domain, rest = template.split(".", 1)
    assert rest.startswith("{slug}"), template
    tail = rest[len("{slug}"):].lstrip("_")
    for entry in catalog(brand).get(domain, {}).values():
        if slugify(entry["name"]) == tail:
            return entry
    return None


# -- the profiles themselves ------------------------------------------------------------------------------------
@pytest.mark.parametrize("name,brand,slug", CASES, ids=IDS)
def test_profile_is_valid_complete_and_honest(name, brand, slug):
    p = load_profile(name)
    assert validate_profile(p) == []
    assert set(p["commands"]) == set(COMMANDS)
    assert p["integration"] == brand and p["tested"] is False
    assert "verified against ha core" in p["label"].lower() and "not run on a real car" in p["label"].lower()
    assert "4368abee2a92" in p["verified_against"] and "Not run on a real car" in p["verified_against"]
    assert "UNTESTED" not in json.dumps(p)


@pytest.mark.parametrize("name,brand,slug", CASES, ids=IDS)
def test_every_entity_is_one_the_integration_creates_and_disabled_flags_match_core(name, brand, slug):
    p = load_profile(name)
    missing = [(what, e) for what, e in templates(p) if catalog_entry(brand, e) is None]
    assert missing == []
    # fields: disabled_by_default exactly mirrors entity_registry_enabled_default=False in core
    wrong = []
    for fname, spec in p["fields"].items():
        ents = list(spec["entities"].values()) if spec["kind"] == "open_list" else [spec["entity"]]
        for e in ents:
            core_disabled = bool(catalog_entry(brand, e).get("disabled_by_default"))
            if core_disabled != bool(spec.get("disabled_by_default")):
                wrong.append((fname, e, core_disabled))
    assert wrong == []
    # every disabled entity the profile wants has a Home Assistant name to tell the user to enable
    for spec in p["fields"].values():
        if spec.get("disabled_by_default"):
            assert slugify(spec["enable_as"]) in {slugify(v["name"]) for v in catalog(brand)[spec["entity"].split(".")[0]].values()}


def test_integration_specific_entity_names_from_core():
    tm, ts, tf = load_profile("teslemetry_ha"), load_profile("tessie_ha"), load_profile("tesla_fleet")
    for p in (tm, ts):
        assert p["commands"]["vent_windows"]["steps"][0]["entity"] == "cover.{slug}_vent_windows"
    assert tf["commands"]["vent_windows"]["steps"][0]["entity"] == "cover.{slug}_windows"
    assert ts["fields"]["defrost_on"]["entity"] == "switch.{slug}_defrost_mode"
    assert tm["fields"]["defrost_on"]["entity"] == "switch.{slug}_defrost"
    seats = ts["commands"]["seat_heat"]["steps"][0]["entity_by_arg"]["map"]
    assert seats["driver"] == "select.{slug}_seat_heater_left" and seats["passenger"] == "select.{slug}_seat_heater_right"
    # Teslemetry ships range, gear, speed, odometer and the tire sensors disabled; Tessie ships them enabled
    for f in ("range_mi", "shift_state", "speed_mph", "odometer_mi", "tire_psi_fl", "tire_warn_rr"):
        assert tm["fields"][f]["disabled_by_default"] is True
        assert not ts["fields"][f].get("disabled_by_default")
    assert {c["entity"] for c in tm["disabled_command_entities"]} == {
        f"select.{{slug}}_seat_heater_rear_{s}" for s in ("left", "center", "right")}


# -- reading state -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name,brand,slug", CASES, ids=IDS)
def test_state_mapping(name, brand, slug):
    a, st = fetch(name, slug, states(brand))
    v = st.value
    assert v("battery_pct") == 71 and v("range_mi") == pytest.approx(214.3)
    assert v("charging_state") == "charging" and v("plugged_in") is True and v("charge_power_kw") == 7
    assert v("charge_limit_pct") == 80 and v("charge_current_max_a") == 32 and v("charge_port_open") is True
    assert v("charge_eta_min") == pytest.approx(145, abs=1)
    assert v("locked") is True and v("doors_open") == [] and v("windows_open") == []
    assert v("trunk_open") is False and v("frunk_open") is False and v("sentry_on") is False
    assert v("climate_on") is False and v("cabin_temp_f") == 61 and v("outside_temp_f") == 48 and v("target_temp_f") == 68
    assert v("location_zone") == "home" and v("asleep") is False and v("software_update_available") is False
    assert v("odometer_mi") == 12345
    assert [round(v(f), 1) for f in ("tire_psi_fl", "tire_psi_fr", "tire_psi_rl", "tire_psi_rr")] == [42.1, 41.8, 41.9, 42.3]
    assert v("tire_warn_fl") is False


@pytest.mark.parametrize("name,brand,slug", CASES, ids=IDS)
def test_openings_and_sleep_read_correctly(name, brand, slug):
    sts = states(brand)
    for s in sts:
        if s["entity_id"].endswith("_rear_driver_door"):
            s["state"] = "on"
        if s["entity_id"].endswith("_front_passenger_window"):
            s["state"] = "on"
        if s["entity_id"].endswith("_frunk"):
            s["state"] = "open"
        if s["entity_id"].endswith("_status"):
            s["state"] = "off"                       # connectivity off = asleep / offline
    _, st = fetch(name, slug, sts)
    assert st.value("doors_open") == ["rear_driver"] and st.value("windows_open") == ["front_passenger"]
    assert st.value("frunk_open") is True and st.value("asleep") is True


# -- motion fails closed -----------------------------------------------------------------------------------------
def _set(sts, suffix, state):
    for s in sts:
        if s["entity_id"].endswith(suffix):
            s["state"] = state


@pytest.mark.parametrize("name,brand,slug", CASES, ids=IDS)
def test_parked_gear_and_zero_speed_never_prove_parked(name, brand, slug):
    """Both integrations turn a missing gear into 'p' and a missing speed into 0 (and serve cached / restored values), so a
    quiet-looking car with no plug and no TeslaMate state is 'cannot tell', not 'parked'."""
    sts = states(brand)
    _set(sts, "_charge_cable", "off")
    a, st = fetch(name, slug, sts)
    assert not st.has("shift_state") and not st.has("speed_mph")
    assert st.motion() is None and st.motion(NOW, 900) is None
    assert st.has("moving") is False


@pytest.mark.parametrize("name,brand,slug", CASES, ids=IDS)
def test_gear_or_speed_that_says_moving_still_blocks(name, brand, slug):
    for suffix, state, field in (("_shift_state", "d", "shift_state"), ("_shift_state", "r", "shift_state"),
                                 ("_shift_state", "n", "shift_state"), ("_speed", "37", "speed_mph")):
        sts = states(brand)
        _set(sts, suffix, state)
        _set(sts, "_charge_cable", "off")
        _, st = fetch(name, slug, sts)
        assert st.has(field)
        assert st.motion() is True
        assert st.motion(NOW, 900) is True


@pytest.mark.parametrize("name,brand,slug", CASES, ids=IDS)
def test_even_a_stale_moving_reading_blocks_and_a_stale_plug_does_not_prove_parked(name, brand, slug):
    sts = states(brand)
    _set(sts, "_speed", "30")
    for s in sts:
        if s["entity_id"].endswith("_speed"):
            s["last_reported"] = s["last_updated"] = s["last_changed"] = "2026-01-30T10:00:00+00:00"
    _, st = fetch(name, slug, sts)
    assert st.motion(NOW, 900) is True
    sts = states(brand)
    for s in sts:                              # the plug reading is two days old and the gear says nothing useful
        if s["entity_id"].endswith("_charge_cable"):
            s["last_reported"] = s["last_updated"] = s["last_changed"] = "2026-01-30T10:00:00+00:00"
    sts = [s for s in sts if not s["entity_id"].endswith(("_status",))]
    _, st = fetch(name, slug, sts)
    assert st.motion(NOW, 900) is None


@pytest.mark.parametrize("name,brand,slug", CASES, ids=IDS)
def test_a_plugged_in_car_and_a_teslamate_state_entity_prove_parked(name, brand, slug):
    _, st = fetch(name, slug, states(brand))                          # plugged in (charge cable on)
    assert st.motion(NOW, 900) is False
    sts = states(brand)
    _set(sts, "_charge_cable", "off")
    now = "2026-02-01T10:00:00+00:00"
    sts.append({"entity_id": f"sensor.teslamate_{slug}_state", "state": "online", "attributes": {},
                "last_changed": now, "last_reported": now, "last_updated": now})
    _, st = fetch(name, slug, sts)
    assert st.motion(NOW, 900) is False
    sts[-1]["state"] = "driving"
    _, st = fetch(name, slug, sts)
    assert st.motion(NOW, 900) is True


def test_untrusted_values_validation_and_semantics():
    p = load_profile("tessie_ha")
    assert p["fields"]["shift_state"]["untrusted_values"] == ["P"] and p["fields"]["speed_mph"]["untrusted_values"] == [0]
    bad = copy.deepcopy(p)
    bad["fields"]["shift_state"]["untrusted_values"] = "P"
    assert any("untrusted_values" in x for x in validate_profile(bad))
    bad["fields"]["shift_state"]["untrusted_values"] = []
    assert any("untrusted_values" in x for x in validate_profile(bad))
    # the tested tesla_fleet profile is unchanged by this feature
    assert "untrusted_values" not in json.dumps(load_profile("tesla_fleet"))


def test_discovery_signature_validation():
    p = load_profile("tessie_ha")
    p["discovery_signature"] = {"present": ["cover.no_slug"]}
    assert any("discovery_signature" in x for x in validate_profile(p))


# -- discovery ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name,brand,slug", CASES, ids=IDS)
def test_discovery_picks_the_right_profile_and_says_it_is_unrun(name, brand, slug):
    out = discover(states(brand))
    rec = out["recommended"]
    assert rec and rec["ha_profile"] == name and rec["ha_slug"] == slug
    assert "not run on a real car" in rec["note"]
    assert out["proposals"][0]["coverage"] >= 0.9


def test_discovery_still_picks_tesla_fleet_for_a_tesla_fleet_car():
    out = discover(load_fixture("ha_states_tesla_fleet.json"))
    assert out["recommended"]["ha_profile"] == "tesla_fleet"
    # and a tesla_fleet car's states do not get the teslemetry / tessie profile even though most names are shared
    best = {p["profile"]: p for p in out["proposals"]}
    assert best["tesla_fleet"]["signature"] >= 0


def test_discovery_separates_teslemetry_from_tessie_and_tesla_fleet_on_shared_names():
    sts = states("teslemetry")
    names = {p["profile"]: p for p in discover(sts)["proposals"]}
    assert names["teslemetry_ha"]["signature"] > names["tessie_ha"]["signature"]
    assert names["teslemetry_ha"]["signature"] > names["tesla_fleet"]["signature"]
    sts = states("tessie")
    names = {p["profile"]: p for p in discover(sts)["proposals"]}
    assert names["tessie_ha"]["signature"] > names["teslemetry_ha"]["signature"]


def test_discovery_warns_to_enable_what_teslemetry_ships_disabled():
    sts = [s for s in states("teslemetry") if not s["entity_id"].endswith(("_shift_state", "_speed", "_odometer", "_battery_range"))
           and "tire_pressure" not in s["entity_id"]]
    out = discover(sts)
    assert out["recommended"]["ha_profile"] == "teslemetry_ha"
    text = " ".join(out["warnings"])
    assert "Shift state" in text and "Odometer" in text and "Battery range" in text and "Tire pressure front left" in text
    assert "Teslemetry" in text


# -- commands ----------------------------------------------------------------------------------------------------
EXPECT = {
    "teslemetry": {
        "lock": ("lock/lock", {"entity_id": "lock.demo_teslemetry_lock"}),
        "unlock": ("lock/unlock", {"entity_id": "lock.demo_teslemetry_lock"}),
        "open_trunk": ("cover/open_cover", {"entity_id": "cover.demo_teslemetry_trunk"}),
        "open_frunk": ("cover/open_cover", {"entity_id": "cover.demo_teslemetry_frunk"}),
        "vent_windows": ("cover/open_cover", {"entity_id": "cover.demo_teslemetry_vent_windows"}),
        "close_windows": ("cover/close_cover", {"entity_id": "cover.demo_teslemetry_vent_windows"}),
        "sentry_on": ("switch/turn_on", {"entity_id": "switch.demo_teslemetry_sentry_mode"}),
        "sentry_off": ("switch/turn_off", {"entity_id": "switch.demo_teslemetry_sentry_mode"}),
        "charge_start": ("switch/turn_on", {"entity_id": "switch.demo_teslemetry_charge"}),
        "charge_stop": ("switch/turn_off", {"entity_id": "switch.demo_teslemetry_charge"}),
        "honk": ("button/press", {"entity_id": "button.demo_teslemetry_honk_horn"}),
        "flash": ("button/press", {"entity_id": "button.demo_teslemetry_flash_lights"}),
        "climate_off": ("climate/set_hvac_mode", {"entity_id": "climate.demo_teslemetry_climate", "hvac_mode": "off"}),
        "charge_limit": ("number/set_value", {"entity_id": "number.demo_teslemetry_charge_limit", "value": 90}),
        "charge_amps": ("number/set_value", {"entity_id": "number.demo_teslemetry_charge_current", "value": 16}),
        "set_temp": ("climate/set_temperature", {"entity_id": "climate.demo_teslemetry_climate", "temperature": 70.0}),
        "defrost": ("switch/turn_off", {"entity_id": "switch.demo_teslemetry_defrost"}),
        "seat_heat": ("select/select_option", {"entity_id": "select.demo_teslemetry_seat_heater_front_left", "option": "high"}),
    },
    "tessie": {
        "lock": ("lock/lock", {"entity_id": "lock.demo_tessie_lock"}),
        "unlock": ("lock/unlock", {"entity_id": "lock.demo_tessie_lock"}),
        "open_trunk": ("cover/open_cover", {"entity_id": "cover.demo_tessie_trunk"}),
        "open_frunk": ("cover/open_cover", {"entity_id": "cover.demo_tessie_frunk"}),
        "vent_windows": ("cover/open_cover", {"entity_id": "cover.demo_tessie_vent_windows"}),
        "close_windows": ("cover/close_cover", {"entity_id": "cover.demo_tessie_vent_windows"}),
        "sentry_on": ("switch/turn_on", {"entity_id": "switch.demo_tessie_sentry_mode"}),
        "sentry_off": ("switch/turn_off", {"entity_id": "switch.demo_tessie_sentry_mode"}),
        "charge_start": ("switch/turn_on", {"entity_id": "switch.demo_tessie_charge"}),
        "charge_stop": ("switch/turn_off", {"entity_id": "switch.demo_tessie_charge"}),
        "honk": ("button/press", {"entity_id": "button.demo_tessie_honk_horn"}),
        "flash": ("button/press", {"entity_id": "button.demo_tessie_flash_lights"}),
        "climate_off": ("climate/set_hvac_mode", {"entity_id": "climate.demo_tessie_climate", "hvac_mode": "off"}),
        "charge_limit": ("number/set_value", {"entity_id": "number.demo_tessie_charge_limit", "value": 90}),
        "charge_amps": ("number/set_value", {"entity_id": "number.demo_tessie_charge_current", "value": 16}),
        "set_temp": ("climate/set_temperature", {"entity_id": "climate.demo_tessie_climate", "temperature": 70.0}),
        "defrost": ("switch/turn_off", {"entity_id": "switch.demo_tessie_defrost_mode"}),
        "seat_heat": ("select/select_option", {"entity_id": "select.demo_tessie_seat_heater_left", "option": "high"}),
    },
}
ARGS = {"charge_limit": {"percent": 90}, "charge_amps": {"amps": 16}, "set_temp": {"temp_f": 70},
        "defrost": {"on": False}, "seat_heat": {"seat": "driver", "level": 3}}


@pytest.mark.parametrize("name,brand,slug", CASES, ids=IDS)
def test_each_command_maps_to_the_service_call_the_integration_accepts(name, brand, slug):
    ha = FakeHA(states(brand), apply_effects=False)
    try:
        a = HomeAssistantAdapter(ha.url, FakeHA.TOKEN, load_profile(name), slug)
        a.fetch_state(NOW)
        for cmd, (path, payload) in EXPECT[brand].items():
            before = len(ha.service_calls)
            a.send_command(cmd, ARGS.get(cmd, {}))
            assert ha.service_calls[before:] == [(path, payload)], cmd
        a.send_command("climate_on", {"temp_f": 68})
        assert ha.service_calls[-2:] == [
            ("climate/set_hvac_mode", {"entity_id": f"climate.{slug}_climate", "hvac_mode": "heat_cool"}),
            ("climate/set_temperature", {"entity_id": f"climate.{slug}_climate", "temperature": 68.0})]
    finally:
        ha.stop()
    # all 19 commands are covered: the 18 above plus climate_on
    assert set(EXPECT[brand]) | {"climate_on"} == set(COMMANDS)


@pytest.mark.parametrize("name,brand,slug", CASES, ids=IDS)
def test_service_domains_match_entity_domains_and_services_exist_in_core(name, brand, slug):
    """Every step calls a service of its own entity's domain, and only services the core platform implements:
    lock.lock/unlock, cover.open_cover/close_cover, switch.turn_on/off, button.press, number.set_value,
    select.select_option, climate.set_hvac_mode/set_temperature."""
    allowed = {"lock": {"lock", "unlock"}, "cover": {"open_cover", "close_cover"}, "switch": {"turn_on", "turn_off"},
               "button": {"press"}, "number": {"set_value"}, "select": {"select_option"},
               "climate": {"set_hvac_mode", "set_temperature"}}
    p = load_profile(name)
    for cname, cspec in p["commands"].items():
        for step in cspec["steps"]:
            services = [step["service"]] if "service" in step else list(step["service_by_arg"].values())[1:]
            ents = step["entity_by_arg"]["map"].values() if "entity_by_arg" in step else [step["entity"]]
            for svc in services:
                dom, act = svc.split(".")
                assert act in allowed[dom], (cname, svc)
                assert all(e.startswith(dom + ".") for e in ents), (cname, svc)


@pytest.mark.parametrize("name,brand,slug", CASES, ids=IDS)
def test_a_command_for_a_disabled_entity_is_refused_before_sending(name, brand, slug):
    sts = [s for s in states(brand) if not s["entity_id"].endswith("_honk_horn")]
    ha = FakeHA(sts, apply_effects=False)
    try:
        a = HomeAssistantAdapter(ha.url, FakeHA.TOKEN, load_profile(name), slug)
        a.fetch_state(NOW)
        with pytest.raises(Exception):
            a.send_command("honk", {})
        assert ha.service_calls == []
    finally:
        ha.stop()
