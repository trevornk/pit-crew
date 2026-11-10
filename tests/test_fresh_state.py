"""Fresher car state (owner request, 2026-11-09): a 10-minute-old picture of the car confuses.

1. One targeted re-read after a command on Home Assistant's tesla_fleet (``homeassistant.update_entity`` on ONE vehicle
   entity refreshes the whole vehicle coordinator; home-assistant/core c459f2ef: helpers/update_coordinator.py
   CoordinatorEntity.async_update -> coordinator.async_request_refresh; tesla_fleet/coordinator.py L180-187 checks
   ``vehicle()`` (free) first and only reads vehicle_data when the car is online, so it never wakes the car).
2. TeslaMate (TeslaMateApi /status) as an optional fresher source next to Home Assistant, merged per field.

FakeHA's service effect is tesla_fleet's optimistic write (the commanded entity only). Its default answer to
``homeassistant/update_entity`` re-writes just that one entity, which is what HA does when the coordinator skips the
refresh (debouncer cooldown): the stamp moves, nothing was read from the car. ``coordinator_pass`` below is a real
refresh: every entity re-written from the car's data.
"""

import copy
from datetime import datetime, timezone

import pytest

from fakes import FakeHA, FakeTeslaMate, load_fixture
from pit_crew.adapters.homeassistant import parse_ha_time
from pit_crew.safety.approval import ApprovalEvidence

NOW = parse_ha_time("2026-11-09T21:49:55+00:00")
AUTO = ApprovalEvidence("hermes_approval", "approvals off", auto=True)
LOCK = "lock.demo_car_lock"
TRUNK = "cover.demo_car_trunk"
STATUS = "binary_sensor.demo_car_status"
BATTERY = "sensor.demo_car_battery_level"
CHARGING = "sensor.demo_car_charging"
UPDATE = "homeassistant/update_entity"


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def stamp(ha, eid, ts, changed=True):
    s = ha.states[eid]
    s["last_reported"] = s["last_updated"] = iso(ts)
    if changed:
        s["last_changed"] = iso(ts)


def coordinator_pass(ha, ts, truth=None, awake=True):
    """tesla_fleet's coordinator refresh: entities take the car's real values; every entity is re-written."""
    for eid, value in (truth or {}).items():
        ha.set_state(eid, value)
    ha.set_state(STATUS, "on" if awake else "off")
    for eid in ha.states:
        stamp(ha, eid, ts, changed=False)


@pytest.fixture
def ha():
    fake = FakeHA()
    for eid in fake.states:
        stamp(fake, eid, NOW - 300)                       # the integration last re-read the car 5 minutes ago
    fake.set_state("binary_sensor.demo_car_charge_cable", "on")
    fake.set_state(STATUS, "on")
    fake.set_state("device_tracker.demo_car_location", "home")
    yield fake
    fake.stop()


def live(make_service, ha, clock, **settings):
    clock.t = NOW
    ha.now_fn = lambda: clock.t
    cfg = {"adapter": "homeassistant", "history": "none", "ha_url": ha.url, "ha_slug": "demo_car"}
    cfg.update(settings)
    secrets = {"PIT_CREW_HA_TOKEN": FakeHA.TOKEN}
    svc = make_service(cfg, secrets=secrets)
    svc.arm(1, by="t")
    return svc


def on_refresh(ha, clock, **kw):
    """Make HA's update_entity run a real coordinator pass (with the car's true values ``truth``)."""
    plain = ha._effect

    def effect(service, data):
        if service == UPDATE:
            coordinator_pass(ha, clock.t, **kw)
        else:
            plain(service, data)
    ha._effect = effect


def refresh_calls(ha):
    return [c for c in ha.service_calls if c[0] == UPDATE]


# == 1. one targeted re-read after a command ==========================================================================
def test_refresh_confirms_from_the_cars_own_report(make_service, ha, clock):
    ha.set_state(LOCK, "locked")
    svc = live(make_service, ha, clock)
    on_refresh(ha, clock, truth={LOCK: "unlocked"})
    res = svc.run_command("unlock", {}, surface="tool", approval=AUTO)
    assert refresh_calls(ha) == [(UPDATE, {"entity_id": BATTERY})]        # ONE entity, once
    assert clock.t == NOW + 5                                               # the profile's settle delay, nothing more
    assert res["status"] == "confirmed" and res["readback"]["status"] == "confirmed"
    assert res["readback"]["refreshed"] is True
    assert "the car confirmed it" in res["message"] and "re-read the car once" in res["message"]
    assert res["timing_ms"]["refresh"] >= 0
    row = svc.store.audit_recent(1)[0]
    assert row["readback"] == "confirmed" and "5 s settle + re-read request" in row["detail"]


def test_refresh_showing_the_old_value_is_not_confirmed(make_service, ha, clock):
    ha.set_state(TRUNK, "closed")
    svc = live(make_service, ha, clock)
    on_refresh(ha, clock, truth={TRUNK: "closed"})                          # Tesla accepted it; the trunk did not open
    res = svc.run_command("open_trunk", {}, surface="tool", approval=AUTO)
    assert res["status"] == "sent_unconfirmed" and res["readback"]["status"] == "not_confirmed"
    assert "re-read the car at" in res["message"] and "still reports the old value" in res["message"]
    assert "car confirmed it" not in res["message"]


def test_skipped_refresh_never_confirms_the_optimistic_write(make_service, ha, clock):
    """HA re-wrote only the requested entity (coordinator in cooldown): that stamp is NOT a re-read of the car."""
    ha.set_state(LOCK, "locked")
    svc = live(make_service, ha, clock)                                      # default FakeHA: update_entity restamps 1 entity
    res = svc.run_command("unlock", {}, surface="tool", approval=AUTO)
    assert len(refresh_calls(ha)) == 1
    assert res["status"] == "sent_unconfirmed" and res["readback"]["status"] == "accepted"
    assert "had not re-read the car" in res["message"] and "Tesla accepted it" in res["message"]
    st = svc.get_state(force=True)
    assert st.fields["locked"].assumed
    assert st.fields["battery_pct"].updated_at == NOW - 300                  # Pit Crew's own request is not car data
    assert st.newest_update() == NOW - 300


def test_refresh_that_finds_the_car_asleep_proves_nothing(make_service, ha, clock):
    """An asleep car: tesla_fleet re-writes its OLD data (and never wakes the car). Even a matching old value is no proof."""
    ha.set_state(LOCK, "unlocked")
    svc = live(make_service, ha, clock)
    on_refresh(ha, clock, truth={LOCK: "locked"}, awake=False)
    res = svc.run_command("lock", {}, surface="tool")
    assert res["readback"]["status"] == "accepted" and res["status"] == "sent_unconfirmed"
    assert "asleep or offline" in res["message"]


def test_failed_refresh_request_falls_back_to_accepted_with_the_reason(make_service, ha, clock):
    ha.set_state(LOCK, "locked")
    ha.fail_service_names = {UPDATE}
    svc = live(make_service, ha, clock)
    res = svc.run_command("unlock", {}, surface="tool", approval=AUTO)
    assert ("lock/unlock", {"entity_id": LOCK}) in ha.service_calls
    assert res["status"] == "sent_unconfirmed" and res["readback"]["status"] == "accepted"
    assert "request to re-read the car failed" in res["message"]
    assert res["readback"]["refreshed"] is False
    assert "failed)" in svc.store.audit_recent(1)[0]["detail"]


def test_refresh_is_bounded(make_service, ha, clock, monkeypatch):
    from pit_crew.adapters.homeassistant import HomeAssistantAdapter
    from pit_crew.safety.engine import REFRESH_BUDGET_S
    seen = []
    monkeypatch.setattr(HomeAssistantAdapter, "request_refresh", lambda self, timeout_s: seen.append(timeout_s) or "x")
    ha.set_state(LOCK, "locked")
    svc = live(make_service, ha, clock)
    svc.run_command("unlock", {}, surface="tool", approval=AUTO)
    assert seen == [REFRESH_BUDGET_S - 5] and REFRESH_BUDGET_S == 10        # settle 5 s + request <= 5 s
    assert clock.t - NOW <= REFRESH_BUDGET_S


def test_setting_off_means_no_refresh(make_service, ha, clock):
    ha.set_state(LOCK, "locked")
    svc = live(make_service, ha, clock, refresh_after_command=False)
    res = svc.run_command("unlock", {}, surface="tool", approval=AUTO)
    assert refresh_calls(ha) == [] and clock.t == NOW
    assert res["readback"]["status"] == "accepted" and res["readback"]["quick"]


@pytest.mark.parametrize("cmd", ["flash", "honk"])
def test_unverifiable_commands_never_refresh(make_service, ha, clock, cmd):
    svc = live(make_service, ha, clock)
    res = svc.run_command(cmd, {}, surface="tool", approval=AUTO)
    assert res["readback"]["status"] == "unverifiable"
    assert refresh_calls(ha) == [] and clock.t == NOW


def test_status_reads_and_the_watcher_never_refresh(make_service, ha, clock):
    svc = live(make_service, ha, clock)
    svc.status(force=True)
    svc.get_state(force=True)
    svc.readiness()
    svc.watcher.poll_once(force=True)
    assert refresh_calls(ha) == []
    assert not [r for r in ha.requests if r[0] == "POST"]


def test_already_fresh_state_needs_no_refresh(make_service, ha, clock):
    ha.set_state(LOCK, "locked"); stamp(ha, LOCK, NOW - 30)
    for eid in ("sensor.demo_car_battery_range", "sensor.demo_car_inside_temperature"):
        stamp(ha, eid, NOW - 30)
    svc = live(make_service, ha, clock)
    res = svc.run_command("lock", {}, surface="tool")
    assert res["readback"].get("already") and refresh_calls(ha) == []


def test_fast_reporting_profiles_never_refresh(make_service, ha, clock):
    """teslemetry_ha reports within seconds (streaming): no refresh plan, the normal wait applies."""
    from pit_crew.adapters.profile import load_profile
    for name in ("teslemetry_ha", "tessie_ha", "kia_hyundai_connect", "bmw_connected_drive"):
        assert "refresh_after_command" not in load_profile(name)
    prof = load_profile("tesla_fleet")["refresh_after_command"]
    assert prof["entity"] == "sensor.{slug}_battery_level" and prof["settle_s"] == 5 and "$0.002" in prof["cost_note"]


def test_profile_validation_of_refresh_after_command():
    from pit_crew.adapters.profile import validate_profile
    base = {"fields": {"battery_pct": {"entity": "sensor.{slug}_battery_level", "kind": "number"}}}
    assert not validate_profile({**base, "refresh_after_command": {"entity": "sensor.{slug}_x", "settle_s": 5}})
    assert validate_profile({**base, "refresh_after_command": {"entity": "sensor.x", "settle_s": 5}})
    assert validate_profile({**base, "refresh_after_command": {"entity": "sensor.{slug}_x", "settle_s": 30}})


# == 2. TeslaMate as a fresher source next to Home Assistant =========================================================
class ScriptedTeslaMate(FakeTeslaMate):
    """TeslaMateApi fake whose /status the test edits (``car``: car_status keys; ``state``: TeslaMate's car state)."""

    def __init__(self):
        super().__init__()
        self.data = copy.deepcopy(load_fixture("teslamate_cars_1_status.json"))
        self.status["state"] = "online"
        self.status["car_geodata"]["geofence"] = "Home"
        self.down = False

    @property
    def status(self):
        return self.data["data"]["status"]

    def handle(self, h, method, path, body):
        if method == "GET" and path.startswith("/api/v1/cars/1/status"):
            if self.down:
                return h._reply(503, {"error": "down"})
            return h._reply(200, self.data)
        return super().handle(h, method, path, body)


@pytest.fixture
def tm():
    fake = ScriptedTeslaMate()
    yield fake
    fake.stop()


def with_tm(make_service, ha, clock, tm, **settings):
    return live(make_service, ha, clock, teslamate_url=tm.url, **settings)


def test_without_teslamate_nothing_changes(make_service, ha, clock):
    svc = live(make_service, ha, clock)
    st = svc.get_state(force=True)
    assert all(f.source is None for f in st.fields.values())
    assert "field_sources" not in st.to_public(now=clock.t)["freshness"]
    assert "fresher_state_from" not in svc.describe_adapter()


def test_newer_teslamate_reading_wins_per_field_and_says_so(make_service, ha, clock, tm):
    ha.set_state(LOCK, "unlocked")                                           # HA: 5 min old
    tm.status["car_status"].update(locked=True)
    svc = with_tm(make_service, ha, clock, tm)
    st = svc.get_state(force=True)
    assert st.value("locked") is True and st.fields["locked"].source == "teslamate"
    assert st.fields["locked"].updated_at == NOW - 60                       # honest: TeslaMateApi has no per-field time
    fr = st.to_public(now=clock.t)["freshness"]
    assert fr["field_sources"]["locked"] == "teslamate"
    assert "no per-field times" in fr["source_note"]
    assert svc.describe_adapter()["fresher_state_from"] == "teslamate (read-only)"


def test_newer_home_assistant_reading_wins():
    from pit_crew.adapters.teslamate import TeslaMateOverlay
    from pit_crew.brain.models import FieldValue, VehicleState
    fields = {"charging_state": FieldValue("charging", NOW - 10), "locked": FieldValue(False, NOW - 300)}
    tm_state = VehicleState(name="", source="teslamate", captured_at=NOW, fields={
        "charging_state": FieldValue("complete", NOW - 60, source="teslamate"),
        "locked": FieldValue(True, NOW - 60, source="teslamate")})
    TeslaMateOverlay.merge(fields, tm_state, None)
    assert fields["charging_state"].value == "charging" and fields["charging_state"].source == "home_assistant"
    assert fields["locked"].value is True and fields["locked"].source == "teslamate"


def test_sleeping_car_teslamate_values_are_aged_from_state_since(make_service, ha, clock, tm):
    tm.status["state"] = "asleep"
    tm.status["state_since"] = iso(NOW - 3600)
    tm.status["car_status"]["locked"] = True
    ha.set_state(LOCK, "unlocked")                                           # 5 min old: newer than TeslaMate's hour
    svc = with_tm(make_service, ha, clock, tm)
    st = svc.get_state(force=True)
    assert st.value("locked") is False and st.fields["locked"].source == "home_assistant"


def test_teslamate_is_only_ever_read(make_service, ha, clock, tm):
    on_refresh(ha, clock, truth={LOCK: "unlocked"})
    ha.set_state(LOCK, "locked")
    svc = with_tm(make_service, ha, clock, tm)
    svc.run_command("unlock", {}, surface="tool", approval=AUTO)
    svc.status(force=True)
    assert tm.requests and all(r[0] == "GET" for r in tm.requests)
    assert ("lock/unlock", {"entity_id": LOCK}) in ha.service_calls          # the command went through HA only


def test_readback_confirms_from_teslamate_only_after_the_send(make_service, ha, clock, tm):
    """HA's re-read is off; TeslaMate's change since Pit Crew's look right after the send is the car's report."""
    ha.set_state(LOCK, "locked")
    tm.status["car_status"]["locked"] = True
    svc = with_tm(make_service, ha, clock, tm, refresh_after_command=False)
    plain = ha._effect

    def effect(service, data):
        plain(service, data)
        clock.advance(1)                                 # the call to HA takes a moment
        tm.status["car_status"]["locked"] = True        # unchanged at the baseline look ...
    ha._effect = effect
    real_status = tm.handle
    looks = []

    def handle(h, method, path, body):
        looks.append(path)
        if len(looks) >= 3:                              # preflight, baseline, then the read-back look sees the change
            tm.status["car_status"]["locked"] = False
        return real_status(h, method, path, body)
    tm.handle = handle
    res = svc.run_command("unlock", {}, surface="tool", approval=AUTO)
    assert res["status"] == "confirmed" and res["readback"]["status"] == "confirmed"
    st = svc._last_state
    assert st.fields["locked"].source == "teslamate" and st.fields["locked"].updated_at >= NOW


def test_teslamate_reading_from_before_the_send_never_confirms(make_service, ha, clock, tm):
    """TeslaMate already shows 'unlocked' at the baseline look (it may be the car's state from before): no proof."""
    ha.set_state(LOCK, "locked")
    tm.status["car_status"]["locked"] = True
    svc = with_tm(make_service, ha, clock, tm, refresh_after_command=False)
    plain = ha._effect

    def effect(service, data):
        plain(service, data)
        clock.advance(1)
        tm.status["car_status"]["locked"] = False       # TeslaMate shows it before Pit Crew's baseline look
    ha._effect = effect
    res = svc.run_command("unlock", {}, surface="tool", approval=AUTO)
    assert res["readback"]["status"] == "accepted" and res["status"] == "sent_unconfirmed"


def test_moving_from_either_source_wins(make_service, ha, clock, tm):
    tm.status["driving_details"].update(shift_state="D", speed=30)
    tm.status["state"] = "driving"
    svc = with_tm(make_service, ha, clock, tm)
    st = svc.get_state(force=True)
    assert st.motion() is True and st.fields["moving"].source == "teslamate"
    ha.set_state(TRUNK, "closed")
    res = svc.run_command("open_trunk", {}, surface="tool", approval=AUTO)
    assert res["status"] == "blocked"


def test_older_teslamate_parked_does_not_hide_a_ha_moving_reading(make_service, ha, clock, tm, monkeypatch):
    from pit_crew.adapters.teslamate import TeslaMateOverlay
    from pit_crew.brain.models import FieldValue, VehicleState
    fields = {"speed_mph": FieldValue(25.0, NOW - 900), "shift_state": FieldValue("D", NOW - 900)}
    tm_state = VehicleState(name="", source="teslamate", captured_at=NOW, fields={
        "speed_mph": FieldValue(0.0, NOW - 5, source="teslamate"), "shift_state": FieldValue("P", NOW - 5, source="teslamate")})
    TeslaMateOverlay.merge(fields, tm_state, None)
    assert fields["speed_mph"].value == 25.0 and fields["shift_state"].value == "D"


def test_teslamate_down_falls_back_to_home_assistant(make_service, ha, clock, tm):
    tm.down = True
    ha.set_state(LOCK, "locked")
    svc = with_tm(make_service, ha, clock, tm)
    st = svc.get_state(force=True)
    assert st.value("locked") is True and st.fields["locked"].source == "home_assistant"
    assert "TeslaMate could not be read" in st.to_public(now=clock.t)["freshness"]["source_note"]


def test_assumed_ha_value_loses_to_a_teslamate_reading_newer_than_has_last_reread(make_service, ha, clock, tm):
    """After a command HA shows its own assumption; TeslaMate's (newer than HA's 5-min-old re-read) is the car's word."""
    ha.set_state(TRUNK, "closed")
    tm.status["car_status"]["trunk_open"] = False
    svc = with_tm(make_service, ha, clock, tm, refresh_after_command=False)
    svc.run_command("open_trunk", {}, surface="tool", approval=AUTO)
    st = svc.get_state(force=True)
    assert st.value("trunk_open") is False and st.fields["trunk_open"].source == "teslamate"


# == 3. an estimated TeslaMate stamp is never proof the car reported recently ========================================
def test_estimated_teslamate_unlocked_never_means_already_unlocked(make_service, ha, clock, tm):
    """2026-11-09 hand-lock case with TeslaMate configured: the owner locked by hand; TeslaMate has not seen it and still
    says unlocked (stamped by the awake-car assumption, exactly 60 s old); HA's own reading is 5 minutes old. The unlock
    must be sent, never reported as a no-op 'already unlocked'."""
    ha.set_state(LOCK, "unlocked")
    stamp(ha, LOCK, NOW - 300)
    tm.status["car_status"]["locked"] = False
    svc = with_tm(make_service, ha, clock, tm, refresh_after_command=False)
    st = svc.get_state(force=True)
    assert st.fields["locked"].source == "teslamate" and st.fields["locked"].estimated
    res = svc.run_command("unlock", {}, surface="tool", approval=AUTO)
    assert ("lock/unlock", {"entity_id": LOCK}) in ha.service_calls
    assert "already" not in res["readback"] and res["readback"]["status"] != "confirmed"
    assert "already unlocked" not in str(res).lower() and "nothing needed to change" not in str(res)


def test_estimated_stamps_are_flagged_only_for_the_awake_assumption(make_service, ha, clock, tm):
    svc = with_tm(make_service, ha, clock, tm)
    tm.status["state"] = "asleep"
    tm.status["state_since"] = iso(NOW - 3600)
    st = svc.get_state(force=True)
    tm_fields = [f for f in st.fields.values() if f.source == "teslamate"]
    assert tm_fields and not any(f.estimated for f in tm_fields)         # state_since is a real time
    pub = svc.get_state(force=True).to_public(now=clock.t)["freshness"]
    assert "estimated_times" not in pub


# == 4. TeslaMate also merges asleep / user_present (19:38 real-install case) =========================================
def test_awake_teslamate_beats_stale_ha_asleep_and_no_wake_is_asked(make_service, ha, clock, tm):
    """HA: status off (asleep) last written 30 min ago. TeslaMate: online since 4 min ago, user present."""
    coordinator_pass(ha, NOW - 1800, awake=False)
    tm.status["state"] = "online"
    tm.status["state_since"] = iso(NOW - 240)
    tm.status["car_status"].update(is_user_present=True)
    svc = with_tm(make_service, ha, clock, tm, refresh_after_command=False)
    st = svc.get_state(force=True)
    assert st.value("asleep") is False and st.fields["asleep"].source == "teslamate"
    assert st.fields["asleep"].updated_at == NOW - 240 and not st.fields["asleep"].estimated      # state_since: a real time
    assert st.value("user_present") is True and st.fields["user_present"].source == "teslamate"
    ha.set_state(TRUNK, "closed")
    stamp(ha, TRUNK, NOW - 10)
    res = svc.run_command("open_trunk", {}, surface="tool", approval=AUTO)
    assert res["status"] != "blocked" and "asleep" not in str(res.get("message", "")).lower()
    assert any(c[1] == {"entity_id": TRUNK} for c in ha.service_calls)        # sent, no wake question


def test_without_teslamate_stale_ha_asleep_is_unchanged(make_service, ha, clock):
    coordinator_pass(ha, NOW - 1800, awake=False)
    st = live(make_service, ha, clock).get_state(force=True)
    assert st.value("asleep") is True and st.fields["asleep"].source is None


def test_teslamate_offline_never_establishes_asleep_or_parked(make_service, ha, clock, tm):
    from pit_crew.adapters.teslamate import status_to_state
    tm.status["state"] = "offline"
    tm.status["state_since"] = iso(NOW - 600)
    tm.status["driving_details"]["shift_state"] = None
    tm_st = status_to_state(tm.data["data"], "c", NOW)
    assert not tm_st.fields["asleep"].available and tm_st.value("shift_state") is None
    ha.set_state(STATUS, "on")
    svc = with_tm(make_service, ha, clock, tm)
    st = svc.get_state(force=True)
    assert st.value("asleep") is False and st.fields["asleep"].source in (None, "home_assistant")   # HA's own word stays
    from pit_crew.brain.models import FieldValue, VehicleState
    only = VehicleState(name="c", source="x", captured_at=NOW, fields={"asleep": FieldValue(None, None, False)})
    assert only.motion(NOW, 600) is None


def test_teslamate_asleep_with_state_since_is_parked_evidence_and_estimated_is_not():
    from pit_crew.brain.models import FieldValue, VehicleState
    real = VehicleState(name="c", source="x", captured_at=NOW,
                        fields={"asleep": FieldValue(True, NOW - 120, source="teslamate")})
    assert real.motion(NOW, 600) is False
    est = VehicleState(name="c", source="x", captured_at=NOW,
                       fields={"asleep": FieldValue(True, NOW - 60, source="teslamate", estimated=True)})
    assert est.motion(NOW, 600) is None


def test_teslamate_asleep_reading_has_real_stamp_and_wins_over_older_ha_awake(make_service, ha, clock, tm):
    coordinator_pass(ha, NOW - 1800, awake=True)
    tm.status["state"] = "asleep"
    tm.status["state_since"] = iso(NOW - 120)
    svc = with_tm(make_service, ha, clock, tm)
    st = svc.get_state(force=True)
    assert st.value("asleep") is True and st.fields["asleep"].source == "teslamate" and not st.fields["asleep"].estimated
