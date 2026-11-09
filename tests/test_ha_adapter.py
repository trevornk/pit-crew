import json

import pytest

from fakes import FakeHA, load_fixture
from pit_crew.adapters.base import AdapterError, AdapterUnavailable
from pit_crew.adapters.homeassistant import HomeAssistantAdapter, parse_ha_time
from pit_crew.adapters.profile import bundled_profiles, load_profile, validate_profile
from pit_crew.brain.commands import COMMANDS

NOW = parse_ha_time("2025-10-05T00:17:00+00:00")


@pytest.fixture
def ha():
    fake = FakeHA()
    yield fake
    fake.stop()


def adapter(ha, **kw):
    return HomeAssistantAdapter(ha.url, kw.pop("token", FakeHA.TOKEN), load_profile("tesla_fleet"),
                                kw.pop("slug", "demo_car"), **kw)


def test_profile_valid_and_bundled():
    assert "tesla_fleet" in bundled_profiles()
    assert validate_profile(load_profile("tesla_fleet")) == []
    assert set(load_profile("tesla_fleet")["commands"]) == set(COMMANDS)


def test_state_mapping_against_live_fixture(ha):
    st = adapter(ha).fetch_state(NOW)
    v = st.value
    assert v("battery_pct") == 80 and v("range_mi") == pytest.approx(222.94)
    assert v("charging_state") == "complete" and v("plugged_in") is True
    assert v("charge_power_kw") == 1 and v("charge_limit_pct") == 80
    assert v("charge_eta_min") == 0  # 'unavailable' timestamp while not charging
    assert v("charge_current_max_a") == 12 and v("charger_voltage_v") == 117
    assert v("locked") is False  # fixture: lock unlocked
    assert v("doors_open") == ["front_driver"]  # fixture: front driver door 'on'
    assert v("windows_open") == [] and v("trunk_open") is False and v("frunk_open") is False
    assert v("sentry_on") is False and v("user_present") is True
    assert v("climate_on") is True and v("target_temp_f") == 70.0
    assert v("cabin_temp_f") == pytest.approx(69.8) and v("outside_temp_f") == pytest.approx(68.9)
    assert v("location_zone") == "home"
    assert v("asleep") is False  # status connectivity entity is on
    assert v("software_update_available") is False and v("software_version") == "2026.1.1"
    assert v("charge_port_open") is True


def test_freshness_uses_ha_timestamps_and_unavailable_is_honest(ha):
    st = adapter(ha).fetch_state(NOW)
    assert st.age_s("battery_pct", NOW) is not None and st.age_s("battery_pct", NOW) > 0
    # tesla_fleet fixture has no speed/shift/odometer entities -> unavailable, not guessed
    for f in ("speed_mph", "shift_state", "odometer_mi"):
        assert st.is_available(f) is False, f
    # the fixture does carry a TeslaMate state entity ('suspended'): that is the only motion source -> not moving
    assert st.value("moving") is False
    # parked state is also inferred from being plugged in
    assert st.motion() is False


def test_unavailable_entity_state(ha):
    ha.set_state("sensor.demo_car_battery_level", "unavailable")
    st = adapter(ha).fetch_state(NOW)
    assert st.is_available("battery_pct") is False


def test_unit_conversion_from_metric_ha(ha):
    ha.set_state("sensor.demo_car_battery_range", "100", unit_of_measurement="km")
    ha.set_state("sensor.demo_car_inside_temperature", "20", unit_of_measurement="°C")
    st = adapter(ha).fetch_state(NOW)
    assert st.value("range_mi") == pytest.approx(62.14, abs=0.01)
    assert st.value("cabin_temp_f") == pytest.approx(68.0)


def test_overrides_and_missing_slug_and_token(ha):
    ha.states["sensor.other_soc"] = {"entity_id": "sensor.other_soc", "state": "42", "attributes": {},
                                     "last_reported": "2025-10-04T23:17:00+00:00"}
    st = adapter(ha, overrides={"battery_pct": "sensor.other_soc"}).fetch_state(NOW)
    assert st.value("battery_pct") == 42
    with pytest.raises(AdapterError, match="Car name"):
        adapter(ha, slug="")
    with pytest.raises(AdapterError, match="token"):
        adapter(ha, token=None)


def test_wrong_slug_yields_all_unavailable_not_crash(ha):
    st = adapter(ha, slug="nope").fetch_state(NOW)
    assert not st.has("battery_pct") and not st.has("locked")


def test_bad_token_and_unreachable_host_are_clean_errors(ha):
    with pytest.raises(AdapterError, match="rejected the credentials"):
        adapter(ha, token="wrong").fetch_state(NOW)
    a = HomeAssistantAdapter("http://127.0.0.1:9", "tok", load_profile("tesla_fleet"), "demo_car", timeout=1)
    with pytest.raises(AdapterUnavailable) as e:
        a.fetch_state(NOW)
    assert "tok" not in str(e.value)


def test_capabilities_follow_entities_present(ha):
    caps = adapter(ha).capabilities()
    assert "battery_pct" in caps.state_fields and "speed_mph" not in caps.state_fields
    assert {"lock", "unlock", "climate_on", "charge_limit", "honk", "seat_heat"} <= caps.commands
    # remove the frunk cover -> open_frunk disappears
    del ha.states["cover.demo_car_frunk"]
    a = adapter(ha)
    assert "open_frunk" not in a.capabilities().commands


def test_commands_send_correct_services(ha):
    a = adapter(ha)
    a.fetch_state(NOW)
    a.send_command("lock", {})
    a.send_command("set_temp", {"temp_f": 71})
    a.send_command("climate_on", {"temp_f": 68})
    a.send_command("charge_limit", {"percent": 90})
    a.send_command("charge_start", {})
    a.send_command("defrost", {"on": False})
    a.send_command("seat_heat", {"seat": "driver", "level": 2})
    a.send_command("honk", {})
    calls = ha.service_calls
    assert ("lock/lock", {"entity_id": "lock.demo_car_lock"}) in calls
    assert ("climate/set_temperature", {"entity_id": "climate.demo_car_climate", "temperature": 71.0}) in calls
    assert ("climate/set_hvac_mode", {"entity_id": "climate.demo_car_climate", "hvac_mode": "heat_cool"}) in calls
    assert ("number/set_value", {"entity_id": "number.demo_car_charge_limit", "value": 90}) in calls
    assert ("switch/turn_on", {"entity_id": "switch.demo_car_charge"}) in calls
    assert ("switch/turn_off", {"entity_id": "switch.demo_car_defrost"}) in calls
    assert ("select/select_option", {"entity_id": "select.demo_car_seat_heater_front_left", "option": "medium"}) in calls
    assert ("button/press", {"entity_id": "button.demo_car_honk_horn"}) in calls
    # read-back sees the effects
    st = a.fetch_state(NOW)
    assert st.value("locked") is True and st.value("charge_limit_pct") == 90


def test_celsius_ha_gets_converted_target(ha):
    ha.set_state("climate.demo_car_climate", "heat_cool", temperature_unit="°C")
    a = adapter(ha)
    a.fetch_state(NOW)
    a.send_command("set_temp", {"temp_f": 70})
    assert ha.service_calls[-1][1]["temperature"] == 21.0


def test_command_missing_entity_is_refused_before_sending(ha):
    del ha.states["lock.demo_car_lock"]
    a = adapter(ha)
    a.fetch_state(NOW)
    with pytest.raises(AdapterError, match="does not exist"):
        a.send_command("lock", {})
    assert ha.service_calls == []


def test_ha_service_error_is_reported(ha):
    ha.fail_services = True
    a = adapter(ha)
    a.fetch_state(NOW)
    with pytest.raises(AdapterError, match="internal error"):
        a.send_command("lock", {})


def test_no_token_in_error_messages_or_describe(ha):
    d = adapter(ha).describe()
    assert FakeHA.TOKEN not in json.dumps(d)


def test_r14_partly_unavailable_door_list_is_unknown_not_closed(ha):
    for eid in ("binary_sensor.demo_car_front_driver_door",):
        ha.set_state(eid, "off")
    ha.set_state("binary_sensor.demo_car_rear_driver_door", "unavailable")
    st = adapter(ha).fetch_state(NOW)
    assert st.is_available("doors_open") is False
    ha.set_state("binary_sensor.demo_car_front_driver_door", "on")       # an open door is still reported
    assert adapter(ha).fetch_state(NOW).value("doors_open") == ["front_driver"]
