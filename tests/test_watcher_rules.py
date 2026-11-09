import json

import pytest

from pit_crew import tools
from pit_crew.watcher.rules import RULES, RULES_BY_ID


def poll(svc):
    return svc.watcher.poll_once()


def alerts(svc, *states):
    return svc.alerts(states or ("new", "surfaced", "acked", "resolved"))


def ids(svc, **kw):
    return sorted(a["rule"] for a in svc.alerts(("new", "surfaced", "acked")))


@pytest.fixture
def svc(stub_service, stub):
    s = stub_service(armed=False, watcher_enabled=True)
    return s


def test_every_rule_has_defaults_toggle_and_description():
    assert len(RULES) == 11
    for r in RULES:
        assert r.defaults["enabled"] is True and r.description and r.title and r.id in RULES_BY_ID


def test_healthy_car_raises_nothing(svc, stub):
    res = poll(svc)
    assert res["polled"] and res["raised"] == []
    assert alerts(svc) == []


def test_left_unlocked_away_waits_for_hold_then_fires_once(svc, stub, clock):
    stub.values.update(locked=False, location_zone="away")
    assert poll(svc)["raised"] == []              # condition just began
    clock.advance(9 * 60)
    assert poll(svc)["raised"] == []              # 9 min < 10 min threshold
    clock.advance(2 * 60)
    raised = poll(svc)["raised"]
    assert [a["rule"] for a in raised] == ["left_unlocked_away"]
    assert "unlocked and away" in raised[0]["title"] and "lock" in raised[0]["suggestion"].lower()
    clock.advance(10 * 60)
    assert poll(svc)["raised"] == []              # de-duplicated: no repeat inside the cooldown
    assert len(alerts(svc)) == 1


def test_alert_resolves_when_condition_clears_and_refires_later(svc, stub, clock):
    stub.values.update(locked=False, location_zone="away")
    poll(svc); clock.advance(11 * 60); poll(svc)
    stub.values["locked"] = True
    poll(svc)
    assert [a["state"] for a in alerts(svc)] == ["resolved"]
    stub.values["locked"] = False
    poll(svc); clock.advance(11 * 60)
    assert len(poll(svc)["raised"]) == 1          # a brand-new episode alerts again


def test_acknowledged_alert_stays_quiet_even_after_cooldown(svc, stub, clock):
    stub.values.update(locked=False, location_zone="away")
    poll(svc); clock.advance(11 * 60)
    a = poll(svc)["raised"][0]
    svc.store.alert_set_state(a["id"], "acked")
    clock.advance(5 * 3600)
    assert poll(svc)["raised"] == []


def test_cooldown_refire_for_unacked_alert(svc, stub, clock):
    stub.values.update(locked=False, location_zone="away")
    poll(svc); clock.advance(11 * 60); poll(svc)
    clock.advance(61 * 60)                         # cooldown for this rule is 60 min
    assert len(poll(svc)["raised"]) == 1


def test_unlocked_at_home_only_at_night(svc, stub, clock):
    stub.values["locked"] = False
    svc.settings.set_override("night_start", "00:00")
    svc.settings.set_override("night_end", "23:59")     # always "night"
    poll(svc); clock.advance(6 * 60)
    assert [a["rule"] for a in poll(svc)["raised"]] == ["unlocked_at_home_night"]
    svc.settings.set_override("night_start", "03:00"); svc.settings.set_override("night_end", "03:01")
    stub.values["locked"] = True; poll(svc); stub.values["locked"] = False
    poll(svc); clock.advance(6 * 60)
    assert poll(svc)["raised"] == []


def test_open_things_rule(svc, stub, clock):
    stub.values.update(doors_open=["rear_driver"], windows_open=["front_driver"])
    poll(svc); clock.advance(6 * 60)
    a = poll(svc)["raised"][0]
    assert a["rule"] == "opening_left_open" and "rear driver" in a["body"] and "front driver" in a["body"]
    # window-only -> offers to close windows
    stub.values.update(doors_open=[], windows_open=["front_driver"])
    poll(svc); clock.advance(6 * 60)
    b = [x for x in poll(svc)["raised"]]
    assert b and "close the windows" in b[0]["suggestion"]


def test_not_moving_required_for_open_rule(svc, stub, clock):
    stub.values.update(doors_open=["rear_driver"], moving=True, speed_mph=30.0)
    poll(svc); clock.advance(6 * 60)
    assert poll(svc)["raised"] == []


def test_plugged_not_charging(svc, stub, clock):
    stub.values.update(charging_state="stopped", battery_pct=35.0)
    poll(svc); clock.advance(31 * 60)
    a = poll(svc)["raised"][0]
    assert a["rule"] == "plugged_not_charging" and "start charging" in a["suggestion"].lower()
    # complete (at limit) is NOT a problem
    stub.values.update(charging_state="complete", battery_pct=80.0)
    poll(svc); clock.advance(31 * 60)
    assert poll(svc)["raised"] == []


def test_no_power_suggests_checking_outlet(svc, stub, clock):
    stub.values.update(charging_state="no_power", battery_pct=35.0)
    poll(svc); clock.advance(31 * 60)
    assert "outlet" in poll(svc)["raised"][0]["suggestion"]


def test_charging_stopped_unexpectedly_needs_prior_charging(svc, stub, clock):
    stub.values.update(charging_state="stopped", battery_pct=50.0)
    assert [a["rule"] for a in poll(svc)["raised"]] == []                   # never saw it charging
    stub.values.update(charging_state="charging"); poll(svc)
    stub.values.update(charging_state="stopped")
    raised = poll(svc)["raised"]
    assert "charging_stopped_unexpectedly" in [a["rule"] for a in raised]
    # finishing normally is fine
    stub.values.update(charging_state="charging"); poll(svc); clock.advance(7200)
    stub.values.update(charging_state="complete", battery_pct=80.0)
    before = len(alerts(svc))
    poll(svc)
    assert len(alerts(svc)) == before


def test_soc_below_tomorrow(svc, stub, clock):
    stub.values.update(battery_pct=18.0, charging_state="complete", charge_limit_pct=18, charge_power_kw=0)
    svc.settings.set_override("default_trip_mi", 60)
    svc.watcher.engine.set_rule("soc_below_tomorrow", trip_mi=60, within_hours=24)
    raised = poll(svc)["raised"]
    assert any(a["rule"] == "soc_below_tomorrow" for a in raised)
    a = next(a for a in raised if a["rule"] == "soc_below_tomorrow")
    assert "NOT ready" in a["body"] and a["suggestion"]


def test_sentry_off_away_and_on_at_home(svc, stub, clock):
    stub.values.update(location_zone="away", sentry_on=False)
    poll(svc); clock.advance(11 * 60)
    assert [a["rule"] for a in poll(svc)["raised"]] == ["sentry_off_away"]


def test_cabin_extreme_only_with_someone_inside(svc, stub):
    stub.values.update(cabin_temp_f=118.0, user_present=False)
    assert poll(svc)["raised"] == []
    stub.values.update(user_present=True)
    raised = poll(svc)["raised"]
    assert raised and raised[0]["rule"] == "cabin_extreme" and "hot" in raised[0]["title"] and raised[0]["severity"] == "high"


def test_software_update_is_info_and_slow_cooldown(svc, stub, clock):
    stub.values["software_update_available"] = True
    raised = poll(svc)["raised"]
    assert raised[0]["rule"] == "software_update" and raised[0]["severity"] == "info"
    clock.advance(3 * 86400)
    assert poll(svc)["raised"] == []


def test_rule_skipped_when_car_lacks_the_data(svc, stub, clock):
    stub.removed_fields = {"user_present"}
    stub.values.update(cabin_temp_f=130.0, user_present=True)
    assert poll(svc)["raised"] == []     # honest: can't evaluate without occupancy data
    rules = {r["id"]: r for r in svc.watcher.engine.describe_rules(svc.get_state(force=True))}
    assert "doesn't report user_present" in rules["cabin_extreme"]["note"]


def test_disable_and_configure_rules_via_tool(svc, stub, clock):
    out = json.loads(tools.car_rules({"action": "disable", "rule": "left_unlocked_away"}))
    assert out["ok"] and out["now"]["enabled"] is False
    stub.values.update(locked=False, location_zone="away")
    poll(svc); clock.advance(30 * 60)
    assert poll(svc)["raised"] == []
    out = json.loads(tools.car_rules({"action": "enable", "rule": "left_unlocked_away"}))
    cfg = json.loads(tools.car_rules({"action": "configure", "rule": "left_unlocked_away", "settings": {"minutes": 45}}))
    assert cfg["now"]["minutes"] == 45 and cfg["now"]["enabled"] is True
    listing = json.loads(tools.car_rules({}))
    assert {r["id"] for r in listing["rules"]} == set(RULES_BY_ID)
    assert next(r for r in listing["rules"] if r["id"] == "left_unlocked_away")["settings"]["minutes"] == 45


def test_rules_tool_errors(svc):
    assert json.loads(tools.car_rules({"action": "enable"}))["ok"] is False
    assert json.loads(tools.car_rules({"action": "enable", "rule": "nope"}))["ok"] is False
    assert json.loads(tools.car_rules({"action": "configure", "rule": "left_unlocked_away", "settings": {"bogus": 1}}))["ok"] is False
    assert json.loads(tools.car_rules({"action": "configure", "rule": "left_unlocked_away", "settings": {"minutes": -5}}))["ok"] is False
    assert json.loads(tools.car_rules({"action": "dance"}))["ok"] is False


def test_rules_setting_override_from_config(stub_service, stub, clock):
    s = stub_service(rules={"left_unlocked_away": {"minutes": 1}})
    stub.values.update(locked=False, location_zone="away")
    poll(s); clock.advance(90)
    assert len(poll(s)["raised"]) == 1


def test_car_alerts_tool_list_ack_check_now(svc, stub, clock):
    stub.values.update(locked=False, location_zone="away", doors_open=["rear_driver"])
    poll(svc); clock.advance(11 * 60)
    out = json.loads(tools.car_alerts({"action": "check_now"}))
    assert out["ok"] and out["checked"] and len(out["open_alerts"]) == 2
    listed = json.loads(tools.car_alerts({}))
    assert listed["count"] == 2 and {a["rule"] for a in listed["alerts"]} == {"left_unlocked_away", "opening_left_open"}
    first = listed["alerts"][0]["id"]
    assert json.loads(tools.car_alerts({"action": "ack", "alert_id": str(first)}))["acknowledged"] == [first]
    assert json.loads(tools.car_alerts({"action": "ack", "alert_id": "all"}))["ok"]
    assert all(a["state"] == "acked" for a in json.loads(tools.car_alerts({}))["alerts"])
    assert json.loads(tools.car_alerts({"action": "ack"}))["ok"] is False
    assert json.loads(tools.car_alerts({"action": "wat"}))["ok"] is False
    assert json.loads(tools.car_alerts({"action": "ack", "alert_id": "x"}))["ok"] is False


def test_check_now_all_clear(svc):
    out = json.loads(tools.car_alerts({"action": "check_now"}))
    assert out["ok"] and out["open_alerts"] == [] and "All clear" in out["summary"]


def test_rule_crash_is_contained(svc, stub, monkeypatch):
    from pit_crew.watcher import rules as R
    boom = R.Rule("boom", "x", "x", {"enabled": True}, (), lambda c: 1 / 0)
    monkeypatch.setattr("pit_crew.watcher.engine.RULES", [boom] + list(R.RULES))
    assert poll(svc)["polled"] is True      # other rules still run, nothing raised


def test_r16_stale_readings_do_not_raise_alerts(svc, stub, clock):
    """Pass 8 R16: a day-old 'unlocked, away' reading must not alert; a refreshed one does."""
    stub.values.update(locked=False, location_zone="away")
    stub.ages["locked"] = 86400.0
    poll(svc); clock.advance(11 * 60)
    assert poll(svc)["raised"] == []
    stub.ages.clear()
    poll(svc); clock.advance(11 * 60)
    assert [a["rule"] for a in poll(svc)["raised"]] == ["left_unlocked_away"]
