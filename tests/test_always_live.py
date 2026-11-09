"""Worker 11 (owner decision 2026-11-09): live mode "always" vs "on_demand", owner-adjustable protections with fixed
invariants, the mall case (walk back to the parked, sleeping car and open the trunk), and no waiting for a read-back that
cannot arrive in time."""

import pytest

from pit_crew.safety.approval import ApprovalEvidence

HUMAN = ApprovalEvidence("hermes_approval", "once")


def last_audit(svc):
    return svc.store.audit_recent(1)[0]


# == Part 1: live_mode ===================================================================================================
def test_fresh_install_is_on_demand_practice(stub_service, stub):
    svc = stub_service()
    info = svc.armed_info()
    assert svc.settings.get("live_mode") == "on_demand" and info["armed"] is False and info["state"] == "practice"
    assert svc.run_command("lock", {}, surface="tool")["status"] == "dry_run" and stub.sent == []


def test_always_mode_sends_without_arming(stub_service, stub):
    svc = stub_service(live_mode="always")
    stub.values["locked"] = False
    info = svc.armed_info()
    assert info["armed"] is True and info["state"] == "always" and info["until"] is None
    res = svc.run_command("lock", {}, surface="tool")
    assert res["status"] == "confirmed" and stub.sent == [("lock", {})]
    assert not [r for r in svc.store.audit_recent(10) if r["command"] == "arm"]      # nothing armed it


def test_always_mode_stop_pauses_and_live_resumes_with_no_expiry(stub_service, stub, clock):
    svc = stub_service(live_mode="always")
    info = svc.disarm(by="test")
    assert info["armed"] is False and info["state"] == "paused" and info["paused"] is True
    assert svc.live_label() == "Paused (practice) — /car live to resume"
    res = svc.run_command("lock", {}, surface="tool")
    assert res["status"] == "dry_run" and stub.sent == []
    assert "paused" in res["message"] and "/car live" in res["message"]
    clock.advance(30 * 86400)
    assert svc.armed_info()["state"] == "paused"                   # a pause lasts until the user resumes
    info = svc.arm(3, by="test")                                    # a number does not put a limit on "always"
    assert info["armed"] and info["state"] == "always" and info["until"] is None and info["hours"] is None
    clock.advance(365 * 86400)
    assert svc.armed_info()["armed"] is True and svc.live_label() == "Live: always on"
    assert svc.run_command("lock", {}, surface="tool")["status"] in ("confirmed", "duplicate")
    assert [r["command"] for r in svc.store.audit_recent(5) if r["command"] in ("arm", "disarm")][:2] == ["arm", "disarm"]


def test_on_demand_is_unchanged(stub_service, stub, clock):
    svc = stub_service()
    info = svc.arm(2, by="test")
    assert info["state"] == "timed" and info["until"] and svc.live_label().startswith("Live until ")
    clock.advance(2 * 3600 + 1)
    assert svc.armed_info()["armed"] is False and svc.armed_info()["state"] == "practice"
    svc.arm(1, by="test")
    info = svc.disarm(by="test")
    assert info["state"] == "practice" and info["paused"] is False
    assert svc.live_label() == "Practice mode · nothing is sent"


def test_switching_the_setting_applies_without_a_restart(store, clock, stub):
    from pit_crew.adapters import base
    from pit_crew.service import CarService
    from pit_crew.settings import Settings
    import conftest

    conftest._STUB_HOLDER["adapter"] = stub
    if "stub" not in base._ADAPTERS:
        base.register_adapter("stub", lambda ctx: conftest._STUB_HOLDER["adapter"])
    cfg = {"adapter": "stub", "history": "none"}
    svc = CarService(Settings(lambda: dict(cfg)), {}.get, store, wall_clock=clock, sleep=clock.sleep)
    assert svc.armed_info()["armed"] is False
    cfg["live_mode"] = "always"                       # the user picks "always" in the settings form
    assert svc.armed_info()["state"] == "always"
    svc.disarm(by="t")
    cfg["live_mode"] = "on_demand"
    assert svc.armed_info()["state"] == "practice"
    cfg["live_mode"] = "always"
    assert svc.armed_info()["state"] == "paused"      # the pause is remembered
    cfg["live_mode"] = "on_demand"
    svc.disarm(by="t")                                 # a stop in on_demand leaves no pause behind ...
    cfg["live_mode"] = "always"
    assert svc.armed_info()["state"] == "always"      # ... so choosing "always" later starts live


def test_old_armed_setting_migrates_to_always():
    from pit_crew.settings import Settings
    assert Settings(lambda: {"armed": True}).get("live_mode") == "always"
    assert Settings(lambda: {"armed": "true"}).get("live_mode") == "always"
    assert Settings(lambda: {"armed": True, "live_mode": "on_demand"}).get("live_mode") == "on_demand"
    assert Settings(lambda: {"armed": False}).get("live_mode") == "on_demand"
    assert Settings(lambda: {"live_mode": "ALWAYS"}).get("live_mode") == "always"
    assert Settings(lambda: {"live_mode": "forever"}).get("live_mode") == "on_demand"     # unknown -> conservative


def test_always_mode_still_refuses_approval_class_in_unattended_runs(stub_service, stub):
    from pit_crew.safety.gate import ApprovalGate
    from conftest import CronCtx

    svc = stub_service(live_mode="always")
    res = svc.run_command("unlock", {}, surface="cron", approval=HUMAN, unattended=True)
    assert res["status"] == "blocked" and res["approval"] == "unattended" and stub.sent == []
    assert "No setting changes this" in res["message"]
    svc.ctx = CronCtx(cron=True)
    d = ApprovalGate(lambda: svc).pre_tool_call("car_command", {"command": "open_trunk"}, task_id="t")
    assert d["action"] == "block" and "unattended" in d["message"] and stub.sent == []
    # comfort commands may still run unattended, as before
    assert svc.run_command("set_temp", {"temp_f": 70}, surface="cron", unattended=True)["status"] == "confirmed"


def test_always_mode_keeps_every_other_check(stub_service, stub):
    svc = stub_service(live_mode="always")
    assert svc.run_command("unlock", {}, surface="tool")["approval"] == "missing"          # still needs Hermes' approval
    stub.values.update(moving=True, speed_mph=30.0)
    assert svc.run_command("unlock", {}, surface="tool", approval=HUMAN)["status"] == "blocked"
    assert stub.sent == []


def test_no_tool_can_arm_or_change_settings():
    import json
    from pit_crew import schemas, tools

    names = {t["name"] for t in (schemas.CAR_STATUS, schemas.CAR_HISTORY, schemas.CAR_READINESS, schemas.CAR_COMMAND,
                                 schemas.CAR_ALERTS, schemas.CAR_RULES)}
    assert names == {"car_status", "car_history", "car_readiness", "car_command", "car_alerts", "car_rules"}
    assert "arm" not in json.dumps(schemas.CAR_COMMAND["parameters"]["properties"]["command"].get("enum", []))
    src = open(tools.__file__).read()
    assert ".arm(" not in src and ".disarm(" not in src and "set_config" not in src


def test_car_command_cannot_arm(stub_service, stub):
    from pit_crew import tools
    import json

    svc = stub_service()
    for cmd in ("arm", "live", "go_live"):
        out = json.loads(tools.car_command({"command": cmd}))
        assert out.get("status") in ("invalid", None) and not out.get("ok")
    assert svc.armed_info()["armed"] is False


# -- /car in always mode -------------------------------------------------------------------------------------------------
def test_slash_stop_and_live_in_always_mode(stub_service, stub):
    from pit_crew import slash

    svc = stub_service(live_mode="always")
    svc.ctx = None                                         # no ask_choice here: needs the typed confirmation
    assert "Live: always on" in slash.handle("")
    assert slash.handle("stop") == "Paused (practice) — /car live to resume. Nothing will be sent to the car until then."
    assert "Paused (practice) — /car live to resume" in slash.handle("")
    out = slash.handle("live")
    assert "/car live confirm" in out and svc.armed_info()["armed"] is False      # asks first, does not arm
    out = slash.handle("live 3 confirm")
    assert out.startswith("Live: always on again (no time limit)") and "3 h you gave doesn't apply" in out
    assert svc.armed_info()["state"] == "always"
    assert "always on already" in slash.handle("live")


def test_slash_on_demand_wording_unchanged(stub_service, stub):
    from pit_crew import slash

    svc = stub_service()
    svc.ctx = None
    assert "Mode: Practice" in slash.handle("")
    assert slash.handle("live 2 confirm").startswith("Live for 2 h")
    assert "Mode: Live until " in slash.handle("")
    assert slash.handle("stop") == "Back to practice mode. Nothing will be sent to the car."


def test_page_backend_reports_the_state(stub_service, stub):
    from pit_crew.dashboard import plugin_api as api

    stub_service(live_mode="always")
    s = api.summary()["armed"]
    assert s["state"] == "always" and s["label"] == "Live: always on" and s["live_mode"] == "always"
    assert api.disarm()["label"] == "Paused (practice) — /car live to resume"
    out = api.arm({"confirmed": True})
    assert out["ok"] and out["state"] == "always" and out["until_local"] is None
    assert api.arm({})["ok"] is False                     # still needs the confirmation click


# == Part 2: owner-adjustable protections ================================================================================
@pytest.mark.parametrize("key,value,expect", [
    ("max_state_age_s", 0, 60), ("max_state_age_s", 10 ** 9, 3600), ("max_state_age_s", 900, 900),
    ("rate_limit_commands", 0, 1), ("rate_limit_commands", 1000, 60), ("rate_limit_window_s", 1, 60),
    ("dedupe_window_s", 0, 5), ("dedupe_window_s", 99999, 600), ("dedupe_window_s", "abc", 60),
])
def test_numeric_protections_are_bounded(key, value, expect):
    from pit_crew.settings import Settings
    assert Settings(lambda: {key: value}).get(key) == expect


def test_choice_settings_default_conservative():
    from pit_crew.settings import Settings
    s = Settings(lambda: {})
    assert (s.get("live_mode"), s.get("zone_rule"), s.get("wake_for_commands")) == ("on_demand", "trusted_zones", "ask")
    assert Settings(lambda: {"zone_rule": "everywhere"}).get("zone_rule") == "trusted_zones"


def mall(stub, *, asleep=True):
    """Parked at the mall: away from home, asleep, gear/speed last reported 3 h ago (P / 0), not plugged in."""
    stub.values.update(location_zone="away", asleep=asleep, plugged_in=False, charging_state="disconnected",
                       shift_state="P", speed_mph=0.0, moving=False, trunk_open=False)
    for f in ("shift_state", "speed_mph", "moving", "doors_open", "windows_open"):
        stub.ages[f] = 3 * 3600


def test_mall_case_away_anywhere_asleep_auto_wake_trunk_sends(stub_service, stub):
    svc = stub_service(live_mode="always", zone_rule="anywhere", wake_for_commands="auto")
    mall(stub)
    woke = []
    stub.allow_wake_once = lambda: woke.append(True)         # a direct adapter that would otherwise refuse to wake
    res = svc.run_command("open_trunk", {}, surface="tool", approval=HUMAN)
    assert res["status"] == "confirmed", res["message"]
    assert stub.sent == [("open_trunk", {})] and woke == [True]
    checks = {c["check"]: c for c in res["policy"]["checks"]}
    assert checks["parked"]["ok"] and checks["trusted_zone"]["ok"] and "anywhere" in checks["trusted_zone"]["detail"]
    assert any("asleep" in w for w in res["policy"]["warnings"])


def test_mall_case_with_trusted_zones_is_refused_naming_the_setting(stub_service, stub):
    svc = stub_service(live_mode="always", wake_for_commands="auto")
    mall(stub)
    res = svc.run_command("open_trunk", {}, surface="tool", approval=HUMAN)
    assert res["status"] == "blocked" and stub.sent == []
    msg = res["message"]
    assert msg.startswith("Refused: the car is at 'away', away from your trusted zones")
    assert "Unlock/trunk/frunk zone" in msg and "zone_rule" in msg and "'anywhere'" in msg


def test_asleep_car_with_wake_ask_is_refused_naming_the_setting(stub_service, stub):
    svc = stub_service(live_mode="always", zone_rule="anywhere")
    mall(stub)
    res = svc.run_command("open_trunk", {}, surface="tool", approval=HUMAN)
    assert res["status"] == "blocked" and stub.sent == []
    assert "wake_for_commands" in res["message"] and "'auto'" in res["message"]
    # the agent's explicit wake_ok (after asking the user) still works with "ask"
    assert svc.run_command("open_trunk", {"wake_ok": True}, surface="tool", approval=HUMAN)["status"] == "confirmed"


@pytest.mark.parametrize("moving", [
    dict(speed_mph=25.0, moving=True),                       # fresh: driving
    dict(shift_state="D"),                                   # gear says Drive (even with a fresh "asleep")
    dict(shift_state="R"), dict(shift_state="N"),
])
def test_moving_is_refused_regardless_of_settings(stub_service, stub, moving):
    svc = stub_service(live_mode="always", zone_rule="anywhere", wake_for_commands="auto", allow_unknown_motion=True,
                       max_state_age_s=3600)
    mall(stub)
    stub.values.update(moving)
    for cmd in ("open_trunk", "unlock", "open_frunk"):
        res = svc.run_command(cmd, {}, surface="tool", approval=HUMAN)
        assert res["status"] == "blocked" and "moving" in res["message"] and "no setting changes that" in res["message"]
    assert stub.sent == []


def test_stale_asleep_reading_is_not_parked_evidence(stub_service, stub):
    svc = stub_service(live_mode="always", zone_rule="anywhere", wake_for_commands="auto")
    mall(stub)
    stub.ages["asleep"] = 3 * 3600
    stub.ages["battery_pct"] = 0                              # data otherwise fresh
    res = svc.run_command("open_trunk", {}, surface="tool", approval=HUMAN)
    assert res["status"] == "blocked" and "cannot confirm the car is parked" in res["message"]
    assert "allow_unknown_motion" in res["message"] and stub.sent == []


def test_awake_car_is_not_parked_by_asleep_rule(stub_service, stub):
    svc = stub_service(live_mode="always", zone_rule="anywhere")
    mall(stub, asleep=False)                                  # just woken / awake: stale P proves nothing
    res = svc.run_command("open_trunk", {}, surface="tool", approval=HUMAN)
    assert res["status"] == "blocked" and "cannot confirm the car is parked" in res["message"]


def test_rate_and_duplicate_messages_name_their_settings(stub_service, stub):
    svc = stub_service(live_mode="always", rate_limit_commands=1)
    assert svc.run_command("set_temp", {"temp_f": 70}, surface="tool")["status"] == "confirmed"
    res = svc.run_command("set_temp", {"temp_f": 71}, surface="tool")
    assert res["status"] == "rate_limited" and "rate_limit_commands" in res["message"]
    svc2 = stub_service(live_mode="always", rate_limit_commands=10)
    svc2.run_command("set_temp", {"temp_f": 72}, surface="tool")
    dup = svc2.run_command("set_temp", {"temp_f": 72}, surface="tool")
    assert dup["status"] == "duplicate" and "dedupe_window_s" in dup["message"]


def test_stale_state_refusal_names_the_setting(stub_service, stub):
    svc = stub_service(live_mode="always")
    stub.ages["*"] = 7200
    res = svc.run_command("unlock", {}, surface="tool", approval=HUMAN)
    assert res["status"] == "blocked" and "max_state_age_s" in res["message"]


def test_require_human_for_refusal_names_the_setting(stub_service, stub):
    svc = stub_service(live_mode="always", require_human_for=["unlock"])
    res = svc.run_command("unlock", {}, surface="tool")
    assert res["approval"] == "missing" and "require_human_for" in res["message"]


def test_practice_message_points_at_the_live_mode_setting(stub_service):
    res = stub_service().run_command("lock", {}, surface="tool")
    assert res["status"] == "dry_run" and "Safety · Live mode" in res["message"] and "'always'" in res["message"]


# == Part 3: no waiting for a report that cannot arrive in time ==========================================================
def test_slow_reporting_profile_answers_after_one_read(stub_service, stub, clock):
    svc = stub_service(live_mode="always")
    stub.report_latency_s = lambda: 600.0
    stub.service_name = lambda: "Tesla"
    stub.apply_commands = False                                # the car's report has not arrived
    t0 = clock()
    res = svc.run_command("open_trunk", {}, surface="tool", approval=HUMAN)
    assert clock() == t0                                       # no read-back wait at all
    assert res["status"] == "sent_unconfirmed" and res["readback"]["status"] == "accepted" and res["readback"]["quick"]
    assert "Tesla accepted it" in res["message"] and "~10 min" in res["message"] and "car_status" in res["message"]
    assert last_audit(svc)["readback"] == "accepted"
    # charging: the 90 s lag window doesn't help a car that reports every 10 minutes either
    res = svc.run_command("charge_start", {}, surface="tool")
    assert clock() == t0 and res["readback"]["status"] == "accepted"


def test_slow_profile_still_confirms_when_the_one_read_shows_the_car_report(stub_service, stub, clock):
    svc = stub_service(live_mode="always")
    stub.report_latency_s = lambda: 600.0
    t0 = clock()
    res = svc.run_command("open_trunk", {}, surface="tool", approval=HUMAN)
    assert res["status"] == "confirmed" and clock() == t0


def test_fast_reporting_profile_keeps_the_full_wait(stub_service, stub, clock):
    svc = stub_service(live_mode="always", verify_timeout_s=6, verify_interval_s=2)
    stub.report_latency_s = lambda: 5.0
    stub.apply_commands = False
    stub.values["locked"] = False
    t0 = clock()
    res = svc.run_command("lock", {}, surface="tool")
    assert res["readback"]["status"] == "not_confirmed" and 6 <= clock() - t0 <= 9


def test_bundled_profiles_declare_report_latency():
    from pit_crew.adapters.profile import load_profile, validate_profile
    assert load_profile("tesla_fleet")["report_latency_s"] == 600
    assert load_profile("teslemetry_ha")["report_latency_s"] <= 10
    assert load_profile("tessie_ha")["report_latency_s"] <= 60
    assert validate_profile({**load_profile("tesla_fleet"), "report_latency_s": -1})


# == pages: which state applies (Desktop page + chip, web dashboard) =====================================================
from pagerender import backend_summary, needs_desktop_render, needs_web_render, render, render_web  # noqa: E402


def _state_summary(make_service, live_mode, action):
    svc = make_service({"live_mode": live_mode})
    if action == "stop":
        svc.disarm(by="t")
    elif action == "live":
        svc.arm(2, by="t")
    mod, summary = backend_summary(svc)
    return svc, mod, summary


@needs_desktop_render
@pytest.mark.parametrize("live_mode,action,expect,button", [
    ("always", None, "Live: always on", ">Stop<"),
    ("always", "stop", "Paused (practice) — /car live to resume", "Go live…"),
    ("on_demand", "live", "Live until ", ">Stop<"),
    ("on_demand", None, "Practice mode · nothing is sent", "Go live…"),
])
def test_desktop_page_and_chip_say_which_state_applies(make_service, tmp_path, live_mode, action, expect, button):
    pytest.importorskip("fastapi")
    svc, mod, summary = _state_summary(make_service, live_mode, action)
    out = render(tmp_path, summary, rules=mod.rules())
    page = out["page"].replace("</i>", "")
    assert expect in page and button in page
    assert "LIVE until" not in page
    if live_mode == "always" and action is None:
        assert "Practice mode · nothing is sent" not in page
    chip_title = out["chip"]
    assert expect in chip_title                       # the status-bar chip's tooltip names the state too


@needs_web_render
@pytest.mark.parametrize("live_mode,action,expect,button", [
    ("always", None, "Live: always on", ">Stop<"),
    ("always", "stop", "Paused (practice) — /car live to resume", "Go live…"),
    ("on_demand", "live", "Live until ", ">Stop<"),
    ("on_demand", None, "Practice mode · nothing is sent", "Go live…"),
])
def test_web_page_says_which_state_applies(make_service, tmp_path, live_mode, action, expect, button):
    pytest.importorskip("fastapi")
    svc, mod, summary = _state_summary(make_service, live_mode, action)
    out = render_web(tmp_path, {"/summary": summary, "/log": {"ok": True, "rows": []}})
    html = out["html"]
    assert expect in html and button in html and "LIVE until" not in html
    if action == "stop":
        assert "Use Go live at the top to resume" in html
    assert [c for c in out["calls"] if c[1] == "POST"] == []
