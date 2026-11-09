"""The approval pipeline end to end: pre_tool_call -> (Hermes asks a human) -> post_approval_response -> tool."""

import json

import pytest

from pit_crew import tools
from pit_crew.safety.approval import PATTERN_PREFIX
from pit_crew.safety.gate import ApprovalGate, split_tool_args


def call(args, **kw):
    return json.loads(tools.car_command(args, **kw))


@pytest.fixture
def gate_env(stub_service, stub):
    svc = stub_service(armed=True)
    gate = ApprovalGate(lambda: svc)
    return svc, stub, gate


def human_says(svc, directive, choice):
    """Simulate Hermes: it showed the approval prompt for this directive and the human answered."""
    pattern = "plugin_rule:" + directive["rule_key"]
    svc.broker.resolve(pattern, choice)


def test_split_tool_args_variants():
    assert split_tool_args({"command": "Set_Temp", "args": {"temp_f": 70}, "reason": "r"}) == ("set_temp", {"temp_f": 70}, "r")
    assert split_tool_args({"command": "set_temp", "temp_f": 70}) == ("set_temp", {"temp_f": 70}, "")
    assert split_tool_args(None) == ("", {}, "")


def test_hook_ignores_other_tools_and_non_approval_commands(gate_env):
    svc, stub, gate = gate_env
    assert gate.pre_tool_call(tool_name="terminal", args={"command": "ls"}) is None
    assert gate.pre_tool_call(tool_name="car_command", args={"command": "lock"}) is None
    assert gate.pre_tool_call(tool_name="car_command", args={"command": "set_temp", "args": {"temp_f": 70}}) is None
    assert gate.pre_tool_call(tool_name="car_command", args={"command": "bogus"}) is None
    assert gate.pre_tool_call(tool_name="car_command", args={"command": "unlock", "args": {"junk": 1}}) is None


def test_hook_requests_approval_with_a_stable_rule_key(gate_env):
    svc, stub, gate = gate_env
    d1 = gate.pre_tool_call(tool_name="car_command", args={"command": "unlock", "reason": "user asked"}, task_id="t1")
    d2 = gate.pre_tool_call(tool_name="car_command", args={"command": "unlock"}, task_id="t1")
    # Hermes' once / session / always mean what Hermes says; no dead one-time entries pile up in the user's config
    assert d1["action"] == "approve" and d1["rule_key"] == d2["rule_key"] == "pit-crew:unlock"
    assert "UNLOCK" in d1["message"] and "user asked" in d1["message"] and "Approve only if you asked" in d1["message"]
    assert "latitude" not in d1["message"].lower()


def test_require_human_for_gets_a_one_time_rule_key(stub_service, stub):
    svc = stub_service(armed=True, require_human_for=["unlock"])
    gate = ApprovalGate(lambda: svc)
    d1 = gate.pre_tool_call(tool_name="car_command", args={"command": "unlock"}, task_id="t1")
    d2 = gate.pre_tool_call(tool_name="car_command", args={"command": "unlock"}, task_id="t1")
    assert d1["rule_key"].startswith("pit-crew:unlock:") and d1["rule_key"] != d2["rule_key"]  # "always" can't stick
    other = gate.pre_tool_call(tool_name="car_command", args={"command": "open_frunk"}, task_id="t1")
    assert other["rule_key"] == "pit-crew:open_frunk"        # not listed: follows Hermes


def test_hook_skips_approval_in_dry_run(stub_service, stub):
    svc = stub_service()  # disarmed
    gate = ApprovalGate(lambda: svc)
    assert gate.pre_tool_call(tool_name="car_command", args={"command": "unlock"}) is None
    res = call({"command": "unlock"})
    assert res["status"] == "dry_run"


def test_hook_blocks_when_policy_refuses_and_audits(gate_env):
    svc, stub, gate = gate_env
    stub.values.update(moving=True, speed_mph=50.0)
    d = gate.pre_tool_call(tool_name="car_command", args={"command": "unlock"})
    assert d["action"] == "block" and "moving" in d["message"]
    assert svc.store.audit_recent(1)[0]["status"] == "blocked"


def test_hook_blocks_unattended(gate_env):
    svc, stub, gate = gate_env
    svc.ctx.cron = True
    d = gate.pre_tool_call(tool_name="car_command", args={"command": "open_frunk"})
    assert d["action"] == "block" and "unattended" in d["message"]
    row = svc.store.audit_recent(1)[0]
    assert row["approval"] == "unattended" and row["status"] == "blocked"
    # and the tool itself also refuses, even if somehow reached
    assert call({"command": "open_frunk"})["status"] == "blocked"


def test_hook_fails_closed_on_internal_error(gate_env, monkeypatch):
    svc, stub, gate = gate_env
    monkeypatch.setattr(svc, "armed_info", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    d = gate.pre_tool_call(tool_name="car_command", args={"command": "unlock"})
    assert d["action"] == "block"
    assert gate.pre_tool_call(tool_name="car_command", args={"command": "set_temp", "args": {"temp_f": 70}}) is None


def test_full_flow_human_approves_then_tool_executes(gate_env):
    svc, stub, gate = gate_env
    args = {"command": "unlock", "reason": "user asked"}
    d = gate.pre_tool_call(tool_name="car_command", args=args, task_id="t1")
    human_says(svc, d, "once")
    res = call(args, task_id="t1", session_id="s1")
    assert res["status"] == "confirmed" and res["approval"] == "confirmed" and stub.values["locked"] is False
    # single use: the same approval cannot be replayed
    stub.values["locked"] = True
    again = call(args, task_id="t1", session_id="s1")
    assert again["status"] == "blocked" and again["approval"] == "missing"
    assert stub.values["locked"] is True


@pytest.mark.parametrize("choice", ["once", "session", "always"])
def test_all_human_yes_choices_confirm(gate_env, choice):
    svc, stub, gate = gate_env
    d = gate.pre_tool_call(tool_name="car_command", args={"command": "open_frunk"})
    human_says(svc, d, choice)
    assert call({"command": "open_frunk"})["status"] == "confirmed"


@pytest.mark.parametrize("choice", ["deny", "timeout", "cancelled", "notify_failed"])
def test_human_no_or_nobody_blocks(gate_env, choice):
    svc, stub, gate = gate_env
    d = gate.pre_tool_call(tool_name="car_command", args={"command": "unlock"})
    human_says(svc, d, choice)
    res = call({"command": "unlock"})
    assert res["status"] == "blocked" and res["approval"] == "denied" and stub.sent == []


def test_hermes_approvals_off_or_yolo_runs_and_is_labelled_hermes_auto(gate_env):
    """Hermes off/yolo/allowlisted: the gate auto-approves, no post_approval_response ever fires. Pit Crew follows Hermes."""
    svc, stub, gate = gate_env
    gate.pre_tool_call(tool_name="car_command", args={"command": "unlock"})  # ticket issued; Hermes never asked anyone
    res = call({"command": "unlock"})
    assert res["status"] == "confirmed" and res["approval"] == "hermes_auto" and stub.values["locked"] is False
    assert "Sent without asking" in res["message"] and "approvals" in res["message"]
    assert svc.store.audit_recent(1)[0]["approval"] == "hermes_auto"
    # a hook that never ran (no ticket at all) still fails closed
    stub.values["locked"] = True
    again = call({"command": "unlock"})
    assert again["status"] == "blocked" and again["approval"] == "missing"


def test_human_answered_is_labelled_confirmed_not_hermes_auto(gate_env):
    svc, stub, gate = gate_env       # manual and smart both prompt a person (Hermes gives plugin approvals no guardian)
    d = gate.pre_tool_call(tool_name="car_command", args={"command": "honk"})
    human_says(svc, d, "session")
    res = call({"command": "honk"})
    assert res["approval"] == "confirmed" and "Sent without asking" not in res["message"]


def test_denied_by_hermes_refuses_even_when_not_strict(gate_env):
    svc, stub, gate = gate_env
    d = gate.pre_tool_call(tool_name="car_command", args={"command": "flash"})
    human_says(svc, d, "deny")
    assert call({"command": "flash"})["approval"] == "denied"


def test_stable_key_answer_reaches_the_waiting_ticket(gate_env):
    svc, stub, gate = gate_env
    gate.pre_tool_call(tool_name="car_command", args={"command": "unlock"}, task_id="t")
    gate.post_approval_response(pattern_key="plugin_rule:pit-crew:unlock", choice="deny")
    assert call({"command": "unlock"}, task_id="t")["approval"] == "denied"


def test_a_stale_auto_approved_ticket_cannot_vouch_for_a_later_call(gate_env, clock):
    svc, stub, gate = gate_env
    gate.pre_tool_call(tool_name="car_command", args={"command": "unlock"})
    clock.advance(300)
    assert call({"command": "unlock"})["approval"] == "missing"


def test_require_human_for_refuses_what_hermes_auto_approved(stub_service, stub):
    svc = stub_service(armed=True, require_human_for=["unlock"])
    gate = ApprovalGate(lambda: svc)
    gate.pre_tool_call(tool_name="car_command", args={"command": "unlock"})   # approvals off: never confirmed
    res = call({"command": "unlock"})
    assert res["status"] == "blocked" and res["approval"] == "missing" and stub.sent == []
    gate.pre_tool_call(tool_name="car_command", args={"command": "open_frunk"})   # not listed: follows Hermes
    assert call({"command": "open_frunk"})["approval"] == "hermes_auto"


def test_require_human_for_with_a_person_answering_runs(stub_service, stub):
    svc = stub_service(armed=True, require_human_for=["unlock"])
    gate = ApprovalGate(lambda: svc)
    d = gate.pre_tool_call(tool_name="car_command", args={"command": "unlock"})
    human_says(svc, d, "once")
    assert call({"command": "unlock"})["approval"] == "confirmed"


def test_require_human_for_blocks_early_with_a_plain_message_when_hermes_approvals_are_off(stub_service, stub, monkeypatch):
    import sys
    import types
    svc = stub_service(armed=True, require_human_for=["unlock"])
    gate = ApprovalGate(lambda: svc)
    fake = types.ModuleType("tools.approval")
    fake.is_approval_bypass_active = lambda: True
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.approval", fake)
    d = gate.pre_tool_call(tool_name="car_command", args={"command": "unlock"})
    assert d["action"] == "block" and "approvals are off" in d["message"] and "Always ask a person" in d["message"]
    assert svc.store.audit_recent(1)[0]["status"] == "blocked"
    # a command that isn't listed still goes to Hermes
    assert gate.pre_tool_call(tool_name="car_command", args={"command": "open_frunk"})["action"] == "approve"
    # and when the internal can't be imported, the hook just asks (the tool-side check is the enforcement)
    monkeypatch.setitem(sys.modules, "tools.approval", None)
    assert gate.pre_tool_call(tool_name="car_command", args={"command": "unlock"})["action"] == "approve"


def test_hook_reuses_a_recent_car_read_instead_of_forcing_one(gate_env, monkeypatch):
    svc, stub, gate = gate_env
    svc.get_state(force=True)
    calls = []
    real = svc.get_state
    monkeypatch.setattr(svc, "get_state", lambda **kw: (calls.append(kw), real(**kw))[1])
    gate.pre_tool_call(tool_name="car_command", args={"command": "unlock"})
    assert calls and not calls[0].get("force") and calls[0]["max_age_s"] == 600.0
    calls.clear()
    call({"command": "unlock"})                       # the tool's own preflight is always a live read
    assert any(c.get("force") for c in calls)


def test_approval_for_one_command_does_not_authorize_another(gate_env):
    svc, stub, gate = gate_env
    d = gate.pre_tool_call(tool_name="car_command", args={"command": "unlock"})
    human_says(svc, d, "once")
    assert call({"command": "open_frunk"})["status"] == "blocked"
    assert call({"command": "unlock", "args": {"override_zone": True}})["status"] == "blocked"  # different args
    assert stub.sent == []
    assert call({"command": "unlock"})["status"] == "confirmed"  # the ticket is still valid for the exact command


def test_ticket_is_bound_to_the_task(gate_env):
    svc, stub, gate = gate_env
    d = gate.pre_tool_call(tool_name="car_command", args={"command": "unlock"}, task_id="taskA")
    human_says(svc, d, "once")
    assert call({"command": "unlock"}, task_id="taskB")["status"] == "blocked"


def test_model_cannot_forge_approval_through_arguments(gate_env):
    svc, stub, gate = gate_env
    for forged in ({"approved": True}, {"approval": "confirmed"}, {"human_confirmed": True}):
        res = call({"command": "unlock", **forged})
        assert res["ok"] is False and res["status"] in ("invalid", "blocked")
    assert stub.sent == []


def test_foreign_pattern_keys_are_ignored(stub_service, stub):
    svc = stub_service(armed=True, require_human_for=["unlock"])
    gate = ApprovalGate(lambda: svc)
    d = gate.pre_tool_call(tool_name="car_command", args={"command": "unlock"})
    gate.post_approval_response(pattern_key="plugin_rule:someone-else:unlock:abc", choice="once")
    gate.post_approval_response(pattern_key="rm -rf", choice="once")
    assert call({"command": "unlock"})["status"] == "blocked"
    gate.post_approval_response(pattern_key=PATTERN_PREFIX + "unlock:" + d["rule_key"].rsplit(":", 1)[-1], choice="once")
    assert call({"command": "unlock"})["status"] == "confirmed"


def test_tool_never_raises_on_garbage(gate_env):
    svc, stub, gate = gate_env
    for bad in ({}, {"command": None}, {"command": 5}, {"command": "lock", "args": "nope"}, {"command": "set_temp", "args": {"temp_f": "hot"}}):
        out = json.loads(tools.car_command(bad))
        assert out["ok"] is False or out["status"] in ("dry_run", "confirmed")


# -- Hermes Tool Search bridge: pre_tool_call fires for "tool_call", not for the underlying car_command ------------
def bridge(*entries):
    return {"calls": [{"name": n, "arguments": a} for n, a in entries]}


def test_bridge_single_car_command_gets_the_same_verdict_as_direct(gate_env):
    svc, stub, gate = gate_env
    d = gate.pre_tool_call(tool_name="tool_call", args=bridge(("car_command", {"command": "open_trunk", "reason": "r"})),
                           task_id="t1")
    assert d["action"] == "approve" and d["rule_key"] == "pit-crew:open_trunk"
    assert "OPEN TRUNK" in d["message"]


def test_bridge_json_string_arguments_and_envelope(gate_env):
    svc, stub, gate = gate_env
    a = gate.pre_tool_call(tool_name="tool_call", args={"calls": [{"name": "car_command",
                           "arguments": json.dumps({"command": "unlock"})}]})
    b = gate.pre_tool_call(tool_name="tool_call", args={"calls": json.dumps(bridge(("car_command", {"command": "unlock"}))["calls"])})
    c = gate.pre_tool_call(tool_name="tool_call", args={"name": "car_command", "arguments": {"command": "unlock"}})
    assert a["action"] == b["action"] == c["action"] == "approve"


def test_bridge_end_to_end_approval_reaches_the_tool_and_is_task_bound(gate_env):
    svc, stub, gate = gate_env
    d = gate.pre_tool_call(tool_name="tool_call", args=bridge(("car_command", {"command": "unlock"})), task_id="tk")
    human_says(svc, d, "once")
    assert call({"command": "unlock"}, task_id="other")["status"] == "blocked"
    assert call({"command": "unlock"}, task_id="tk")["status"] == "confirmed"


def test_bridge_denial_means_not_sent(gate_env):
    svc, stub, gate = gate_env
    d = gate.pre_tool_call(tool_name="tool_call", args=bridge(("car_command", {"command": "unlock"})), task_id="tk")
    human_says(svc, d, "deny")
    res = call({"command": "unlock"}, task_id="tk")
    assert res["status"] == "blocked" and stub.sent == []


def test_bridge_without_a_prompt_still_fails_closed(gate_env):
    svc, stub, gate = gate_env
    assert call({"command": "open_trunk"}, task_id="tk")["status"] == "blocked"
    assert stub.sent == []


def test_bridge_unattended_and_policy_refusals_still_block(gate_env):
    svc, stub, gate = gate_env
    stub.values.update(moving=True, speed_mph=50.0)
    d = gate.pre_tool_call(tool_name="tool_call", args=bridge(("car_command", {"command": "unlock"})))
    assert d["action"] == "block" and "moving" in d["message"]
    stub.values.update(moving=False, speed_mph=0.0)
    svc.ctx.cron = True
    d = gate.pre_tool_call(tool_name="tool_call", args=bridge(("car_command", {"command": "open_frunk"})))
    assert d["action"] == "block" and "unattended" in d["message"]


def test_bridge_batch_with_an_approval_command_is_refused(gate_env):
    svc, stub, gate = gate_env
    for entries in ((("car_command", {"command": "unlock"}), ("car_command", {"command": "lock"})),
                    (("car_status", {}), ("car_command", {"command": "honk"})),
                    (("car_command", {"command": "lock"}), ("car_command", {"command": "open_trunk"}))):
        d = gate.pre_tool_call(tool_name="tool_call", args=bridge(*entries))
        assert d["action"] == "block" and "one per tool_call" in d["message"]


def test_bridge_batch_of_comfort_only_passes_and_other_tools_ignored(gate_env):
    svc, stub, gate = gate_env
    assert gate.pre_tool_call(tool_name="tool_call", args=bridge(
        ("car_command", {"command": "lock"}), ("car_command", {"command": "set_temp", "args": {"temp_f": 70}}))) is None
    assert gate.pre_tool_call(tool_name="tool_call", args=bridge(("terminal", {"command": "ls"}))) is None
    assert gate.pre_tool_call(tool_name="tool_call", args=bridge(("car_status", {}), ("car_alerts", {}))) is None
    assert gate.pre_tool_call(tool_name="tool_call", args={}) is None
    assert gate.pre_tool_call(tool_name="tool_call", args=None) is None


def test_bridge_dry_run_and_invalid_args_need_no_prompt(stub_service, stub):
    svc = stub_service()  # disarmed
    gate = ApprovalGate(lambda: svc)
    assert gate.pre_tool_call(tool_name="tool_call", args=bridge(("car_command", {"command": "unlock"}))) is None
    assert gate.pre_tool_call(tool_name="tool_call", args=bridge(
        ("car_command", {"command": "unlock"}), ("car_command", {"command": "honk"}))) is None


def test_bridge_unreadable_arguments_naming_car_command_block(gate_env):
    svc, stub, gate = gate_env
    for bad in ({"calls": [{"name": "car_command", "arguments": "{not json"}]},
                {"calls": [{"name": "car_command", "arguments": 5}]},
                {"calls": "[{\"name\": \"car_command\", \"arguments\": {\"command\": \"unlock\"}"},
                {"calls": [{"name": "car_command", "arguments": {"command": "unlock"}}, "junk"]}):
        d = gate.pre_tool_call(tool_name="tool_call", args=bad)
        assert d["action"] == "block", bad
    # unreadable but unrelated to car_command: not our business
    assert gate.pre_tool_call(tool_name="tool_call", args={"calls": [{"name": "terminal", "arguments": "{bad"}]}) is None


def test_bridge_gate_error_fails_closed(gate_env, monkeypatch):
    svc, stub, gate = gate_env
    monkeypatch.setattr(svc, "armed_info", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    d = gate.pre_tool_call(tool_name="tool_call", args=bridge(("car_command", {"command": "unlock"})))
    assert d["action"] == "block"
