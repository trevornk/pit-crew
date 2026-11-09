"""BMW and Kia/Hyundai Home Assistant profiles (G7): checked against fixtures derived from the integrations' own
sources/tests. They are NOT run on a real car, and these tests do not pretend otherwise."""

import json
import re
from pathlib import Path

import pytest

from fakes import FakeHA
from pit_crew.adapters.discovery import discover
from pit_crew.adapters.homeassistant import HomeAssistantAdapter, parse_ha_time
from pit_crew.adapters.profile import entity_ids_for_field, load_profile, validate_profile

FIX = Path(__file__).resolve().parent / "fixtures" / "integrations"
NOW = parse_ha_time("2026-02-01T10:05:00+00:00")


def states(brand, name):
    return json.loads((FIX / brand / name).read_text())


def catalog(brand):
    return json.loads((FIX / brand / "entity_catalog.json").read_text())["entities"]


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def adapter(ha, profile, slug):
    return HomeAssistantAdapter(ha.url, FakeHA.TOKEN, load_profile(profile), slug)


def fetch(profile, slug, sts):
    ha = FakeHA(sts)
    try:
        a = adapter(ha, profile, slug)
        return a, a.fetch_state(NOW), ha
    finally:
        ha.stop()


def all_template_entities(profile, include_alts=False):
    out = []
    for name, spec in profile["fields"].items():
        if spec["kind"] == "open_list":
            out += [(name, e) for e in spec["entities"].values()]
        else:
            out.append((name, spec["entity"]))
            if spec.get("data_time_entity"):
                out.append((f"{name} data time", spec["data_time_entity"]))
            if include_alts:
                out += [(name, e) for e in spec.get("alt_entities", [])]
    for cname, cspec in profile["commands"].items():
        for step in cspec["steps"]:
            out.append((f"command {cname}", step["entity"]))
    return out


def derivable(brand, entity_template):
    """Is ``<domain>.{slug}_<name>`` an entity the integration really creates (name slug taken from its strings)?"""
    domain, rest = entity_template.split(".", 1)
    assert rest.startswith("{slug}"), entity_template
    tail = rest[len("{slug}"):].lstrip("_")
    names = {slugify(v["name"]) for v in catalog(brand).get(domain, {}).values() if v.get("name")}
    if domain == "device_tracker":
        # the tracker is the car itself (BMW: _attr_name = None -> no suffix) or a named "Location" entity (Kia)
        return tail in ("", *names)
    assert tail, entity_template
    return tail in names


# -- both profiles -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name,brand", [("bmw_connected_drive", "bmw_connected_drive"), ("kia_hyundai_connect", "kia_uvo")])
def test_every_entity_in_the_profile_is_one_the_integration_actually_creates(name, brand):
    profile = load_profile(name)
    assert validate_profile(profile) == []
    bad = [(what, ent) for what, ent in all_template_entities(profile) if not derivable(brand, ent)]
    assert bad == []


@pytest.mark.parametrize("name", ["bmw_connected_drive", "kia_hyundai_connect"])
def test_profiles_are_honest_about_what_was_and_was_not_verified(name):
    p = load_profile(name)
    assert p["tested"] is False
    assert "not run on a real car" in p["label"].lower() or "not run on a real car" in p["verified_against"].lower()
    assert "UNTESTED" not in json.dumps(p)                          # the old 'written from documentation' wording is gone
    assert p["verified_against"] and p["integration_title"]


def test_bmw_profile_says_the_integration_was_removed_from_core():
    p = load_profile("bmw_connected_drive")
    assert p["deprecated"] is True and "removed" in p["deprecated_note"].lower() and "#165075" in p["deprecated_note"]


def test_kia_profile_says_it_is_a_hacs_custom_integration():
    p = load_profile("kia_hyundai_connect")
    assert "HACS" in p["label"] and "custom" in p["label"].lower() and p["integration"] == "kia_uvo"


# -- BMW -------------------------------------------------------------------------------------------------------------
def test_bmw_i4_state_mapping_from_cores_own_snapshots():
    a, st, _ = fetch("bmw_connected_drive", "i4_edrive40", states("bmw_connected_drive", "ha_states_i4_edrive40.json"))
    v = st.value
    assert v("battery_pct") == 80 and v("range_mi") == pytest.approx(472 / 1.609344, abs=0.05)
    assert v("odometer_mi") == pytest.approx(1121 / 1.609344, abs=0.05)
    assert v("plugged_in") is False and v("charging_state") == "disconnected"       # not_charging + unplugged
    assert v("locked") is True and v("location_zone") == "home"
    assert v("doors_open") == [] and v("windows_open") == [] and v("trunk_open") is False and v("frunk_open") is False
    assert v("charge_limit_pct") == 80 and v("charge_current_max_a") == 16
    assert v("climate_on") is True
    assert v("tire_psi_fl") == pytest.approx(2.41 * 14.5038, abs=0.05) and v("tire_psi_rr") == pytest.approx(3.31 * 14.5038, abs=0.05)
    assert v("tire_rec_front_psi") == pytest.approx(2.69 * 14.5038, abs=0.05)


def test_bmw_ix_charging_and_commands_available():
    a, st, _ = fetch("bmw_connected_drive", "ix_xdrive50", states("bmw_connected_drive", "ha_states_ix_xdrive50.json"))
    assert st.value("charging_state") == "charging" and st.value("plugged_in") in (True, False)
    caps = a.capabilities().commands
    assert {"lock", "unlock", "climate_on", "climate_off", "honk", "flash", "charge_start", "charge_stop", "charge_limit",
            "charge_amps"} <= caps


def test_bmw_openings_come_from_the_attributes_of_the_lids_and_windows_entities():
    sts = states("bmw_connected_drive", "ha_states_i4_edrive40.json")
    for s in sts:
        if s["entity_id"].endswith("_lids"):
            s["state"] = "on"
            s["attributes"].update(leftFront="OPEN", trunk="OPEN", hood="INTERMEDIATE")
        if s["entity_id"].endswith("_windows"):
            s["state"] = "on"
            s["attributes"].update(rightRear="OPEN_TILT", rear="OPEN")
    _, st, _ = fetch("bmw_connected_drive", "i4_edrive40", sts)
    assert st.value("doors_open") == ["front_driver"]
    assert st.value("windows_open") == ["rear_passenger", "rear"]
    assert st.value("trunk_open") is True and st.value("frunk_open") is True


def test_bmw_unknown_opening_states_are_unknown_not_closed():
    sts = states("bmw_connected_drive", "ha_states_i4_edrive40.json")
    for s in sts:
        if s["entity_id"].endswith("_lids"):
            s["attributes"].update(leftFront="UNKNOWN")
        if s["entity_id"].endswith("_windows"):
            s["state"] = "unavailable"
    _, st, _ = fetch("bmw_connected_drive", "i4_edrive40", sts)
    assert not st.has("doors_open")                  # nothing reads open, but one door is unknown: no 'all closed' claim
    assert not st.has("windows_open")


def test_bmw_charging_status_enum_covers_every_state_the_integration_declares():
    prof = load_profile("bmw_connected_drive")
    declared = {"default", "charging", "error", "complete", "fully_charged", "finished_fully_charged", "finished_not_full",
                "invalid", "not_charging", "plugged_in", "waiting_for_charging", "target_reached"}   # sensor.py options
    assert declared <= set(prof["fields"]["charging_state"]["map"])


def test_bmw_target_pressures_feed_the_low_tire_threshold():
    from pit_crew.brain import tires
    _, st, _ = fetch("bmw_connected_drive", "i4_edrive40", states("bmw_connected_drive", "ha_states_i4_edrive40.json"))
    assert st.value("tire_rec_front_psi") == pytest.approx(39.0, abs=0.1)
    assert tires.low_threshold_psi(st, "front", 0) == pytest.approx(35.1, abs=0.1)
    assert tires.low_threshold_psi(st, "rear", 0) == pytest.approx(39.6, abs=0.1)
    # the target pressures ship disabled in HA: without them the profile says so and the default threshold applies
    sts = [s for s in states("bmw_connected_drive", "ha_states_i4_edrive40.json") if "target_pressure" not in s["entity_id"]]
    a, st2, _ = fetch("bmw_connected_drive", "i4_edrive40", sts)
    assert not st2.has("tire_rec_front_psi") and tires.low_threshold_psi(st2, "front", 0) == tires.DEFAULT_MIN_PSI
    assert "Front left target pressure" in " ".join(a.setup_warnings())


def test_bmw_commands_use_the_integrations_real_services():
    ha = FakeHA(states("bmw_connected_drive", "ha_states_ix_xdrive50.json"))
    try:
        a = adapter(ha, "bmw_connected_drive", "ix_xdrive50")
        a.fetch_state(NOW)
        for cmd, args in (("lock", {}), ("climate_on", {}), ("charge_stop", {}), ("charge_limit", {"percent": 90}),
                          ("charge_amps", {"amps": 16}), ("honk", {})):
            a.send_command(cmd, args)
        calls = [(s, b) for s, b in ha.service_calls]
        assert ("lock/lock", {"entity_id": "lock.ix_xdrive50_lock"}) in calls
        assert ("button/press", {"entity_id": "button.ix_xdrive50_activate_air_conditioning"}) in calls
        assert ("switch/turn_off", {"entity_id": "switch.ix_xdrive50_charging"}) in calls
        assert ("number/set_value", {"entity_id": "number.ix_xdrive50_target_soc", "value": 90}) in calls
        assert ("select/select_option", {"entity_id": "select.ix_xdrive50_ac_charging_limit", "option": "16"}) in calls
        assert ("button/press", {"entity_id": "button.ix_xdrive50_sound_horn"}) in calls
        from pit_crew.adapters.base import AdapterError
        with pytest.raises(AdapterError, match="does not accept amps=17"):             # not a KeyError
            a.send_command("charge_amps", {"amps": 17})
    finally:
        ha.stop()


# -- Kia / Hyundai --------------------------------------------------------------------------------------------------
def test_kia_us_units_state_mapping():
    a, st, _ = fetch("kia_hyundai_connect", "my_ev", states("kia_uvo", "ha_states_us.json"))
    v = st.value
    assert v("battery_pct") == 64 and v("range_mi") == 181 and v("odometer_mi") == 18342
    assert v("plugged_in") is True and v("charging_state") == "stopped" and v("charge_power_kw") == 0.0
    assert v("charge_limit_pct") == 80 and v("charge_eta_min") == 0 and v("charge_port_open") is False
    assert v("locked") is True and v("location_zone") == "home"
    assert v("doors_open") == [] and v("windows_open") == [] and v("trunk_open") is False and v("frunk_open") is False
    assert v("climate_on") is False and v("defrost_on") is False
    assert v("outside_temp_f") == 55 and v("target_temp_f") == 70 and v("shift_state") == "P"
    assert [v(f) for f in ("tire_psi_fl", "tire_psi_fr", "tire_psi_rl", "tire_psi_rr")] == [35, 35, 36, 36]
    assert st.motion() is False                                    # plugged in and in Park: provably parked


def test_kia_metric_units_are_converted_and_openings_listed():
    a, st, _ = fetch("kia_hyundai_connect", "my_ev_eu", states("kia_uvo", "ha_states_eu.json"))
    v = st.value
    assert v("range_mi") == pytest.approx(291 / 1.609344, abs=0.05) and v("odometer_mi") == pytest.approx(29521 / 1.609344, abs=0.1)
    assert v("outside_temp_f") == pytest.approx(55.4, abs=0.05) and v("target_temp_f") == pytest.approx(69.8, abs=0.05)
    assert v("charging_state") == "charging" and v("charge_power_kw") == 7.4 and v("charge_eta_min") == 95
    assert v("doors_open") == ["front_passenger"] and v("windows_open") == ["rear_driver"]
    assert v("tire_psi_rr") == pytest.approx(2.2 * 14.5038, abs=0.05)


def test_kia_older_entity_names_still_work_through_alt_entities():
    sts = states("kia_uvo", "ha_states_us.json")
    renamed = {"binary_sensor.my_ev_ev_battery_plug": "binary_sensor.my_ev_ev_battery_is_plugged_in",
               "binary_sensor.my_ev_ev_battery_charge": "binary_sensor.my_ev_ev_battery_is_charging",
               "binary_sensor.my_ev_air_conditioner": "binary_sensor.my_ev_air_conditioner_is_on"}
    for s in sts:
        s["entity_id"] = renamed.get(s["entity_id"], s["entity_id"])
    _, st, _ = fetch("kia_hyundai_connect", "my_ev", sts)
    assert st.value("plugged_in") is True and st.value("charging_state") == "stopped" and st.value("climate_on") is False
    spec = load_profile("kia_hyundai_connect")["fields"]["plugged_in"]
    assert entity_ids_for_field(spec, "x") == ["binary_sensor.x_ev_battery_plug", "binary_sensor.x_ev_battery_is_plugged_in"]
    assert entity_ids_for_field(spec, "x", "binary_sensor.mine") == ["binary_sensor.mine"]      # an override wins


def test_kia_hazard_buttons_ship_disabled_so_honk_and_flash_are_explained_not_offered():
    ha = FakeHA(states("kia_uvo", "ha_states_us.json"))
    try:
        a = adapter(ha, "kia_hyundai_connect", "my_ev")
        a.fetch_state(NOW)
        caps = a.capabilities().commands
        assert {"lock", "unlock", "climate_on", "climate_off", "set_temp", "charge_start", "charge_stop", "charge_limit",
                "vent_windows", "close_windows"} <= caps
        assert "honk" not in caps and "flash" not in caps
        warn = " ".join(a.setup_warnings())
        assert "Start Hazard Lights and Horn (for honk)" in warn and "Start Hazard Lights (for flash)" in warn
        assert "Kia Uvo / Hyundai Bluelink" in warn
        ha.states["button.my_ev_start_hazard_lights"] = {"entity_id": "button.my_ev_start_hazard_lights", "state": "unknown",
                                                           "attributes": {}}
        a._refresh_states()
        assert "flash" in a.capabilities().commands and "(for flash)" not in " ".join(a.setup_warnings())
    finally:
        ha.stop()


def test_kia_commands_use_the_integrations_real_entities_and_services():
    ha = FakeHA(states("kia_uvo", "ha_states_us.json"))
    try:
        a = adapter(ha, "kia_hyundai_connect", "my_ev")
        a.fetch_state(NOW)
        for cmd, args in (("lock", {}), ("climate_on", {"temp_f": 72}), ("climate_off", {}), ("charge_start", {}),
                          ("charge_limit", {"percent": 90}), ("close_windows", {})):
            a.send_command(cmd, args)
        calls = ha.service_calls
        assert calls[0] == ("lock/lock", {"entity_id": "lock.my_ev_door_lock"})
        assert ("climate/set_temperature", {"entity_id": "climate.my_ev_climate_control", "temperature": 72}) in calls
        assert calls.index(("climate/set_temperature", {"entity_id": "climate.my_ev_climate_control", "temperature": 72})) < \
            calls.index(("switch/turn_on", {"entity_id": "switch.my_ev_climate"}))            # temperature first, then start
        assert ("switch/turn_on", {"entity_id": "switch.my_ev_ev_charging"}) in calls
        assert ("number/set_value", {"entity_id": "number.my_ev_ac_charging_limit", "value": 90}) in calls
        assert ("button/press", {"entity_id": "button.my_ev_close_all_windows"}) in calls
    finally:
        ha.stop()


def test_kia_without_gear_or_plug_cannot_prove_parked():
    sts = [s for s in states("kia_uvo", "ha_states_us.json") if not s["entity_id"].endswith(("_gear_position", "_ev_battery_plug"))]
    _, st, _ = fetch("kia_hyundai_connect", "my_ev", sts)
    assert st.motion() is None                                     # unlock/trunk stay refused (fail closed)


# -- discovery --------------------------------------------------------------------------------------------------------
def test_discovery_finds_each_brand_and_states_what_was_verified():
    res = discover(states("kia_uvo", "ha_states_us.json"))
    assert res["recommended"]["ha_profile"] == "kia_hyundai_connect" and res["recommended"]["ha_slug"] == "my_ev"
    assert "not run on a real car" in res["recommended"]["note"]
    assert any("Start Hazard Lights" in w for w in res["warnings"])
    res = discover(states("bmw_connected_drive", "ha_states_i4_edrive40.json"))
    assert res["recommended"]["ha_profile"] == "bmw_connected_drive" and res["recommended"]["ha_slug"] == "i4_edrive40"
    assert any("removed" in w.lower() for w in res["warnings"])


def test_a_current_profile_outranks_a_deprecated_one_with_equal_coverage():
    mixed = states("kia_uvo", "ha_states_us.json") + states("bmw_connected_drive", "ha_states_i4_edrive40.json")
    res = discover(mixed)
    assert res["proposals"][0]["deprecated"] is False


# -- Kia: the integration serves a cached cloud copy, so HA's own stamp proves nothing about the car's data (review P1-3) --
def _kia_with_data_time(when):
    sts = states("kia_uvo", "ha_states_us.json")
    for s in sts:
        if s["entity_id"].endswith("_last_updated_at"):
            s["state"] = when
    return sts


def test_kia_gear_p_from_hours_old_cloud_data_is_not_proof_of_parked():
    unplugged = [s for s in _kia_with_data_time("2026-02-01T05:00:00+00:00") if not s["entity_id"].endswith("_ev_battery_plug")]
    _, st, _ = fetch("kia_hyundai_connect", "my_ev", unplugged)
    assert st.value("shift_state") == "P"                          # the reading is shown ...
    assert st.age_s("shift_state", NOW) > 4 * 3600                 # ... as the old data it is, not as 'fresh' from HA's stamp
    assert st.motion(NOW, 900) is None                             # and never proves parked
    assert st.age_s("locked", NOW) > 4 * 3600 and st.age_s("location_zone", NOW) > 4 * 3600


def test_kia_gear_p_with_fresh_data_time_proves_parked():
    unplugged = [s for s in _kia_with_data_time("2026-02-01T10:00:00+00:00") if not s["entity_id"].endswith("_ev_battery_plug")]
    _, st, _ = fetch("kia_hyundai_connect", "my_ev", unplugged)
    assert st.motion(NOW, 900) is False


def test_kia_without_the_data_time_sensor_does_not_trust_gear():
    sts = [s for s in states("kia_uvo", "ha_states_us.json")
           if not s["entity_id"].endswith(("_last_updated_at", "_ev_battery_plug"))]
    _, st, _ = fetch("kia_hyundai_connect", "my_ev", sts)
    assert not st.is_available("shift_state") and st.motion(NOW, 900) is None      # fail closed
    assert st.is_available("locked")                                               # other fields fall back to HA's stamp


def test_kia_profile_says_the_gear_claim_depends_on_the_data_time():
    p = load_profile("kia_hyundai_connect")
    assert "Last Updated At" in p["motion_hint"] and "stale" in p["motion_hint"]
    bad = load_profile("kia_hyundai_connect")
    bad["fields"]["shift_state"].pop("data_time_entity")
    assert any("data_time_required" in x for x in validate_profile(bad))
