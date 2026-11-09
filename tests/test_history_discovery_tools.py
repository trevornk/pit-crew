import json

import pytest

from fakes import FakeHA, FakeTeslaMate, load_fixture
from pit_crew import tools
from pit_crew.adapters.base import ChargeRecord, DriveRecord
from pit_crew.adapters.discovery import candidate_slugs, discover, propose_generic
from pit_crew.adapters.profile import load_profile
from pit_crew.brain import history as H


def D(start, miles, kwh, temp, mins=20, s0=70, s1=65):
    return DriveRecord(start=start, end=start + mins * 60, miles=miles, duration_min=mins, start_pct=s0, end_pct=s1,
                       kwh=kwh, wh_per_mi=kwh * 1000 / miles if miles else None, outside_temp_f=temp)


def test_weighted_efficiency_and_buckets():
    rows = [D(1, 10, 2.5, 60), D(2, 10, 3.5, 20), D(3, 30, 9.0, 25)]
    assert H.weighted_wh_per_mi(rows) == pytest.approx((2.5 + 3.5 + 9) / 50 * 1000)
    buckets = {b["temp_range"]: b for b in H.efficiency_by_temperature(rows)}
    assert buckets["under 32°F"]["trips"] == 2 and buckets["under 32°F"]["wh_per_mi"] == pytest.approx(312.5)
    assert H.efficiency_near_temperature(rows, 22, min_trips=3) == (None, 2)
    assert H.efficiency_near_temperature(rows, 22, min_trips=2)[0] == pytest.approx(312.5)


def test_summaries_ignore_tiny_drives_and_compute_in_code():
    drives = [D(1, 10, 2.5, 60), D(2, 0.0, 0.01, 60, mins=0), D(3, 20, 5.0, 60)]
    s = H.summarize_drives(drives)
    assert s["trips"] == 2 and s["miles"] == 30 and s["wh_per_mi"] == 250 and s["longest_trip_mi"] == 20
    charges = [ChargeRecord(start=1, end=2, kwh_added=10, kwh_used=11, cost=1.1, start_pct=50, end_pct=80),
               ChargeRecord(start=3, end=4, kwh_added=20, kwh_used=22, cost=2.2, start_pct=40, end_pct=80)]
    c = H.summarize_charges(charges)
    assert c["sessions"] == 2 and c["kwh_added"] == 30 and c["total_cost_usd"] == 3.3
    assert c["charging_efficiency_pct"] == 91 and c["avg_cost_per_kwh_usd"] == 0.1
    assert H.summarize_charges([]) == {"sessions": 0}


def test_history_report_without_provider():
    out = H.history_report(None, "summary", 1e9)
    assert out["ok"] is False and "history" in out["hint"]


def test_car_history_tool_on_simulator(service):
    out = json.loads(tools.car_history({"kind": "summary", "days": 30}))
    assert out["ok"] and out["driving"]["trips"] > 5 and out["charging"]["sessions"] >= 1
    assert out["battery_health"]["health_pct"] > 90 and out["source"] == "Pit Crew recorded history + Simulated history"
    trips = json.loads(tools.car_history({"kind": "trips", "days": 14}))
    assert trips["ok"] and 0 < len(trips["trips"]) <= 25
    assert json.loads(tools.car_history({"kind": "bogus"}))["ok"] is False
    assert json.loads(tools.car_history({"start_date": "garbage"}))["ok"] is False


def test_car_history_with_teslamate_provider_and_ha_live(make_service):
    tm = FakeTeslaMate()
    ha = FakeHA()
    try:
        make_service({"adapter": "homeassistant", "history": "teslamate", "ha_url": ha.url, "ha_slug": "demo_car",
                      "teslamate_url": tm.url, "teslamate_car_id": 1},
                     secrets={"PIT_CREW_HA_TOKEN": FakeHA.TOKEN})
        # fixture drives are from early Oct 2025; use an explicit window
        out = json.loads(tools.car_history({"kind": "summary", "start_date": "2025-09-28", "end_date": "2025-10-06"}))
        assert out["ok"] and out["driving"]["trips"] == 3  # 2 of the 5 fixture drives are 0-minute stubs
        assert out["charging"]["sessions"] == 5 and out["charging"]["kwh_added"] == pytest.approx(15.2, abs=0.05)
        assert out["battery_health"]["health_pct"] == pytest.approx(90.1, abs=0.1)
        status = json.loads(tools.car_status({}))
        assert status["ok"] and status["vehicle"]["simulated"] is False and status["battery"]["battery_pct"] == 80
        assert "REDACTED" not in json.dumps(status)
        assert {m for m, *_ in tm.requests} == {"GET"}
    finally:
        tm.stop()
        ha.stop()


def test_car_readiness_tool(service):
    out = json.loads(tools.car_readiness({"departure": "tomorrow 7:30 AM", "trip_miles": 25}))
    assert out["ok"] and out["verdict"] in ("ready", "tight", "not_ready")
    assert out["vehicle"]["simulated"] is True and "assumptions" in out and out["assumptions"]["efficiency"]
    far = json.loads(tools.car_readiness({"departure": "tomorrow 7:30 AM", "trip_miles": 400, "round_trip": True}))
    assert far["verdict"] == "not_ready" and far["recommendations"]
    bad = json.loads(tools.car_readiness({"departure": "whenever"}))
    assert bad["ok"] is False and "hint" in bad
    assert json.loads(tools.car_readiness({"trip_miles": "abc"}))["ok"] is False
    assert json.loads(tools.car_readiness({"trip_miles": 9000}))["ok"] is False


def test_discovery_ranks_tesla_fleet_and_finds_slug():
    states = load_fixture("ha_states_tesla_fleet.json")
    result = discover(states)
    best = result["proposals"][0]
    assert best["profile"] == "tesla_fleet" and best["slug"] == "demo_car" and best["coverage"] >= 0.85
    assert "lock" in best["commands_available"] and best["tested"] is True
    assert result["recommended"] == {"ha_profile": "tesla_fleet", "ha_slug": "demo_car", "note": "tested profile"}
    assert "teslamate_demo_car" not in {p["slug"] for p in result["proposals"][:1]}
    assert "demo_car" in candidate_slugs(load_profile("tesla_fleet"), [s["entity_id"] for s in states])


def test_discovery_generic_for_unknown_brand():
    states = [
        {"entity_id": "sensor.ioniq_soc", "state": "55", "attributes": {"device_class": "battery", "unit_of_measurement": "%"}},
        {"entity_id": "lock.ioniq_doors", "state": "locked", "attributes": {}},
        {"entity_id": "climate.ioniq_hvac", "state": "off", "attributes": {}},
        {"entity_id": "device_tracker.ioniq", "state": "home", "attributes": {}},
        {"entity_id": "binary_sensor.ioniq_front_door", "state": "off", "attributes": {"device_class": "door"}},
        {"entity_id": "sensor.house_temp", "state": "70", "attributes": {"device_class": "temperature"}},
    ]
    res = discover(states)
    assert res["recommended"] is None
    gen = res["generic_proposal"]["entity_overrides"]
    assert gen["battery_pct"] == "sensor.ioniq_soc" and gen["locked"] == "lock.ioniq_doors"
    assert gen["climate_on"] == "climate.ioniq_hvac" and gen["location_zone"] == "device_tracker.ioniq"
    assert "cabin_temp_f" not in gen  # a house thermometer is not a cabin sensor
    assert res["generic_proposal"]["doors"] == ["binary_sensor.ioniq_front_door"]


def test_service_discover_ha_via_fake_server(make_service):
    ha = FakeHA()
    try:
        svc = make_service({"adapter": "simulator", "ha_url": ha.url}, secrets={"PIT_CREW_HA_TOKEN": FakeHA.TOKEN})
        out = svc.discover_ha()
        assert out["ok"] and out["recommended"]["ha_slug"] == "demo_car"
        assert {m for m, *_ in ha.requests} == {"GET"}  # discovery never writes
        svc2 = make_service({"ha_url": ha.url}, secrets={})
        assert svc2.discover_ha()["ok"] is False
    finally:
        ha.stop()
