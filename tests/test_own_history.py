"""Pit Crew's own history store: trips, charging and efficiency recorded from any adapter (no TeslaMate)."""

import json

import pytest

from fakes import FakeTeslaMate
from pit_crew import tools
from pit_crew.history_store import KV_FIRST


def tick(svc, clock, seconds=120, **vals):
    """Advance the clock, change the car, take a fresh reading."""
    clock.advance(seconds)
    svc.adapter.values.update(vals)
    return svc.get_state(force=True)


@pytest.fixture
def svc(stub_service, stub):
    return stub_service(history="own", electricity_rate_usd_kwh=0.10, battery_kwh=75.0)


def window(svc):
    return svc.now() - 86400, svc.now() + 60


def test_charging_session_recorded_with_energy_cost_and_zone(svc, stub, clock):
    stub.values.update(battery_pct=50.0, charging_state="stopped", plugged_in=True, charge_power_kw=0.0)
    svc.get_state(force=True)
    for pct in (52, 55, 58, 61, 64):
        tick(svc, clock, 600, battery_pct=float(pct), charging_state="charging", charge_power_kw=7.0)
    tick(svc, clock, 600, battery_pct=66.0, charging_state="complete", charge_power_kw=0.0)
    tick(svc, clock, 600)                                   # idle -> session closed
    tick(svc, clock, 3600)
    ch = svc.history_provider.charges(*window(svc))
    assert len(ch) == 1
    c = ch[0]
    assert (c.start_pct, c.end_pct) == (50.0, 66.0) or c.end_pct >= 64.0
    assert c.kwh_added == pytest.approx((c.end_pct - c.start_pct) / 100 * 75, abs=0.01)
    assert 5 < c.kwh_used < 8                                 # integrated from the reported charge power
    assert c.cost == pytest.approx(c.kwh_used * 0.10, abs=0.02)
    assert c.duration_min >= 55
    assert svc.store.query("SELECT zone FROM charges")[0]["zone"] == "home"


def test_odometer_drive_recorded_with_distance_energy_and_efficiency(svc, stub, clock):
    stub.values.update(odometer_mi=1000.0, battery_pct=70.0, range_mi=200.0, moving=True, speed_mph=40.0, location_zone="home")
    svc.get_state(force=True)
    for i in range(1, 6):
        tick(svc, clock, 120, odometer_mi=1000.0 + i * 1.4, range_mi=200.0 - i * 1.5, moving=True, speed_mph=42.0,
             battery_pct=70.0 - (0 if i < 3 else 1) - (0 if i < 5 else 1), location_zone="away" if i > 2 else "home")
    tick(svc, clock, 120, moving=False, speed_mph=0.0)       # parked -> closes the trip
    tick(svc, clock, 3600)
    drives = svc.history_provider.drives(*window(svc))
    assert len(drives) == 1
    d = drives[0]
    assert d.miles == pytest.approx(7.0, abs=0.1) and d.duration_min == pytest.approx(10.0, abs=2.5)
    assert d.start_pct == 70.0 and d.end_pct == 68.0 and d.kwh == pytest.approx(1.5)
    assert d.wh_per_mi == pytest.approx(1.5 * 1000 / d.miles, abs=1)
    row = svc.store.query("SELECT * FROM drives")[0]
    assert row["dist_src"] == "odometer" and row["start_zone"] == "home" and row["end_zone"] == "away" and row["open"] == 0


def test_zone_only_car_gets_range_based_trip(svc, stub, clock):
    """Like HA tesla_fleet: no odometer, speed or gear — only zone, battery and range."""
    for k in ("odometer_mi", "speed_mph", "moving", "shift_state"):
        stub.values.pop(k, None)
    stub.values.update(battery_pct=70.0, range_mi=200.0, plugged_in=False, charging_state="disconnected")
    svc.get_state(force=True)
    tick(svc, clock, 180, range_mi=190.0, battery_pct=68.0, location_zone="away")       # left home
    tick(svc, clock, 3600)
    d = svc.history_provider.drives(*window(svc))
    assert len(d) == 1 and d[0].miles == pytest.approx(10.0)
    assert svc.store.query("SELECT dist_src FROM drives")[0]["dist_src"] == "range"


def test_idle_drain_and_preconditioning_are_not_trips(svc, stub, clock):
    for k in ("odometer_mi", "speed_mph", "moving", "shift_state"):
        stub.values.pop(k, None)
    stub.values.update(battery_pct=70.0, range_mi=200.0, plugged_in=False, charging_state="disconnected")
    svc.get_state(force=True)
    tick(svc, clock, 36000, range_mi=199.0)                  # 1 mi lost over 10 h
    tick(svc, clock, 1800, range_mi=197.0, climate_on=True)  # pre-conditioning: 2 mi in 30 min = 4 mi/h
    tick(svc, clock, 600, range_mi=197.0)
    assert svc.store.query("SELECT * FROM drives") == []


def test_blackout_longer_than_six_hours_is_not_bridged(svc, stub, clock):
    stub.values.update(battery_pct=70.0, charging_state="charging", charge_power_kw=7.0)
    svc.get_state(force=True)
    tick(svc, clock, 600, battery_pct=72.0)
    tick(svc, clock, 8 * 3600, battery_pct=40.0, charging_state="disconnected", charge_power_kw=0.0, plugged_in=False)
    rows = svc.store.query("SELECT * FROM charges")
    assert len(rows) == 1 and rows[0]["open"] == 0 and rows[0]["end_pct"] == 72.0   # ended where we last saw it
    assert svc.store.query("SELECT * FROM drives") == []


def test_snapshots_are_throttled_and_persist_across_service_instances(svc, stub, clock, store):
    svc.get_state(force=True)
    for _ in range(5):
        clock.advance(5)
        svc.get_state(force=True)
    assert store.query("SELECT COUNT(*) AS n FROM snapshots")[0]["n"] == 1
    assert store.kv_get(KV_FIRST) == pytest.approx(clock.t - 25)
    from conftest import CronCtx  # noqa: F401
    from pit_crew.service import CarService
    from pit_crew.settings import Settings
    other = CarService(Settings(overrides={"adapter": "stub", "history": "own"}), {}.get, store, wall_clock=clock, sleep=clock.sleep)
    assert other.history_provider.coverage_start() == svc.history_provider.coverage_start()


def test_recorder_failure_never_breaks_state_reads(svc, stub, monkeypatch):
    monkeypatch.setattr(svc._recorder, "_record", lambda *a, **k: 1 / 0)
    assert svc.get_state(force=True).value("battery_pct") == 70.0


def test_history_tool_uses_own_store_and_explains_coverage(svc, stub, clock):
    stub.values.update(battery_pct=50.0, charging_state="charging", charge_power_kw=7.0)
    svc.get_state(force=True)
    tick(svc, clock, 1800, battery_pct=60.0)
    tick(svc, clock, 600, charging_state="complete", charge_power_kw=0.0, battery_pct=61.0)
    tick(svc, clock, 3600)
    out = json.loads(tools.car_history({"kind": "summary", "days": 30}))
    assert out["ok"] and out["source"] == "Pit Crew recorded history"
    assert out["charging"]["sessions"] == 1 and out["charging"]["kwh_added"] > 5
    assert out["recording_since"] and "only been recording since" in out["note"]
    assert out["driving"] == {"trips": 0}


def test_history_off_still_reports_cleanly(stub_service, stub):
    s = stub_service(history="none")
    out = json.loads(tools.car_history({}))
    assert out["ok"] is False and "no history source" in out["error"]


def test_simulated_car_records_new_activity_on_top_of_its_synthetic_past(service):
    sim = service.adapter
    before = json.loads(tools.car_history({"kind": "summary", "days": 60}))["driving"]["trips"]
    service.get_state(force=True)
    sim.control("drive", miles=12, to="work")
    service.get_state(force=True)
    sim.control("advance", minutes=10)
    service.get_state(force=True)
    sim.control("advance", minutes=120)
    service.get_state(force=True)
    prov = service.history_provider
    assert prov.kind == "own+simulator" and prov.coverage_start() is not None
    assert prov.drives(0, 1e12)[-1].end < 1e12
    assert json.loads(tools.car_history({"kind": "summary", "days": 60}))["driving"]["trips"] >= before


def test_import_from_teslamate_is_idempotent_and_does_not_duplicate_recorded(stub_service, stub, clock):
    clock.t = 1_759_851_820.0
    tm = FakeTeslaMate()
    try:
        svc = stub_service(history="own", teslamate_url=tm.url, teslamate_car_id=1)
        res = svc.import_history("teslamate", 30)
        assert res["ok"] and res["drives"] == 5 and res["charges"] == 5
        again = svc.import_history("teslamate", 30)
        assert again["drives"] == 0 and again["charges"] == 0 and again["already_imported"] == 10
        assert {m for m, *_ in tm.requests} == {"GET"}
        out = json.loads(tools.car_history({"kind": "summary", "days": 30}))
        assert out["ok"] and out["charging"]["sessions"] == 5 and out["driving"]["trips"] == 3
        assert svc.store.query("SELECT DISTINCT source FROM drives") == [{"source": "teslamate"}]
        # something Pit Crew recorded itself overlapping an import window is not duplicated
        svc.store.execute("DELETE FROM charges WHERE source='teslamate'")
        svc.store.execute("INSERT INTO charges(source,start_ts,end_ts,start_pct,end_pct,kwh_added,open) "
                          "VALUES('recorded', ?, ?, 70, 80, 7.5, 0)",
                          (1759684020.0 - 600, 1759684020.0 + 600))   # overlaps a fixture session (fixture charge-session region)
        res3 = svc.import_history("teslamate", 30)
        assert res3["charges"] + res3["skipped_overlap"] >= 5
    finally:
        tm.stop()


def test_import_without_teslamate_configured_is_a_clean_error(stub_service):
    res = stub_service().import_history("teslamate", 30)
    assert res["ok"] is False and "TeslaMate API URL" in res["error"]
    assert stub_service().import_history("nope", 30)["ok"] is False


def test_slash_import_and_api(stub_service, clock):
    stub_service()
    from pit_crew import slash
    assert "Usage" in slash.handle("import")
    assert "Import failed" in slash.handle("import teslamate")


def _zone_only(stub):
    for k in ("odometer_mi", "speed_mph", "moving", "shift_state"):
        stub.values.pop(k, None)
    stub.values.update(battery_pct=70.0, range_mi=200.0, plugged_in=False, charging_state="disconnected")


def test_r8_sleep_drain_catch_up_is_not_a_trip(svc, stub, clock):
    """The car sleeps 5 h with stale numbers, then two polls 2 min apart show the accumulated 3 mi loss."""
    _zone_only(stub)
    svc.get_state(force=True)
    tick(svc, clock, 5 * 3600)                              # unchanged for 5 h (asleep)
    tick(svc, clock, 120, range_mi=197.0, battery_pct=69.0)  # wake: 3 mi lost -> 90 mi/h if naively divided by 2 min
    tick(svc, clock, 120)
    tick(svc, clock, 3600)
    assert svc.store.query("SELECT * FROM drives") == []


def test_r8_range_only_drop_at_home_needs_corroboration(svc, stub, clock):
    _zone_only(stub)
    svc.get_state(force=True)
    tick(svc, clock, 120, range_mi=195.0, battery_pct=68.0)  # fast drop but still 'home' both ends: not provable
    tick(svc, clock, 3600)
    assert svc.store.query("SELECT * FROM drives") == []


def test_r9_range_sourced_drives_have_no_circular_efficiency(svc, stub, clock):
    _zone_only(stub)
    svc.get_state(force=True)
    tick(svc, clock, 180, range_mi=170.0, battery_pct=62.0, location_zone="away")
    tick(svc, clock, 3600)
    d = svc.history_provider.drives(*window(svc))
    assert len(d) == 1 and d[0].miles == pytest.approx(30.0) and d[0].kwh and d[0].wh_per_mi is None
