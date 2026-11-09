"""Tire pressure (G5) and the disabled-by-default Home Assistant entities (G6): units, adapters, rules, surfaces."""

import json

import pytest

from fakes import FakeHA, load_fixture
from pit_crew import slash, tools
from pit_crew.adapters.homeassistant import HomeAssistantAdapter, parse_ha_time
from pit_crew.adapters.profile import load_profile, validate_profile
from pit_crew.adapters.discovery import discover
from pit_crew.brain import tires
from pit_crew.brain.models import FIELD_NAMES
from pit_crew.brain.units import PSI_PER_BAR, convert_by_dimension, convert_pressure

NOW = parse_ha_time("2025-10-05T00:17:00+00:00")
POS = {"fl": "front_left", "fr": "front_right", "rl": "rear_left", "rr": "rear_right"}


# -- units ------------------------------------------------------------------------------------------------
def test_pressure_units_convert_to_psi():
    assert convert_pressure(2.9, "bar") == pytest.approx(42.06, abs=0.01)
    assert convert_pressure(290, "kPa") == pytest.approx(42.06, abs=0.01)
    assert convert_pressure(2900, "hPa") == pytest.approx(42.06, abs=0.01)
    assert convert_pressure(2900, "mbar") == pytest.approx(42.06, abs=0.01)
    assert convert_pressure(41.0, "psi") == 41.0
    assert convert_by_dimension(2.9, "pressure", "bar") == pytest.approx(2.9 * PSI_PER_BAR)


@pytest.mark.parametrize("unit", [None, "", "furlongs", "inHg"])
def test_unknown_pressure_unit_is_refused_not_guessed(unit):
    """A bar value read as psi would be a 'flat tire': never guess."""
    with pytest.raises(ValueError):
        convert_pressure(2.9, unit)


# -- Home Assistant adapter -----------------------------------------------------------------------------
def ha_states(extra):
    base = load_fixture("ha_states_tesla_fleet.json")
    return base + [{"entity_id": eid, "state": st, "attributes": attrs,
                    "last_changed": "2025-10-05T00:16:00+00:00", "last_reported": "2025-10-05T00:16:30+00:00",
                    "last_updated": "2025-10-05T00:16:00+00:00"} for eid, st, attrs in extra]


def adapter(ha):
    return HomeAssistantAdapter(ha.url, FakeHA.TOKEN, load_profile("tesla_fleet"), "demo_car")


def test_profile_maps_the_four_tire_sensors_and_odometer_with_ha_core_entity_names():
    prof = load_profile("tesla_fleet")
    assert validate_profile(prof) == []
    f = prof["fields"]
    # verified against home-assistant/core homeassistant/components/tesla_fleet/sensor.py + strings.json
    assert f["tire_psi_fl"]["entity"] == "sensor.{slug}_tire_pressure_front_left"
    assert f["tire_psi_fr"]["entity"] == "sensor.{slug}_tire_pressure_front_right"
    assert f["tire_psi_rl"]["entity"] == "sensor.{slug}_tire_pressure_rear_left"
    assert f["tire_psi_rr"]["entity"] == "sensor.{slug}_tire_pressure_rear_right"
    assert f["odometer_mi"]["entity"] == "sensor.{slug}_odometer"
    for name in ("tire_psi_fl", "tire_psi_fr", "tire_psi_rl", "tire_psi_rr", "odometer_mi", "shift_state", "speed_mph"):
        assert f[name]["disabled_by_default"] is True and f[name]["enable_as"], name
    assert {f[n]["dimension"] for n in ("tire_psi_fl", "tire_psi_fr", "tire_psi_rl", "tire_psi_rr")} == {"pressure"}


def test_tire_pressures_in_psi_or_bar_and_odometer_in_miles_or_km():
    extra = [("sensor.demo_car_tire_pressure_front_left", "41.8", {"unit_of_measurement": "psi"}),
             ("sensor.demo_car_tire_pressure_front_right", "2.9", {"unit_of_measurement": "bar"}),
             ("sensor.demo_car_tire_pressure_rear_left", "290", {"unit_of_measurement": "kPa"}),
             ("sensor.demo_car_tire_pressure_rear_right", "2.8", {"unit_of_measurement": "bar"}),
             ("sensor.demo_car_odometer", "20000", {"unit_of_measurement": "km"})]
    ha = FakeHA(ha_states(extra))
    try:
        st = adapter(ha).fetch_state(NOW)
    finally:
        ha.stop()
    assert st.value("tire_psi_fl") == pytest.approx(41.8)
    assert st.value("tire_psi_fr") == pytest.approx(42.06, abs=0.01)
    assert st.value("tire_psi_rl") == pytest.approx(42.06, abs=0.01)
    assert st.value("tire_psi_rr") == pytest.approx(40.61, abs=0.01)
    assert st.value("odometer_mi") == pytest.approx(12427.4, abs=0.1)          # km -> mi
    assert st.age_s("tire_psi_fl", NOW) is not None
    pub = st.to_public(NOW)
    assert pub["tires"] == {"front_left_psi": 41.8, "front_right_psi": 42.1, "rear_left_psi": 42.1, "rear_right_psi": 40.6}


def test_unavailable_unknown_unit_and_garbage_tire_states_stay_unavailable():
    extra = [("sensor.demo_car_tire_pressure_front_left", "unavailable", {"unit_of_measurement": "psi"}),
             ("sensor.demo_car_tire_pressure_front_right", "2.9", {}),                         # no unit: not guessed
             ("sensor.demo_car_tire_pressure_rear_left", "n/a", {"unit_of_measurement": "psi"}),
             ("sensor.demo_car_tire_pressure_rear_right", "unknown", {"unit_of_measurement": "bar"})]
    ha = FakeHA(ha_states(extra))
    try:
        st = adapter(ha).fetch_state(NOW)
    finally:
        ha.stop()
    assert not any(st.has(f) for f in ("tire_psi_fl", "tire_psi_fr", "tire_psi_rl", "tire_psi_rr"))
    assert st.to_public(NOW)["tires"] == {}


def test_disabled_entities_are_explained_when_missing_and_silent_once_enabled():
    ha = FakeHA()
    try:
        a = adapter(ha)
        a.fetch_state(NOW)
        warns = a.setup_warnings()
        assert len(warns) == 1
        w = warns[0]
        for title in ("Odometer", "Tire pressure front left", "Tire pressure front right", "Tire pressure rear left",
                      "Tire pressure rear right"):
            assert title in w, title
        assert "Tesla Fleet" in w and "ships them disabled" in w and "turn on Enabled" in w
        assert "Shift state" not in w and "Speed" not in w                 # motion has its own warning
        assert "Tire pressure front left" in a.disabled_entity_hint(("tire_psi_fl",))
        assert "Odometer" not in a.disabled_entity_hint(("tire_psi_fl",))
    finally:
        ha.stop()
    every = [("sensor.demo_car_odometer", "100", {"unit_of_measurement": "mi"})] + [
        (f"sensor.demo_car_tire_pressure_{p}", "42", {"unit_of_measurement": "psi"}) for p in POS.values()]
    ha = FakeHA(ha_states(every))
    try:
        a = adapter(ha)
        a.fetch_state(NOW)
        assert a.setup_warnings() == []
    finally:
        ha.stop()


def test_discovery_says_which_disabled_entities_to_enable():
    res = discover(load_fixture("ha_states_tesla_fleet.json"))
    text = " ".join(res["warnings"])
    assert "Odometer" in text and "Tire pressure rear right" in text and "Settings → Devices & services → Tesla Fleet" in text
    full = ha_states([("sensor.demo_car_odometer", "1", {}), ("sensor.demo_car_shift_state", "p", {}), ("sensor.demo_car_speed", "0", {})]
                     + [(f"sensor.demo_car_tire_pressure_{p}", "42", {}) for p in POS.values()])
    assert discover(full)["warnings"] == []


def test_status_and_card_carry_the_enable_instructions(make_service):
    ha = FakeHA()
    try:
        svc = make_service({"adapter": "homeassistant", "ha_url": ha.url, "ha_slug": "demo_car", "ha_profile": "tesla_fleet"},
                           secrets={"PIT_CREW_HA_TOKEN": FakeHA.TOKEN})
        res = json.loads(tools.car_status({"refresh": True}))
        assert any("Odometer" in w and "Tire pressure front left" in w for w in res["warnings"])
        assert "Enable Odometer" in slash.handle("")
        rules = {r["id"]: r for r in svc.watcher.engine.describe_rules(svc.get_state())}
        assert "Tire pressure front left" in rules["tire_pressure_low"]["note"]
    finally:
        ha.stop()


def test_profile_validation_requires_enable_as_for_disabled_entities():
    prof = load_profile("tesla_fleet")
    prof["fields"]["odometer_mi"].pop("enable_as")
    assert any("enable_as" in p for p in validate_profile(prof))


# -- Teslemetry / Tessie ------------------------------------------------------------------------------------
def test_direct_tesla_tire_pressures_bar_to_psi_with_per_tire_freshness():
    from pit_crew.adapters.tesla_direct import tesla_data_to_state

    now = 1_790_000_000.0
    vs = {"timestamp": int(now * 1000), "odometer": 100.0,
          "tpms_pressure_fl": 2.9, "tpms_pressure_fr": 2.85, "tpms_pressure_rl": 2.8, "tpms_pressure_rr": None,
          "tpms_last_seen_pressure_time_fl": int(now - 30), "tpms_last_seen_pressure_time_fr": int(now - 7200),
          "tpms_rcp_front_value": 2.9, "tpms_rcp_rear_value": 2.8}
    st = tesla_data_to_state({"vehicle_state": vs}, now, name="t")
    assert st.value("tire_psi_fl") == pytest.approx(42.1, abs=0.05)
    assert st.value("tire_psi_fr") == pytest.approx(41.3, abs=0.05)
    assert not st.has("tire_psi_rr")                                       # null reading: unavailable, not 0
    assert st.value("tire_rec_front_psi") == pytest.approx(42.1, abs=0.05)
    assert st.value("tire_rec_rear_psi") == pytest.approx(40.6, abs=0.05)
    assert st.age_s("tire_psi_fl", now) == pytest.approx(30, abs=2)
    assert st.age_s("tire_psi_fr", now) == pytest.approx(7200, abs=2)     # an old tire reading looks old
    assert st.age_s("tire_psi_rl", now) == pytest.approx(0, abs=2)        # no last-seen: the block's own timestamp


# -- simulator ------------------------------------------------------------------------------------------------
def test_simulator_reports_tires_leaks_and_inflates(service):
    sim = service.adapter
    st = service.get_state(force=True)
    for f in ("tire_psi_fl", "tire_psi_fr", "tire_psi_rl", "tire_psi_rr", "tire_rec_front_psi"):
        assert st.has(f) and 35 < st.value(f) < 47, f
    sim.control("leak", tire="rr", psi_per_hour=12)
    sim.control("advance", minutes=60)
    st = service.get_state(force=True)
    assert st.value("tire_psi_rr") < st.value("tire_psi_rl") - 8
    sim.control("inflate")
    st = service.get_state(force=True)
    assert st.value("tire_psi_rr") == pytest.approx(st.value("tire_psi_fl"), abs=1.5)
    sim.control("low_tire", tire="front left", psi=29)
    assert service.get_state(force=True).value("tire_psi_fl") < 31
    with pytest.raises(Exception):
        sim.control("leak", tire="roof")
    assert "Simulator:" in slash.handle("sim leak rr 8") and "Simulator:" in slash.handle("sim low_tire fl 30")


def test_all_new_fields_are_normalized_names():
    assert {"tire_psi_fl", "tire_psi_fr", "tire_psi_rl", "tire_psi_rr", "tire_rec_front_psi", "tire_rec_rear_psi"} <= set(FIELD_NAMES)


# -- pure logic --------------------------------------------------------------------------------------------------
def test_thresholds_prefer_user_then_recommended_then_default(stub):
    from pit_crew.brain.models import VehicleState, FieldValue
    st = VehicleState("x", "x", 1000.0, {"tire_rec_front_psi": FieldValue(36.0, 1000.0), "tire_rec_rear_psi": FieldValue(2.9, 1000.0)})
    assert tires.low_threshold_psi(st, "front", 0) == pytest.approx(32.4)              # 90% of 36
    assert tires.low_threshold_psi(st, "rear", 0) == tires.DEFAULT_MIN_PSI             # 2.9 is not a plausible psi
    assert tires.low_threshold_psi(st, "front", 30) == 30.0                            # the user's number wins
    assert tires.low_threshold_psi(st, "front", "garbage") == pytest.approx(32.4)
    assert tires.low_threshold_psi(VehicleState("x", "x", 1.0), "rear", None) == 30.0           # nothing known: conservative
    tesla = VehicleState("x", "x", 1.0, hints={"tire_min_psi": 38.0})
    assert tires.low_threshold_psi(tesla, "rear", None) == 38.0                                 # model default from the adapter
    assert tires.low_threshold_psi(tesla, "rear", 33) == 33.0                                   # the user still wins
    assert tires.low_threshold_psi(VehicleState("x", "x", 1.0, hints={"tire_min_psi": "x"}), "rear", None) == 30.0


# -- watcher rules ---------------------------------------------------------------------------------------------------
@pytest.fixture
def svc(stub_service, stub):
    stub.values.update(tire_psi_fl=42.0, tire_psi_fr=42.0, tire_psi_rl=41.0, tire_psi_rr=41.0)
    stub.hints = {"tire_min_psi": 38.0}            # a Tesla-like car: the adapter says its model default is 38 psi
    return stub_service(armed=False, watcher_enabled=True)


def poll(s):
    return s.watcher.poll_once()


def open_alerts(s):
    return [a for a in s.alerts(("new", "surfaced", "acked")) if a["rule"].startswith("tire_")]


def test_healthy_tires_raise_nothing(svc, stub, clock):
    for _ in range(5):
        assert poll(svc)["raised"] == []
        clock.advance(120)
    assert open_alerts(svc) == []


def test_low_tire_waits_for_the_hold_then_alerts_once_with_safe_text(svc, stub, clock):
    stub.values["tire_psi_rr"] = 31.0
    assert poll(svc)["raised"] == []                                   # just began; hold is 10 min
    clock.advance(11 * 60)
    stub.values["tire_psi_rr"] = 31.0
    raised = poll(svc)["raised"]
    assert [a["rule"] for a in raised] == ["tire_pressure_low"]
    a = raised[0]
    assert a["title"] == "A tire is low on air" and "rear right 31 psi" in a["body"] and "below 38 psi" in a["body"]
    assert a["severity"] == "medium" and "Add air" in a["suggestion"]
    clock.advance(600)
    assert poll(svc)["raised"] == []                                   # cooldown/dedupe
    stub.values["tire_psi_rr"] = 41.0
    poll(svc)
    assert open_alerts(svc) == []                                      # topped up: resolved


def test_low_tire_uses_the_cars_recommended_pressure_and_the_users_override(svc, stub, clock):
    # A car that recommends 33 psi: 90% is 29.7, so 31 / 30.5 psi are fine even though they're under the 38 psi fallback.
    stub.values.update(tire_rec_front_psi=33.0, tire_rec_rear_psi=33.0, tire_psi_fl=31.0, tire_psi_rr=30.5)
    for _ in range(3):
        assert poll(svc)["raised"] == []
        clock.advance(11 * 60)
    assert open_alerts(svc) == []
    # The user knows better and sets 32: now both are low.
    svc.watcher.engine.set_rule("tire_pressure_low", min_psi=32)
    poll(svc); clock.advance(11 * 60)
    raised = poll(svc)["raised"]
    assert len(raised) == 1 and raised[0]["rule"] == "tire_pressure_low"
    assert "front left 31 psi" in raised[0]["body"] and "rear right 30 psi" in raised[0]["body"]   # 30.5 formats as 30
    assert "below 32 psi" in raised[0]["body"]


def test_very_low_tire_is_high_severity(svc, stub, clock):
    stub.values["tire_psi_fr"] = 22.0
    poll(svc); clock.advance(11 * 60)
    assert poll(svc)["raised"][0]["severity"] == "high"


@pytest.mark.parametrize("bad", [None, 0.0, 2.9, 4.0, 300.0, -3.0, float("nan")])
def test_unavailable_or_implausible_readings_never_alert(svc, stub, clock, bad):
    """bar-looking numbers, zeros, NaN and absurd values are sensor/unit faults, not a flat tire."""
    stub.values["tire_psi_fl"] = bad
    if bad is None:
        stub.removed_fields.add("tire_psi_fl")
    for _ in range(4):
        poll(svc)
        clock.advance(11 * 60)
    assert open_alerts(svc) == []


def test_stale_readings_never_alert_and_do_not_resolve_an_open_alert(svc, stub, clock):
    stub.values["tire_psi_rl"] = 30.0
    poll(svc); clock.advance(11 * 60)
    stub.values["tire_psi_rl"] = 30.0
    assert len(poll(svc)["raised"]) == 1
    # the sensor stops reporting: the reading ages beyond stale_after_s (R16 pattern)
    stub.ages["tire_psi_rl"] = 3 * 3600.0
    clock.advance(11 * 60)
    assert poll(svc)["raised"] == []
    assert len(open_alerts(svc)) == 1                                  # kept as is, neither re-raised nor resolved


def test_stale_low_tire_from_the_start_is_silent(svc, stub, clock):
    stub.values["tire_psi_rl"] = 20.0
    stub.ages["tire_psi_rl"] = 5 * 3600.0
    for _ in range(3):
        assert poll(svc)["raised"] == []
        clock.advance(11 * 60)
    assert open_alerts(svc) == []


def test_fast_loss_alerts_on_a_drop_inside_the_window_only(svc, stub, clock):
    poll(svc)
    for psi in (41.0, 40.2, 39.0, 37.5):                               # 41 -> 37.5 over 8 min: 3.5 psi
        clock.advance(120)
        stub.values["tire_psi_rr"] = psi
        poll(svc)
    fast = [a for a in svc.alerts(("new", "surfaced")) if a["rule"] == "tire_fast_loss"]
    assert len(fast) == 1
    assert fast[0]["severity"] == "high" and "Rear right fell 3.5 psi (41 to 37.5) within 30 min" in fast[0]["body"]
    assert "puncture" in fast[0]["suggestion"]


def test_slow_drift_is_not_fast_loss(svc, stub, clock):
    for i in range(12):                                                # 1 psi per 20 minutes: 3 psi over an hour
        clock.advance(20 * 60)
        stub.values["tire_psi_fl"] = 42.0 - i * 0.25
        poll(svc)
    assert [a for a in svc.alerts(("new", "surfaced", "acked")) if a["rule"] == "tire_fast_loss"] == []


def test_fast_loss_ignores_a_single_tire_that_is_simply_unavailable(svc, stub, clock):
    poll(svc)
    clock.advance(120)
    stub.removed_fields.add("tire_psi_rr")
    assert poll(svc)["raised"] == []
    clock.advance(120)
    stub.removed_fields.discard("tire_psi_rr")                         # comes back at its old value: no loss
    assert poll(svc)["raised"] == []


def test_fast_loss_thresholds_are_configurable_and_resolve_after_the_window(svc, stub, clock):
    svc.watcher.engine.set_rule("tire_fast_loss", drop_psi=1.5, window_min=10)
    poll(svc)
    clock.advance(180); stub.values["tire_psi_fl"] = 40.0
    assert [a["rule"] for a in poll(svc)["raised"]] == ["tire_fast_loss"]
    for _ in range(8):                                                 # stable at the new level: the window slides past the drop
        clock.advance(180)
        poll(svc)
    assert [a for a in svc.alerts(("new", "surfaced")) if a["rule"] == "tire_fast_loss"] == []


def test_cars_without_tire_data_skip_the_rules_honestly(stub_service, stub):
    s = stub_service(armed=False, watcher_enabled=True)
    for f in ("tire_psi_fl", "tire_psi_fr", "tire_psi_rl", "tire_psi_rr"):
        stub.removed_fields.add(f)
    st = s.get_state(force=True)
    notes = {r["id"]: r.get("note") for r in s.watcher.engine.describe_rules(st)}
    assert "tire pressure" in notes["tire_pressure_low"] and "tire pressure" in notes["tire_fast_loss"]
    assert poll(s)["raised"] == []


def test_status_card_shows_tires_and_marks_a_low_one(svc, stub):
    stub.values["tire_psi_rr"] = 30.0
    card = slash.handle("")
    assert "Tires (psi): FL 42 · FR 42 · RL 41 · RR 30 LOW" in card
    res = json.loads(tools.car_status({"refresh": True}))
    assert res["tires"]["rear_right_psi"] == 30.0 and "tire_psi_rr" not in json.dumps(res["tires"])


# -- pages -----------------------------------------------------------------------------------------------------
from pagerender import backend_summary, needs_desktop_render, needs_web_render, render, render_web  # noqa: E402


@needs_desktop_render
def test_desktop_page_shows_a_tires_tile_and_the_leak_demo_button(service, tmp_path):
    service.adapter.control("low_tire", tire="rr", psi=31)
    mod, summary = backend_summary(service)
    out = render(tmp_path, summary, rules=mod.rules())
    page = out["page"]
    assert "Tires" in page and "psi lowest" in page and "RR 31" in page and "FL 4" in page
    assert "Leak a tire" in page and "Pump up tires" in page and "Low tire pressure" in page


@needs_web_render
def test_web_page_shows_a_tires_tile(service, tmp_path):
    service.adapter.control("low_tire", tire="rr", psi=31)
    mod, summary = backend_summary(service)
    out = render_web(tmp_path, {"/summary": summary, "/log": {"ok": True, "rows": []}})
    assert "Tires" in out["html"] and "31 psi lowest" in out["html"] and "RR 31" in out["html"]


def test_generic_cars_without_a_recommendation_use_the_conservative_30_psi_default(svc, stub, clock):
    """A Kia/Hyundai/other car reports no recommended pressure: its 35 psi placard must not be a standing 'low tire'."""
    stub.hints = {}
    stub.values.update(tire_psi_fl=35.0, tire_psi_fr=35.0, tire_psi_rl=34.0, tire_psi_rr=34.0)
    for _ in range(4):
        assert poll(svc)["raised"] == []
        clock.advance(11 * 60)
    stub.values["tire_psi_rl"] = 27.0
    poll(svc); clock.advance(11 * 60)
    raised = poll(svc)["raised"]
    assert raised and "rear left 27 psi" in raised[0]["body"] and "below 30 psi" in raised[0]["body"]


def test_home_assistant_tesla_profile_and_tesla_api_supply_the_tesla_default():
    from pit_crew.adapters.tesla_direct import tesla_data_to_state
    assert load_profile("tesla_fleet")["tire_min_psi_default"] == 38
    assert tesla_data_to_state({"vehicle_state": {}}, 1_790_000_000.0, name="t").hints["tire_min_psi"] == 38.0
    ha = FakeHA(ha_states([]))
    try:
        assert adapter(ha).fetch_state(NOW).hints["tire_min_psi"] == 38.0
    finally:
        ha.stop()
    bad = load_profile("tesla_fleet")
    bad["tire_min_psi_default"] = 3
    assert any("tire_min_psi_default" in p for p in validate_profile(bad))


def test_fast_loss_ignores_readings_taken_while_driving(svc, stub, clock):
    """A tire that is hot from the road and cools after the stop is not a leak: moving samples are not recorded."""
    poll(svc)
    stub.values.update(moving=True, speed_mph=65.0, shift_state="D", tire_psi_rr=45.0)
    clock.advance(120); poll(svc)
    stub.values.update(moving=False, speed_mph=0.0, shift_state="P", tire_psi_rr=41.0)
    clock.advance(120); poll(svc)
    assert [a for a in svc.alerts(("new", "surfaced")) if a["rule"] == "tire_fast_loss"] == []
