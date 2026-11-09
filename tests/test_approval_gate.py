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


def test_hook_requests_real_approval_with_one_time_rule_key(gate_env):
    svc, stub, gate = gate_env
    d1 = gate.pre_tool_call(tool_name="car_command", args={"command": "unlock", "reason": "user asked"}, task_id="t1")
    d2 = gate.pre_tool_call(tool_name="car_command", args={"command": "unlock"}, task_id="t1")
    assert d1["action"] == "approve" and d1["rule_key"].startswith("pit-crew:unlock:")
    assert d1["rule_key"] != d2["rule_key"]  # "always allow" can never become a standing permission
    assert "UNLOCK" in d1["message"] and "user asked" in d1["message"] and "Approve only if you asked" in d1["message"]
    assert "latitude" not in d1["message"].lower()


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


def test_strict_mode_requires_positive_confirmation_even_if_prompt_was_skipped(gate_env):
    """Hermes YOLO / approvals-off: the gate auto-approves, no post_approval_response ever fires."""
    svc, stub, gate = gate_env
    gate.pre_tool_call(tool_name="car_command", args={"command": "unlock"})  # ticket issued, never confirmed
    res = call({"command": "unlock"})
    assert res["status"] == "blocked" and res["approval"] == "missing" and stub.sent == []


def test_hermes_gate_mode_trusts_hermes_when_configured(stub_service, stub):
    svc = stub_service(armed=True, approval_confirmation="hermes_gate")
    gate = ApprovalGate(lambda: svc)
    gate.pre_tool_call(tool_name="car_command", args={"command": "unlock"})
    assert call({"command": "unlock"})["status"] == "confirmed"
    # but a hook that never ran (no ticket at all) still fails closed
    stub.values["locked"] = True
    assert call({"command": "unlock"})["status"] == "blocked"


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


def test_foreign_pattern_keys_are_ignored(gate_env):
    svc, stub, gate = gate_env
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
