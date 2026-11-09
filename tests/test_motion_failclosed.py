"""Fail-closed motion handling (live finding: HA ships Shift state / Speed disabled, so motion was unknown)."""

import copy
import json
from datetime import datetime, timezone

import pytest

from fakes import FakeHA, load_fixture
from pit_crew import slash, tools
from pit_crew.adapters.discovery import discover
from pit_crew.adapters.homeassistant import HomeAssistantAdapter, parse_ha_time
from pit_crew.adapters.profile import load_profile
from pit_crew.brain.models import VehicleState
from pit_crew.safety.approval import ApprovalEvidence
from pit_crew.settings import DEFAULTS

NOW = parse_ha_time("2025-10-05T00:17:00+00:00")
HUMAN = ApprovalEvidence("hermes_approval", "once")
SPEED = "sensor.demo_car_speed"
SHIFT = "sensor.demo_car_shift_state"
TM = "sensor.teslamate_demo_car_state"


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


@pytest.fixture
def ha():
    fake = FakeHA()
    fresh(fake)
    # unplugged, like a car parked at the curb or in a lot
    fake.set_state("binary_sensor.demo_car_charge_cable", "off")
    fake.set_state("sensor.demo_car_charging", "disconnected")
    fake.states.pop(TM)
    yield fake
    fake.stop()


def fresh(fake, age_s=10):
    for obj in fake.states.values():
        obj["last_reported"] = obj["last_updated"] = iso(NOW - age_s)


def add(fake, eid, state, age_s=10, **attrs):
    fake.states[eid] = {"entity_id": eid, "state": state, "attributes": attrs, "last_reported": iso(NOW - age_s),
                        "last_updated": iso(NOW - age_s)}


def service(make_service, ha, clock, **over):
    clock.t = NOW
    svc = make_service({"adapter": "homeassistant", "history": "none", "ha_url": ha.url, "ha_slug": "demo_car", **over},
                       secrets={"PIT_CREW_HA_TOKEN": FakeHA.TOKEN})
    svc.arm(1, by="t")
    return svc


# -- settings / model ----------------------------------------------------------------------------------------------
def test_default_is_fail_closed():
    assert DEFAULTS["allow_unknown_motion"] is False
    import yaml
    from pathlib import Path
    schema = yaml.safe_load((Path(__file__).resolve().parents[1] / "plugin.yaml").read_text())["config_schema"]
    assert schema["allow_unknown_motion"]["default"] is False


def vs(**fields):
    st = VehicleState(name="x", source="t", captured_at=NOW)
    for k, (v, age) in fields.items():
        st.set(k, v, NOW - age)
    return st


def test_motion_needs_a_fresh_parked_indicator():
    assert vs(speed_mph=(0.0, 10)).motion(NOW, 600) is False
    assert vs(speed_mph=(0.0, 3000)).motion(NOW, 600) is None            # stale "parked" proves nothing
    assert vs(shift_state=("P", 3000)).motion(NOW, 600) is None
    assert vs(plugged_in=(True, 3000)).motion(NOW, 600) is None
    assert vs(plugged_in=(True, 20)).motion(NOW, 600) is False
    assert vs(speed_mph=(55.0, 3000)).motion(NOW, 600) is True           # a stale "moving" still blocks
    assert vs(speed_mph=(0.0, 10), shift_state=("D", 10)).motion(NOW, 600) is True      # any source saying moving wins
    assert vs().motion(NOW, 600) is None
    assert vs(speed_mph=(0.0, 3000)).motion() is False                    # legacy call without freshness unchanged


# -- policy through the engine, against the real tesla_fleet fixture shapes ---------------------------------------
def test_unplugged_car_without_motion_entities_is_refused_with_actionable_message(make_service, ha, clock):
    svc = service(make_service, ha, clock)
    for cmd in ("unlock", "open_trunk", "open_frunk", "vent_windows", "honk"):
        res = svc.run_command(cmd, {}, surface="test", approval=HUMAN)
        assert res["status"] == "blocked", cmd
    msg = svc.run_command("unlock", {}, surface="test", approval=HUMAN)["message"]
    for needle in ("cannot confirm the car is parked", "Shift state", "Speed", "Settings → Devices & services",
                   "disabled entities", "moving_state_entity", "allow_unknown_motion"):
        assert needle in msg, needle
    assert ha.service_calls == []                                          # nothing reached the car


def test_plugged_in_car_is_still_provably_parked(make_service, ha, clock):
    ha.set_state("binary_sensor.demo_car_charge_cable", "on"); fresh(ha)
    ha.states.pop(TM, None)
    svc = service(make_service, ha, clock)
    assert svc.run_command("unlock", {}, surface="test", approval=HUMAN)["status"] == "confirmed"


def test_enabled_speed_and_shift_entities_prove_parked_or_moving(make_service, ha, clock):
    add(ha, SPEED, "0", unit_of_measurement="mph", device_class="speed")
    add(ha, SHIFT, "p")
    svc = service(make_service, ha, clock)
    assert svc.run_command("unlock", {}, surface="test", approval=HUMAN)["status"] == "confirmed"
    ha.set_state(SPEED, "42"); ha.set_state(SHIFT, "d"); fresh(ha)
    clock.advance(130)                       # past the dedupe window
    fresh(ha, age_s=5)
    ha.states["lock.demo_car_lock"]["state"] = "locked"
    res = svc.run_command("unlock", {}, surface="test", approval=HUMAN)
    assert res["status"] == "blocked" and "moving" in res["message"]


def test_speed_in_kmh_is_converted_and_stale_motion_readings_do_not_count(make_service, ha, clock):
    add(ha, SPEED, "100", unit_of_measurement="km/h", device_class="speed")
    svc = service(make_service, ha, clock)
    st = svc.get_state(force=True)
    assert st.value("speed_mph") == pytest.approx(62.1, abs=0.1) and st.value("moving") is True
    ha.set_state(SPEED, "0"); fresh(ha)
    ha.states[SPEED]["last_reported"] = iso(NOW - 4000)                    # the speed sensor stopped reporting long ago
    for e in ("sensor.demo_car_battery_level",):
        ha.states[e]["last_reported"] = iso(NOW - 5)
    # Pass 8 (R1): while the car is online an unchanged value is confirmed by the integration's poll, so "stale"
    # now means the car is offline/asleep and per-entity stamps apply.
    ha.set_state("binary_sensor.demo_car_status", "off"); ha.states["binary_sensor.demo_car_status"]["last_reported"] = iso(NOW - 5)
    clock.advance(130)
    res = svc.run_command("unlock", {}, surface="test", approval=HUMAN)
    assert res["status"] == "blocked" and "cannot confirm the car is parked" in res["message"]


def test_teslamate_state_entity_as_extra_motion_source(make_service, ha, clock):
    add(ha, TM, "driving")
    svc = service(make_service, ha, clock)
    assert svc.get_state(force=True).value("moving") is True
    assert "moving" in svc.run_command("unlock", {}, surface="test", approval=HUMAN)["message"]
    ha.set_state(TM, "online"); fresh(ha)
    clock.advance(130); fresh(ha, age_s=5)
    assert svc.run_command("unlock", {}, surface="test", approval=HUMAN)["status"] == "confirmed"      # fresh, not driving
    # stale TeslaMate state => unknown again => refused
    ha.states["lock.demo_car_lock"]["state"] = "locked"
    ha.states[TM]["last_reported"] = iso(NOW - 3000)
    ha.set_state("binary_sensor.demo_car_status", "off")        # Pass 8 (R1): offline car => per-entity stamps apply
    clock.advance(130)
    fresh_except = [e for e in ha.states if e != TM]
    for e in fresh_except:
        ha.states[e]["last_reported"] = iso(NOW + 100)
    assert svc.run_command("unlock", {}, surface="test", approval=HUMAN)["status"] == "blocked"


def test_moving_state_entity_override_in_settings(make_service, ha, clock):
    add(ha, "sensor.my_tm_state", "driving")
    svc = service(make_service, ha, clock, ha_entity_overrides={"moving_state_entity": "sensor.my_tm_state"})
    assert svc.get_state(force=True).value("moving") is True


def test_opt_in_allows_unknown_motion_with_a_warning(make_service, ha, clock):
    svc = service(make_service, ha, clock, allow_unknown_motion=True)
    res = svc.run_command("unlock", {}, surface="test", approval=HUMAN)
    assert res["status"] == "confirmed"
    assert any("cannot confirm the car is parked" in w for w in res["policy"]["warnings"])


# -- discovery / status / slash / Desktop -------------------------------------------------------------------------
def test_discovery_warns_when_motion_entities_are_absent():
    states = load_fixture("ha_states_tesla_fleet.json")
    no_tm = [s for s in states if s["entity_id"] != TM]
    res = discover(no_tm)
    assert res["recommended"]["ha_slug"] == "demo_car"
    assert res["warnings"] and "Shift state" in res["warnings"][0] and SPEED in res["warnings"][0]
    assert discover(states)["warnings"] == []                              # TeslaMate state entity present = a motion source
    with_both = no_tm + [{"entity_id": SPEED, "state": "0", "attributes": {}}, {"entity_id": SHIFT, "state": "p", "attributes": {}}]
    assert discover(with_both)["warnings"] == []


def test_slash_discover_prints_the_warning(make_service, ha, clock):
    svc = service(make_service, ha, clock, ha_slug="")
    out = slash.handle("discover")
    assert "WARNING" in out and "Shift state" in out


def test_status_and_card_carry_the_warning(make_service, ha, clock):
    svc = service(make_service, ha, clock)
    out = json.loads(tools.car_status({}))
    assert out["ok"] and out["warnings"] and "Shift state" in out["warnings"][0]
    assert "⚠" in slash.handle("")
    add(ha, SPEED, "0"); add(ha, SHIFT, "p")
    svc.adapter._refresh_states()
    assert "warnings" not in json.loads(tools.car_status({"refresh": True}))


def test_generic_adapter_without_motion_data_gets_a_generic_warning(stub_service, stub):
    for k in ("moving", "speed_mph", "shift_state"):
        stub.values.pop(k)
    svc = stub_service()
    out = json.loads(tools.car_status({}))
    assert out["warnings"] and "reports no speed, gear or driving state" in out["warnings"][0]


@pytest.mark.parametrize("state", ["asleep", "online", "charging", "suspended", "updating", "Online"])
def test_only_allowlisted_teslamate_states_prove_parked(make_service, ha, clock, state):
    add(ha, TM, state)
    svc = service(make_service, ha, clock)
    assert svc.get_state(force=True).value("moving") is False
    assert svc.run_command("unlock", {}, surface="test", approval=HUMAN)["status"] == "confirmed"


@pytest.mark.parametrize("state", ["offline", "start", "rebooting", "some_future_value", "", "unavailable", "unknown"])
def test_other_teslamate_states_are_no_vote_and_unlock_is_refused(make_service, ha, clock, state):
    add(ha, TM, state)
    svc = service(make_service, ha, clock)
    st = svc.get_state(force=True)
    assert not st.has("moving")                          # no vote, not "parked"
    res = svc.run_command("unlock", {}, surface="test", approval=HUMAN)
    assert res["status"] == "blocked" and "cannot confirm the car is parked" in res["message"]
    assert ha.service_calls == []


def test_offline_does_not_override_other_fresh_parked_evidence_but_driving_still_wins(make_service, ha, clock):
    add(ha, TM, "offline"); add(ha, SPEED, "0", unit_of_measurement="mph")
    svc = service(make_service, ha, clock)
    assert svc.get_state(force=True).value("moving") is False       # speed says parked; offline abstains
    assert svc.run_command("unlock", {}, surface="test", approval=HUMAN)["status"] == "confirmed"
    ha.set_state(TM, "driving"); fresh(ha)
    assert svc.get_state(force=True).value("moving") is True
