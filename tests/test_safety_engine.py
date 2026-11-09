import json

import pytest

from pit_crew.safety.approval import ApprovalEvidence

HUMAN = ApprovalEvidence("hermes_approval", "once")


def last_audit(svc):
    return svc.store.audit_recent(1)[0]


# -- dry run ---------------------------------------------------------------------------------------
def test_dry_run_is_default_and_never_contacts_car(stub_service, stub):
    svc = stub_service()
    res = svc.run_command("lock", {}, surface="tool")
    assert res["status"] == "dry_run" and res["dry_run"] is True and res["ok"] is True
    assert "NOT contacted" in res["message"] and "/car live" in res["message"]
    assert stub.sent == []
    row = last_audit(svc)
    assert row["mode"] == "dry_run" and row["status"] == "dry_run" and row["command"] == "lock"


def test_dry_run_still_enforces_policy(stub_service, stub):
    svc = stub_service()
    stub.values["moving"] = True
    stub.values["speed_mph"] = 40.0
    res = svc.run_command("unlock", {}, surface="tool")
    assert res["status"] == "blocked" and "moving" in res["message"]
    assert last_audit(svc)["policy"] == "denied" and stub.sent == []


def test_dry_run_mentions_approval_requirement(stub_service):
    res = stub_service().run_command("unlock", {}, surface="tool")
    assert res["status"] == "dry_run" and "approval" in res["message"]


# -- arming ------------------------------------------------------------------------------------------
def test_arm_disarm_lifecycle_and_expiry(stub_service, clock):
    svc = stub_service()
    assert svc.armed_info()["armed"] is False
    info = svc.arm(2, by="test")
    assert info["armed"] and info["source"] == "runtime"
    clock.advance(3 * 3600)
    assert svc.armed_info()["armed"] is False  # auto-disarm after the window
    svc.arm(1)
    assert svc.disarm(by="test")["armed"] is False
    assert [r["command"] for r in svc.store.audit_recent(5)][:3] == ["disarm", "arm", "arm"]


def test_standing_arm_setting_and_disarm_override(stub_service):
    svc = stub_service()
    svc.settings.set_override("armed", True)
    assert svc.armed_info()["armed"] and svc.armed_info()["source"] == "setting"
    svc.disarm(by="test")
    assert svc.armed_info()["armed"] is False  # /car disarm beats the standing setting
    svc.arm(1)
    assert svc.armed_info()["armed"] is True


# -- live commands -------------------------------------------------------------------------------------
def test_comfort_command_live_with_readback(stub_service, stub):
    svc = stub_service(armed=True)
    res = svc.run_command("set_temp", {"temp_f": 72}, surface="tool")
    assert res["status"] == "confirmed" and res["ok"] and res["readback"]["status"] == "confirmed"
    assert "confirmed it at" in res["message"]
    assert stub.sent == [("set_temp", {"temp_f": 72.0})]
    row = last_audit(svc)
    assert row["mode"] == "live" and row["readback"] == "confirmed" and row["approval"] == "not_required"


def test_safe_direction_lock_needs_no_approval(stub_service, stub):
    svc = stub_service(armed=True)
    stub.values["locked"] = False
    res = svc.run_command("lock", {}, surface="tool")
    assert res["status"] == "confirmed" and stub.values["locked"] is True


@pytest.mark.parametrize("cmd", ["unlock", "open_trunk", "open_frunk", "vent_windows", "sentry_off", "honk", "flash"])
def test_approval_class_fail_closed_without_evidence(stub_service, stub, cmd):
    svc = stub_service(armed=True)
    stub.values["sentry_on"] = True
    res = svc.run_command(cmd, {}, surface="tool")
    assert res["status"] == "blocked" and res["approval"] == "missing" and "fail closed" in res["message"]
    assert stub.sent == []
    assert last_audit(svc)["approval"] == "missing"


def test_unlock_with_human_approval_confirmed_and_audited(stub_service, stub):
    svc = stub_service(armed=True)
    res = svc.run_command("unlock", {}, surface="tool", session="s1", reason="user asked", approval=HUMAN)
    assert res["status"] == "confirmed" and stub.values["locked"] is False and res["approval"] == "confirmed"
    row = last_audit(svc)
    assert row["approval"] == "confirmed" and row["session"] == "s1" and "user asked" in row["detail"]


def test_declined_approval_blocks(stub_service, stub):
    svc = stub_service(armed=True)
    res = svc.run_command("unlock", {}, surface="tool", approval=ApprovalEvidence("hermes_approval", "declined", denied=True))
    assert res["status"] == "blocked" and res["approval"] == "denied" and stub.sent == []


def test_unattended_fails_closed_even_with_evidence(stub_service, stub):
    svc = stub_service(armed=True)
    res = svc.run_command("unlock", {}, surface="cron", approval=HUMAN, unattended=True)
    assert res["status"] == "blocked" and res["approval"] == "unattended" and "no human" in res["message"]
    assert stub.sent == []
    # comfort commands may run unattended
    ok = svc.run_command("set_temp", {"temp_f": 70}, surface="cron", unattended=True)
    assert ok["status"] == "confirmed"


# -- policy ---------------------------------------------------------------------------------------------
def test_moving_blocks_security_commands(stub_service, stub):
    svc = stub_service(armed=True)
    stub.values.update(moving=True, speed_mph=35.0, shift_state="D")
    for cmd in ("unlock", "open_trunk", "open_frunk", "vent_windows"):
        res = svc.run_command(cmd, {}, surface="tool", approval=HUMAN)
        assert res["status"] == "blocked" and "moving" in res["message"], cmd
    assert stub.sent == []


def test_stale_state_blocks_risky_but_not_comfort(stub_service, stub):
    svc = stub_service(armed=True)
    stub.ages["*"] = 3600.0
    res = svc.run_command("unlock", {}, surface="tool", approval=HUMAN)
    assert res["status"] == "blocked" and "old" in res["message"]
    assert svc.run_command("set_temp", {"temp_f": 70}, surface="tool")["ok"] is True


def test_untrusted_zone_unlock_refused_unless_override(stub_service, stub):
    svc = stub_service(armed=True)
    stub.values["location_zone"] = "away"
    res = svc.run_command("unlock", {}, surface="tool", approval=HUMAN)
    assert res["status"] == "blocked" and "trusted zone" in res["message"] and "override_zone" in res["message"]
    assert stub.sent == []
    res2 = svc.run_command("unlock", {"override_zone": True}, surface="tool", approval=HUMAN)
    assert res2["status"] == "confirmed"
    assert any("NOT at a trusted location" in w for w in res2["policy"]["warnings"])
    assert stub.sent == [("unlock", {})]  # policy flags are never sent to the car


def test_custom_trusted_zone(stub_service, stub):
    svc = stub_service(armed=True, trusted_zones=["home", "work"])
    stub.values["location_zone"] = "work"
    assert svc.run_command("unlock", {}, surface="tool", approval=HUMAN)["status"] == "confirmed"


def test_unknown_motion_is_refused_by_default_and_allowed_only_by_explicit_opt_in(stub_service, stub):
    stub.values.pop("moving"), stub.values.pop("speed_mph"), stub.values.pop("shift_state")
    stub.values["plugged_in"] = False
    svc = stub_service(armed=True)                                   # default: fail closed
    res = svc.run_command("unlock", {}, surface="tool", approval=HUMAN)
    assert res["status"] == "blocked" and "cannot confirm the car is parked" in res["message"] and stub.sent == []
    svc2 = stub_service(armed=True, allow_unknown_motion=True)       # explicit opt-in
    res2 = svc2.run_command("unlock", {}, surface="tool", approval=HUMAN)
    assert res2["status"] == "confirmed" and any("cannot confirm" in w for w in res2["policy"]["warnings"])


def test_sleeping_car_not_woken_without_wake_ok(stub_service, stub):
    svc = stub_service(armed=True)
    stub.values["asleep"] = True
    res = svc.run_command("set_temp", {"temp_f": 70}, surface="tool")
    assert res["status"] == "blocked" and "asleep" in res["message"] and "wake_ok" in res["message"]
    assert svc.run_command("set_temp", {"temp_f": 70, "wake_ok": True}, surface="tool")["ok"] is True


def test_bounds_enforced_and_invalid_args(stub_service, stub):
    svc = stub_service(armed=True)
    for cmd, args in (("set_temp", {"temp_f": 99}), ("charge_limit", {"percent": 10}), ("charge_amps", {"amps": 500}),
                      ("seat_heat", {"seat": "driver", "level": 9}), ("set_temp", {})):
        res = svc.run_command(cmd, args, surface="tool")
        assert res["status"] == "invalid" and not res["ok"], (cmd, args)
    assert stub.sent == []
    assert svc.run_command("self_destruct", {}, surface="tool")["status"] == "invalid"


def test_charge_amps_capped_by_charger(stub_service, stub):
    svc = stub_service(armed=True)
    res = svc.run_command("charge_amps", {"amps": 40}, surface="tool")  # charger max is 32 A
    assert res["status"] == "blocked" and "charger" in res["message"]


def test_charge_start_requires_plug(stub_service, stub):
    svc = stub_service(armed=True)
    stub.values["plugged_in"] = False
    assert svc.run_command("charge_start", {}, surface="tool")["status"] == "blocked"


def test_unsupported_command_for_adapter(stub_service, stub):
    svc = stub_service(armed=True)
    stub.commands = frozenset({"lock"})
    res = svc.run_command("honk", {}, surface="tool", approval=HUMAN)
    assert res["status"] == "unsupported" and "cannot perform" in res["message"]


# -- rate limit / dedupe -----------------------------------------------------------------------------------
def test_duplicate_command_suppressed_then_allowed_after_window(stub_service, stub, clock):
    svc = stub_service(armed=True, dedupe_window_s=60)
    assert svc.run_command("set_temp", {"temp_f": 70}, surface="tool")["status"] == "confirmed"
    dup = svc.run_command("set_temp", {"temp_f": 70}, surface="tool")
    assert dup["status"] == "duplicate" and len(stub.sent) == 1
    assert svc.run_command("set_temp", {"temp_f": 71}, surface="tool")["status"] == "confirmed"
    clock.advance(61)
    assert svc.run_command("set_temp", {"temp_f": 70}, surface="tool")["status"] == "confirmed"


def test_rate_limit(stub_service, stub, clock):
    svc = stub_service(armed=True, rate_limit_commands=3, rate_limit_window_s=600, dedupe_window_s=0)
    for t in (65, 66, 67):
        assert svc.run_command("set_temp", {"temp_f": float(t)}, surface="tool")["status"] == "confirmed"
    res = svc.run_command("set_temp", {"temp_f": 75}, surface="tool")
    assert res["status"] == "rate_limited" and len(stub.sent) == 3
    clock.advance(601)
    assert svc.run_command("set_temp", {"temp_f": 75}, surface="tool")["status"] == "confirmed"


# -- read-back ------------------------------------------------------------------------------------------------
def test_readback_not_confirmed_is_reported_honestly(stub_service, stub, clock):
    svc = stub_service(armed=True, verify_timeout_s=6, verify_interval_s=2)
    stub.apply_commands = False
    stub.values["locked"] = False
    t0 = clock()
    res = svc.run_command("lock", {}, surface="tool")
    assert res["status"] == "sent_unconfirmed" and res["ok"] is True
    assert res["readback"]["status"] == "not_confirmed" and "NOT confirmed" in res["message"]
    assert 6 <= clock() - t0 <= 9  # polled until the timeout, no longer
    row = last_audit(svc)
    assert row["status"] == "sent_unconfirmed" and row["readback"] == "not_confirmed"


def test_readback_unverifiable_when_car_lacks_the_field(stub_service, stub):
    svc = stub_service(armed=True)
    stub.removed_fields = {"locked"}
    stub.values["locked"] = False
    res = svc.run_command("lock", {}, surface="tool")
    assert res["status"] == "sent_unconfirmed" and res["readback"]["status"] == "unverifiable"
    assert "can't confirm" in res["message"]


def test_honk_is_unverifiable_but_sent(stub_service):
    res = stub_service(armed=True).run_command("honk", {}, surface="tool", approval=HUMAN)
    assert res["status"] == "sent_unconfirmed" and res["readback"]["status"] == "unverifiable"


def test_adapter_failure_is_audited_not_raised(stub_service, stub):
    svc = stub_service(armed=True)
    stub.fail_with = "HA said no"
    res = svc.run_command("set_temp", {"temp_f": 70}, surface="tool")
    assert res["status"] == "failed" and "HA said no" in res["message"]
    assert last_audit(svc)["status"] == "failed"


def test_unreadable_car_blocks(stub_service, stub):
    svc = stub_service(armed=True)

    def boom(now):
        from pit_crew.adapters.base import AdapterUnavailable
        raise AdapterUnavailable("cannot reach Home Assistant")

    stub.fetch_state = boom
    res = svc.run_command("lock", {}, surface="tool")
    assert res["status"] == "blocked" and "cannot read the car" in res["message"]


# -- audit & privacy ----------------------------------------------------------------------------------------------
def test_audit_rows_complete_and_ordered(stub_service):
    svc = stub_service(armed=True)
    svc.run_command("set_temp", {"temp_f": 70}, surface="desktop")
    svc.run_command("unlock", {}, surface="tool")  # blocked
    rows = svc.audit_log(5)
    assert [r["command"] for r in rows] == ["unlock", "set_temp", "arm"]  # arming itself is audited
    assert {"ts", "surface", "command", "args", "risk", "mode", "policy", "approval", "status", "readback", "when"} <= set(rows[0])
    assert rows[1]["surface"] == "desktop" and rows[0]["risk"] == "security"


def test_no_coordinates_or_secrets_in_audit(stub_service, stub):
    svc = stub_service(armed=True)
    svc.run_command("set_temp", {"temp_f": 70}, surface="tool", reason="user at 12.345678, 23.456789 token eyJabcdefghijk.abcdefghijk.abcdefghijk")
    blob = json.dumps(svc.audit_log(5))
    assert "12.345678" not in blob and "eyJabcdefghijk" not in blob


def test_run_command_never_raises(stub_service, stub, monkeypatch):
    svc = stub_service(armed=True)
    monkeypatch.setattr(type(svc.engine), "execute", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
    res = svc.run_command("lock", {}, surface="tool")
    assert res["ok"] is False and "Internal error" in res["message"]


def test_policy_decisions_never_use_a_cached_state(stub_service, stub):
    svc = stub_service(armed=True)
    svc.get_state()                       # prime the cache (the fake clock does not advance)
    stub.values.update(moving=True, speed_mph=60.0)
    res = svc.run_command("unlock", {}, surface="tool", approval=HUMAN)
    assert res["status"] == "blocked" and "moving" in res["message"] and stub.sent == []
