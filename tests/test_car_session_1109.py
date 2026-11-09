"""2026-11-09 real-car session (audit rows 22-35): what the owner saw at the car vs what Pit Crew said.

Home Assistant's tesla_fleet integration writes the commanded entity itself as soon as Tesla accepts the call
(lock.py ``self._attr_is_locked = False; self.async_write_ha_state()``) and re-reads the car only every 10 minutes
(coordinator.py ``VEHICLE_INTERVAL_SECONDS = 600``). FakeHA's service effect is exactly that optimistic write: it
restamps the commanded entity and nothing else. ``refresh()`` below is a coordinator pass: every entity re-written.
"""

from datetime import datetime, timezone

import pytest

from fakes import FakeHA
from pit_crew.adapters.homeassistant import parse_ha_time
from pit_crew.safety.approval import ApprovalEvidence

NOW = parse_ha_time("2026-11-09T21:49:55+00:00")       # 15:49:55 CST, the unlock
AUTO = ApprovalEvidence("hermes_approval", "approvals off", auto=True)
LOCK = "lock.demo_car_lock"
TRUNK = "cover.demo_car_trunk"


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def stamp(ha, eid, ts, changed=True):
    s = ha.states[eid]
    s["last_reported"] = s["last_updated"] = iso(ts)
    if changed:
        s["last_changed"] = iso(ts)


def refresh(ha, ts):
    """A tesla_fleet coordinator pass: every entity of the car is re-written from fresh car data."""
    for eid in ha.states:
        stamp(ha, eid, ts, changed=False)


@pytest.fixture
def ha():
    fake = FakeHA()
    for eid in fake.states:
        stamp(fake, eid, NOW - 300)                  # the integration last re-read the car 5 minutes ago
    fake.set_state("binary_sensor.demo_car_charge_cable", "on")     # plugged in: proves parked
    fake.set_state("binary_sensor.demo_car_status", "on")
    fake.set_state("device_tracker.demo_car_location", "home")
    yield fake
    fake.stop()


@pytest.fixture
def reports_in_time(monkeypatch):
    """Read-back loop tests: pretend the integration can report inside the wait (like teslemetry's streaming). With the real
    tesla_fleet latency (600 s) Pit Crew reads once and answers at once; see test_tesla_fleet_answers_after_one_read."""
    from pit_crew.adapters.homeassistant import HomeAssistantAdapter
    monkeypatch.setattr(HomeAssistantAdapter, "report_latency_s", lambda self: None)


def live(make_service, ha, clock, **settings):
    clock.t = NOW
    ha.now_fn = lambda: clock.t
    cfg = {"adapter": "homeassistant", "history": "none", "ha_url": ha.url, "ha_slug": "demo_car"}
    cfg.update(settings)
    svc = make_service(cfg, secrets={"PIT_CREW_HA_TOKEN": FakeHA.TOKEN})
    svc.arm(1, by="t")
    return svc


# -- 1. an optimistic write is never "confirmed" ------------------------------------------------------------------------
def test_unlock_after_doors_were_locked_by_hand_is_not_already_unlocked(make_service, ha, clock):
    """15:49:55: HA's 5-minute-old reading said unlocked; the owner had just locked the doors by hand; the car physically
    unlocked. Pit Crew said 'already unlocked before the command; nothing needed to change'. It must not."""
    ha.set_state(LOCK, "unlocked")
    svc = live(make_service, ha, clock)
    res = svc.run_command("unlock", {}, surface="tool", approval=AUTO)
    assert ("lock/unlock", {"entity_id": LOCK}) in ha.service_calls          # it was sent
    assert "already" not in res["readback"]
    assert "nothing needed to change" not in res["message"]
    assert "may be out of date" in res["message"] and "5 min old" in res["message"]
    assert res["status"] == "sent_unconfirmed"
    # HA re-wrote 'unlocked' itself when Tesla accepted the call; that is not the car's report
    assert res["readback"]["status"] == "accepted" and "Tesla accepted it" in res["message"]
    assert "confirmed it" not in res["message"]
    row = svc.store.audit_recent(1)[0]
    assert row["readback"] == "accepted" and row["status"] == "sent_unconfirmed"


def test_open_trunk_confirmed_one_second_later_was_only_the_integrations_own_write(make_service, ha, clock):
    """15:52:24 open_trunk 'confirmed' at 15:52:25; the owner closed it by hand and car_status still said open at 15:58."""
    ha.set_state(TRUNK, "closed")
    svc = live(make_service, ha, clock)
    res = svc.run_command("open_trunk", {}, surface="tool", approval=AUTO)
    assert res["status"] == "sent_unconfirmed" and res["readback"]["status"] == "accepted"
    assert "confirmed it" not in res["message"]
    st = svc.get_state(force=True)
    assert st.fields["trunk_open"].value is True and st.fields["trunk_open"].assumed
    pub = st.to_public(now=clock.t)
    assert "trunk_open" in pub["freshness"]["assumed_after_command"]
    # the optimistic write does not make the car's data look fresh either
    assert st.newest_update() <= NOW - 300 + 1


@pytest.mark.parametrize("cmd,eid,before", [("unlock", LOCK, "locked"), ("open_trunk", TRUNK, "closed")])
def test_tesla_fleet_answers_after_one_read(make_service, ha, clock, cmd, eid, before):
    """tesla_fleet re-reads the car every 600 s, so a 20 s read-back loop only delays the reply: one read, then answer."""
    ha.set_state(eid, before)
    svc = live(make_service, ha, clock)
    svc.capabilities()                                               # (the adapter's one-time first look at HA)
    reads = sum(1 for r in ha.requests if r[0] == "GET")
    res = svc.run_command(cmd, {}, surface="tool", approval=AUTO)
    assert clock.t == NOW                                            # the fake clock never advanced: no read-back wait
    assert sum(1 for r in ha.requests if r[0] == "GET") - reads <= 2  # the preflight read + ONE read-back
    assert res["status"] == "sent_unconfirmed" and res["readback"]["status"] == "accepted" and res["readback"]["quick"]
    assert "Tesla accepted it. The car reports back within ~10 min; check car_status later" in res["message"]
    assert svc.store.audit_recent(1)[0]["readback"] == "accepted"


def test_a_real_reread_after_the_command_does_confirm(make_service, ha, clock, reports_in_time):
    ha.set_state(TRUNK, "closed")
    svc = live(make_service, ha, clock)
    real_sleep = svc._sleep

    def sleep(sec):
        real_sleep(sec)
        if clock.t - NOW >= 6 and ha.states["sensor.demo_car_battery_level"]["last_reported"] != iso(NOW + 6):
            refresh(ha, NOW + 6)                      # the coordinator re-reads the car; the trunk really is open
    svc._sleep = sleep
    res = svc.run_command("open_trunk", {}, surface="tool", approval=AUTO)
    assert res["status"] == "confirmed" and res["readback"]["status"] == "confirmed"
    assert not svc.get_state(force=True).fields["trunk_open"].assumed


def test_a_reread_that_shows_the_old_value_is_not_confirmed(make_service, ha, clock, reports_in_time):
    """Tesla accepted it but the re-read shows the trunk still closed: HA reverts its assumption; Pit Crew says not confirmed."""
    ha.set_state(TRUNK, "closed")
    svc = live(make_service, ha, clock)
    real_sleep = svc._sleep

    def sleep(sec):
        real_sleep(sec)
        if clock.t - NOW >= 6 and ha.states[TRUNK]["state"] == "open":
            ha.set_state(TRUNK, "closed")
            refresh(ha, clock.t)
    svc._sleep = sleep
    res = svc.run_command("open_trunk", {}, surface="tool", approval=AUTO)
    assert res["status"] == "sent_unconfirmed" and res["readback"]["status"] == "not_confirmed"


def test_profiles_with_optimistic_integrations_are_marked():
    from pit_crew.adapters.profile import load_profile
    for name in ("tesla_fleet", "teslemetry_ha", "tessie_ha"):
        assert load_profile(name).get("optimistic_commands") is True, name


# -- 2. "already" needs a fresh reading -----------------------------------------------------------------------------------
def test_close_windows_already_closed_on_a_5_minute_old_reading_is_not_claimed(make_service, ha, clock):
    """15:47:30 close_windows: the window sensors said closed, from before the vent; HA never re-read them."""
    for w in ("front_driver", "front_passenger", "rear_driver", "rear_passenger"):
        ha.set_state(f"binary_sensor.demo_car_{w}_window", "off")
    svc = live(make_service, ha, clock)
    res = svc.run_command("close_windows", {}, surface="tool")
    assert "already" not in res["readback"] and res["status"] == "sent_unconfirmed"
    assert "may be out of date" in res["message"]


def test_already_locked_with_a_reading_from_30_s_ago_is_still_said(make_service, ha, clock):
    refresh(ha, NOW - 30)
    ha.set_state(LOCK, "locked")
    svc = live(make_service, ha, clock)
    res = svc.run_command("lock", {}, surface="tool")
    assert res["status"] == "confirmed" and res["readback"]["already"] == "locked"
    assert "less than a minute before" in res["message"]


def test_already_is_not_claimed_from_a_previous_commands_optimistic_write(make_service, ha, clock):
    """An earlier `lock` made HA *assume* locked 20 s ago; the car was not re-read since. Not 'already locked'."""
    ha.set_state(LOCK, "locked")
    stamp(ha, LOCK, NOW - 20)
    svc = live(make_service, ha, clock)
    res = svc.run_command("lock", {}, surface="tool")
    assert "already" not in res["readback"]
    assert res["readback"]["status"] == "accepted"


# -- 3. practice wording follows the approval mode -------------------------------------------------------------------------
@pytest.fixture
def bypass(monkeypatch):
    import sys
    import types

    def set_mode(off):
        fake = types.ModuleType("tools.approval")
        fake.is_approval_bypass_active = lambda: off
        monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
        monkeypatch.setitem(sys.modules, "tools.approval", fake)
    return set_mode


def test_practice_wording_when_hermes_approvals_are_off(make_service, bypass):
    bypass(True)
    svc = make_service()
    msg = svc.run_command("unlock", {}, surface="tool")["message"]
    assert "Hermes approvals are off, so it would be sent without asking" in msg
    assert "would need your approval" not in msg


def test_practice_wording_when_hermes_asks(make_service, bypass):
    bypass(False)
    msg = make_service().run_command("flash", {}, surface="tool")["message"]
    assert "Hermes would ask you first" in msg


def test_practice_wording_for_always_ask_commands(make_service, bypass):
    bypass(False)
    svc = make_service({"require_human_for": ["unlock"]})
    assert "you'd be asked every time" in svc.run_command("unlock", {}, surface="tool")["message"]
    bypass(True)
    assert "refused until you turn approvals on" in svc.run_command("unlock", {}, surface="tool")["message"]


def test_practice_wording_for_safe_commands_mentions_no_approval(make_service, bypass):
    bypass(True)
    msg = make_service().run_command("lock", {}, surface="tool")["message"]
    assert "approval" not in msg and "would ask" not in msg


# -- 4. timing in the audit; unverifiable commands return without waiting ---------------------------------------------------
def test_timing_is_recorded_and_flash_does_not_wait_for_a_readback(make_service, ha, clock):
    svc = live(make_service, ha, clock)
    res = svc.run_command("flash", {}, surface="tool", approval=AUTO, reason="owner at the car")
    assert res["readback"]["status"] == "unverifiable"
    assert clock.t == NOW                                    # no read-back wait at all
    assert set(res["timing_ms"]) == {"send", "readback"}
    detail = svc.store.audit_recent(1)[0]["detail"]
    assert "timing: service answered in" in detail and "read-back took" in detail
    assert detail.startswith("owner at the car | sent to Home Assistant: button/press")


def test_timing_survives_a_very_long_reason(make_service, ha, clock):
    svc = live(make_service, ha, clock)
    svc.run_command("flash", {}, surface="tool", approval=AUTO, reason="x" * 900)
    assert "read-back took" in svc.store.audit_recent(1)[0]["detail"]
