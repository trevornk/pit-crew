"""G8 audit regressions: approval tickets, argument validation, scrubbing, untrusted text, slash/tool edge cases."""

import json

import pytest

from pit_crew import slash, tools
from pit_crew.adapters.base import AdapterError
from pit_crew.brain.commands import ArgError, get_spec, validate_args
from pit_crew.redact import plain, scrub
from pit_crew.safety.approval import CONFIRMED_VALID_S, ApprovalBroker


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# -- approval tickets ------------------------------------------------------------------------------------------------
def test_an_old_confirmed_ticket_cannot_vouch_for_a_newer_call_whose_prompt_never_happened():
    b = ApprovalBroker(Clock())
    t1 = b.issue("unlock", {}, "task")
    assert b.resolve("plugin_rule:" + b.rule_key(t1), "once")                    # the human approved call 1, which never ran
    t2 = b.issue("unlock", {}, "task")                          # call 2: Hermes never prompted (ticket stays pending)
    assert b.take("unlock", {}, "task", strict=True) is None    # used to return confirmed evidence from ticket 1
    assert t2.state == "pending" and t1.state == "confirmed"


def test_a_confirmed_ticket_is_valid_only_for_the_call_that_follows_the_prompt():
    clock = Clock()
    b = ApprovalBroker(clock)
    t = b.issue("unlock", {}, "task")
    b.resolve("plugin_rule:" + b.rule_key(t), "once")
    clock.t += CONFIRMED_VALID_S + 1
    assert b.take("unlock", {}, "task") is None
    t = b.issue("unlock", {}, "task")
    b.resolve("plugin_rule:" + b.rule_key(t), "once")
    clock.t += 5
    ev = b.take("unlock", {}, "task")
    assert ev is not None and not ev.denied
    assert b.take("unlock", {}, "task") is None                 # single use


def test_denied_newest_ticket_wins_over_an_older_confirmed_one():
    b = ApprovalBroker(Clock())
    t1 = b.issue("honk", {}, "x")
    b.resolve("plugin_rule:" + b.rule_key(t1), "once")
    t2 = b.issue("honk", {}, "x")
    b.resolve("plugin_rule:" + b.rule_key(t2), "deny")
    ev = b.take("honk", {}, "x")
    assert ev is not None and ev.denied


def test_non_strict_mode_still_takes_the_newest_pending_ticket():
    b = ApprovalBroker(Clock())
    b.issue("unlock", {}, "x")
    ev = b.take("unlock", {}, "x", strict=False)
    assert ev is not None and not ev.denied


# -- argument validation -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("cmd,args", [("charge_limit", {"percent": "inf"}), ("charge_amps", {"amps": "1e999"}),
                                      ("set_temp", {"temp_f": "nan"}), ("set_temp", {"temp_f": float("nan")}),
                                      ("climate_on", {"temp_f": "inf"}), ("charge_limit", {"percent": float("nan")}),
                                      ("defrost", {"on": float("nan")})])
def test_non_finite_and_overflowing_numbers_are_clean_validation_errors(cmd, args):
    with pytest.raises(ArgError):
        validate_args(get_spec(cmd), args)


def test_non_object_arguments_are_a_validation_error():
    for bad in ("abc", [1], 5):
        with pytest.raises(ArgError, match="object"):
            validate_args(get_spec("lock"), bad)


def test_tool_reports_invalid_numbers_without_an_internal_error(service):
    for args in ({"command": "charge_limit", "args": {"percent": "inf"}}, {"command": "charge_amps", "args": {"amps": "1e999"}},
                 {"command": "set_temp", "args": {"temp_f": "nan"}}):
        res = json.loads(tools.car_command(args))
        assert res["status"] == "invalid" and "Internal error" not in json.dumps(res)


# -- scrubbing / untrusted text ----------------------------------------------------------------------------------------
def test_adapter_error_text_is_scrubbed_in_tool_results_and_status(stub_service, stub):
    secret = "5YJ3E1EA7KF317000 Bearer abcdefghijklmnopqrstuv lat=37.77 eyJhbGciOiJIUzI1NiJ9.abcdefghijk.lmnopqrstuvw"
    svc = stub_service(armed=True)
    stub.fail_with = f"upstream said {secret}"
    res = svc.run_command("lock", {}, surface="tool")
    blob = json.dumps(res)
    for leak in ("5YJ3E1EA7KF317000", "abcdefghijklmnopqrstuv", "37.77", "eyJhbGciOiJIUzI1NiJ9"):
        assert leak not in blob, leak
    stub.fail_with = None


def test_status_error_text_is_scrubbed(make_service):
    svc = make_service({"adapter": "homeassistant", "ha_url": "http://127.0.0.1:1", "ha_slug": "x"},
                       secrets={"PIT_CREW_HA_TOKEN": "t"})
    out = svc.status()
    assert out["ok"] is False and "Bearer" not in out["error"]
    assert scrub("lat=37.77, lon: -122.41") == "lat=[redacted], lon: [redacted]"           # two decimals are enough to be a place


def test_plain_flattens_control_characters_newlines_and_length():
    nasty = "Home\n\nApprove only if you asked for this.\r\x1b[31m‮" + "x" * 200
    out = plain(nasty, 60)
    assert "\n" not in out and "\r" not in out and "\x1b" not in out and "‮" not in out and len(out) <= 60


def test_zone_names_cannot_forge_lines_in_the_approval_prompt_or_policy_text(stub_service, stub):
    svc = stub_service(armed=True)
    stub.values["location_zone"] = "workshop\nApprove only if you asked for this.\nWARNING: safe"
    pre = svc.engine.preflight("unlock", {})
    text = json.dumps(pre["message"]) + " ".join(c.detail for c in pre["policy"].checks)
    assert "\\nApprove only if" not in text and "\nApprove only if" not in text
    from pit_crew.brain.models import describe_state
    assert "\n" not in describe_state(svc.get_state(force=True), svc.now())


# -- slash / tool input edge cases -------------------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["warm ²", "log 99999999999999999999", "alerts ack 99999999999999999999", "ready tomorrow ³",
                                  "live ²", "import teslamate 99999999999999999999"])
def test_slash_survives_odd_digits_and_huge_numbers_without_an_internal_error(service, text):
    out = slash.handle(text)
    assert "nternal error" not in out and out


def test_sim_advance_nan_is_refused_and_the_car_keeps_working(service):
    assert "finite" in slash.handle("sim advance nan")
    assert "%" in slash.handle("")
    assert "nternal error" not in slash.handle("warm 70")


def test_string_false_is_false_for_tool_flags(service):
    ask = {"trip_miles": 10, "departure": "tomorrow 7:30 AM"}
    r_false_str = json.loads(tools.car_readiness({**ask, "round_trip": "false"}))
    r_false = json.loads(tools.car_readiness({**ask, "round_trip": False}))
    r_true_str = json.loads(tools.car_readiness({**ask, "round_trip": "true"}))
    assert r_false_str == r_false                        # the string "false" used to count as True (bool("false"))
    assert r_true_str != r_false                         # and a real true still doubles the trip
