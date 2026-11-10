"""A reading that is not current must never be worded as current fact (the 00:17 "UNLOCKED" case: the lock reading was
2,130 s old, from before the car fell asleep, and the summary still said UNLOCKED in capitals)."""

import json

from pagerender import backend_summary, needs_desktop_render, needs_web_render, render, render_web  # noqa: F401
from pit_crew import slash, tools
from pit_crew.brain.models import FieldValue, VehicleState, describe_state, last_reported


def svc_stale_unlocked(stub_service, stub, **kw):
    stub.values.update(locked=False, asleep=True, location_zone="home")
    stub.ages["locked"] = 2130.0
    return stub_service(armed=False, watcher_enabled=True, **kw)


def test_stale_unlocked_asleep_says_last_reported_not_bare_unlocked(stub_service, stub):
    s = svc_stale_unlocked(stub_service, stub)
    res = json.loads(tools.car_status({"refresh": True}))
    text = res["summary"]
    assert "last reported unlocked 36 min ago, before the car went to sleep" in text
    assert "UNLOCKED" not in text and "; unlocked;" not in text
    assert res["freshness"]["stale_fields"]["locked"] == 2130        # machine-readable data unchanged


def test_stale_unlocked_awake_has_no_sleep_clause(stub_service, stub):
    stub.values.update(locked=False, asleep=False)
    stub.ages["locked"] = 2130.0
    stub_service(armed=False)
    text = json.loads(tools.car_status({"refresh": True}))["summary"]
    assert "last reported unlocked 36 min ago" in text and "went to sleep" not in text and "UNLOCKED" not in text


def test_fresh_wording_is_unchanged(stub_service, stub):
    stub.values.update(locked=False)
    stub_service(armed=False)
    text = json.loads(tools.car_status({"refresh": True}))["summary"]
    assert "UNLOCKED" in text and "last reported" not in text
    stub.values.update(locked=True)
    assert "; locked;" in json.loads(tools.car_status({"refresh": True}))["summary"]


def test_stale_locked_reading_is_also_hedged():
    st = VehicleState(name="T", source="t", captured_at=10000.0)
    st.fields["locked"] = FieldValue(True, 10000.0 - 3600)
    assert "locked (last reported" not in describe_state(st, 10000.0)
    assert "last reported locked 60 min ago" in describe_state(st, 10000.0)


def test_unknown_age_is_not_presented_as_current():
    st = VehicleState(name="T", source="t", captured_at=100.0)
    st.fields["locked"] = FieldValue(False, None)
    assert last_reported(st, ["locked"], 100.0) == "at an unknown time"
    assert "last reported unlocked at an unknown time" in describe_state(st, 100.0)


def test_before_sleep_clause_needs_the_sleep_to_be_newer_than_the_reading():
    st = VehicleState(name="T", source="t", captured_at=10000.0)
    st.fields["locked"] = FieldValue(False, 10000.0 - 2000)
    st.fields["asleep"] = FieldValue(True, 10000.0 - 3000)            # slept BEFORE this lock reading: not "before sleep"
    assert "before the car went to sleep" not in last_reported(st, ["locked"], 10000.0)
    st.fields["asleep"] = FieldValue(True, 10000.0 - 100)
    assert last_reported(st, ["locked"], 10000.0).endswith("before the car went to sleep")


def test_stale_openings_charging_battery_and_zone_are_hedged():
    st = VehicleState(name="T", source="t", captured_at=10000.0)
    old = 10000.0 - 7200
    st.fields.update(doors_open=FieldValue(["front_left"], old), windows_open=FieldValue([], old), battery_pct=FieldValue(71, old),
                     charging_state=FieldValue("stopped", old), plugged_in=FieldValue(True, old),
                     location_zone=FieldValue("home", old))
    text = describe_state(st, 10000.0)
    assert "door(s) open: front_left (last reported 2 h ago)" in text
    assert "71% battery (last reported 2 h ago)" in text
    assert "plugged in but not charging (last reported 2 h ago)" in text
    assert "at home (last reported 2 h ago)" in text


def test_slash_car_card_hedges_stale_lock_and_closed(stub_service, stub):
    svc_stale_unlocked(stub_service, stub)
    stub.ages["doors_open"] = stub.ages["windows_open"] = stub.ages["trunk_open"] = stub.ages["frunk_open"] = 2130.0
    card = slash.handle("")
    assert "Last reported unlocked 36 min ago, before the car went to sleep" in card and "UNLOCKED" not in card
    assert "all closed (last reported 36 min ago" in card


def test_slash_car_card_fresh_unchanged(stub_service, stub):
    stub.values.update(locked=False)
    stub_service(armed=False)
    card = slash.handle("")
    assert "UNLOCKED" in card and "all closed" in card and "last reported" not in card.lower()


def test_approval_prompt_car_line_is_hedged_too(stub_service, stub):
    s = svc_stale_unlocked(stub_service, stub)
    from pit_crew.safety import gate
    assert gate.describe_state is describe_state            # the same shared sentence builder
    assert "last reported unlocked" in describe_state(s.get_state(force=True), s.now(), 900.0)


# -- watcher: no alert from a stale lock/opening reading (already correct; this pins it) -------------------------
def test_watcher_does_not_alert_unlocked_from_a_stale_lock_reading(stub_service, stub, clock):
    stub.values.update(locked=False, location_zone="away", asleep=True, shift_state="P", speed_mph=0.0, moving=False)
    stub.ages["locked"] = 2130.0
    s = stub_service(armed=False, watcher_enabled=True)
    s.watcher.poll_once()
    clock.advance(3600)
    stub.ages["locked"] = 2130.0 + 3600
    s.watcher.poll_once()
    assert not [a for a in s.alerts(("new", "surfaced", "acked")) if a["rule"] in ("left_unlocked_away", "unlocked_at_home_night")]


def test_watcher_does_not_alert_open_door_from_a_stale_opening_reading(stub_service, stub, clock):
    stub.values.update(doors_open=["front_left"], location_zone="away")
    stub.ages["doors_open"] = 7200.0
    s = stub_service(armed=False, watcher_enabled=True)
    s.watcher.poll_once()
    clock.advance(3600)
    stub.ages["doors_open"] = 7200.0 + 3600
    s.watcher.poll_once()
    assert not [a for a in s.alerts(("new", "surfaced", "acked")) if a["rule"] == "opening_left_open"]


# -- pages -------------------------------------------------------------------------------------------------------
@needs_desktop_render
def test_desktop_page_stale_lock_badge_and_buttons(service, tmp_path):
    mod, summary = backend_summary(service)
    st = summary["status"]
    st["security"].update(locked=True)
    st["freshness"]["stale_fields"] = {"locked": 2130}
    page = render(tmp_path, summary, rules=mod.rules())["page"]
    assert "as of 36 min ago" in page
    assert "Already locked" not in page            # a stale "locked" reading must not grey out the Lock button


@needs_web_render
def test_web_page_stale_lock_badge_and_buttons(service, tmp_path):
    mod, summary = backend_summary(service)
    st = summary["status"]
    st["security"].update(locked=True)
    st["freshness"]["stale_fields"] = {"locked": 2130}
    out = render_web(tmp_path, {"/summary": summary, "/log": {"ok": True, "rows": []}})
    blob = out["html"]
    assert "as of 36 min ago" in blob and "Already locked" not in blob
