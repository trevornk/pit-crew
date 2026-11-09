"""G8 audit regressions: the page backend (dashboard/plugin_api.py) and both pages (jsdom)."""

import importlib.util
import json
from pathlib import Path

import pytest

from pagerender import (ROOT, backend_summary, interact, needs_desktop_render, needs_web_render, render_web)

pytest.importorskip("fastapi")


@pytest.fixture
def api():
    spec = importlib.util.spec_from_file_location("car_api_audit", ROOT / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# -- backend ------------------------------------------------------------------------------------------------------------
def test_pages_cannot_set_the_agents_wake_and_zone_override_flags(stub_service, stub, api):
    svc = stub_service(armed=True)
    stub.values["asleep"] = True
    res = api.command({"command": "climate_on", "args": {"temp_f": 70, "wake_ok": True, "override_zone": True}, "confirmed": True})
    assert res["status"] != "confirmed" and stub.sent == []            # the asleep refusal stands: the flag was dropped
    pf = api.preflight({"command": "climate_on", "args": {"wake_ok": True}})
    assert pf["ok"] is False and "asleep" in json.dumps(pf).lower()


@pytest.mark.parametrize("bad", ["abc", [1, 2], 5, True])
def test_non_object_args_are_a_clean_error_not_a_500(service, api, bad):
    assert api.preflight({"command": "lock", "args": bad})["ok"] is False
    res = api.command({"command": "lock", "args": bad, "confirmed": True})
    assert res["ok"] is False and "Internal error" not in json.dumps(res)


@pytest.mark.parametrize("confirmed", [False, 0, None, "false", "0", "no", 1, "true", "yes", []])
def test_only_a_real_true_confirms(service, api, confirmed):
    res = api.command({"command": "lock", "confirmed": confirmed})
    assert res.get("status") == "needs_confirmation"
    assert api.arm({"confirmed": confirmed})["status"] == "needs_confirmation"
    assert service.armed_info()["armed"] is False


def test_arm_rejects_nan_and_junk_hours_instead_of_arming_for_the_default(service, api):
    for bad in ("nan", "inf", "soon", [1]):
        assert api.arm({"confirmed": True, "hours": bad})["ok"] is False
    assert service.armed_info()["armed"] is False
    assert api.arm({"confirmed": True, "hours": 2})["ok"] is True


def test_rule_and_sim_bodies_are_validated(service, api):
    assert api.set_rule("left_unlocked_away", {"settings": "x"})["ok"] is False
    assert api.set_rule("left_unlocked_away", {"settings": [1]})["ok"] is False
    assert api.set_rule("left_unlocked_away", {"enabled": "false"})["now"]["enabled"] is False     # "false" is not True
    assert api.set_rule("left_unlocked_away", {"enabled": True})["now"]["enabled"] is True
    assert api.sim({"action": "drive", "params": "x"})["ok"] is False
    assert api.sim({"action": "drive", "params": {"miles": "nan"}})["ok"] is False
    assert api.sim({"action": "drive", "params": {"miles": 5}})["ok"] is True


# -- web page (jsdom) ------------------------------------------------------------------------------------------------------
def _summary(service):
    mod, summary = backend_summary(service)
    return mod, summary


@needs_web_render
def test_web_page_says_when_it_can_no_longer_reach_the_backend(service, tmp_path):
    mod, summary = _summary(service)
    out = render_web(tmp_path, {"/summary": summary, "/log": {"ok": True, "rows": []}, "__fail_summary_after": 1}, clicks=["Refresh"])
    assert "Can’t reach Pit Crew right now" in out["html"] and "may be out of date" in out["html"]
    ok = render_web(tmp_path, {"/summary": summary, "/log": {"ok": True, "rows": []}})
    assert "Can’t reach Pit Crew" not in ok["html"]


@needs_web_render
def test_web_acknowledging_an_alert_does_not_force_a_read_of_the_car(service, tmp_path):
    mod, summary = _summary(service)
    summary["alerts"] = [{"id": 7, "rule": "software_update", "severity": "info", "state": "new", "title": "Update", "body": "b",
                          "suggestion": "s", "when": "2026-11-08T22:06:00-06:00"}]
    out = render_web(tmp_path, {"/summary": summary, "/log": {"ok": True, "rows": []}, "/alerts/ack": {"ok": True}}, clicks=["Got it"])
    urls = [c[0] for c in out["calls"]]
    assert any(u.endswith("/alerts/ack") for u in urls)
    assert not any("refresh=true" in u for u in urls)             # a vendor read per click used to happen here


@needs_web_render
def test_web_double_click_sends_the_command_once(service, tmp_path):
    mod, summary = _summary(service)
    pf = mod.preflight({"command": "sentry_on"})                      # the simulated car is locked already; Sentry is off
    out = render_web(tmp_path, {"/summary": summary, "/log": {"ok": True, "rows": []}, "/preflight": pf,
                                "/command": {"ok": True, "message": "PRACTICE RUN done"}}, clicks=["Sentry on", "Practice run×2"])
    sends = [c for c in out["calls"] if c[0].endswith("/command")]
    assert len(sends) == 1


@needs_web_render
def test_web_go_live_keeps_the_prompt_open_when_the_backend_refuses(service, tmp_path):
    mod, summary = _summary(service)
    out = render_web(tmp_path, {"/summary": summary, "/log": {"ok": True, "rows": []},
                                "/arm": {"ok": False, "error": "hours must be a number"}}, clicks=["Go live…", "Go live"])
    assert "hours must be a number" in out["html"] and ">Cancel<" in out["html"]


# -- Desktop page (jsdom, stateful SDK stub) -------------------------------------------------------------------------------
@needs_desktop_render
def test_desktop_dialog_does_not_keep_the_previous_commands_result(service, tmp_path):
    mod, summary = _summary(service)
    data = {"/summary": summary, "/rules": mod.rules(), "/log": {"ok": True, "rows": []},
            "/preflight": mod.preflight({"command": "sentry_on"}),
            "/command": {"ok": True, "status": "dry_run", "message": "PRACTICE RUN done"}}
    out = interact(tmp_path, data, clicks=["Sentry on", "Practice run", "Close", "Sentry on"])
    first, ran, closed, second = out["shots"]
    assert "Practice run" in first["buttons"] and "Result" not in first["dialog"]
    assert "PRACTICE RUN done" in ran["dialog"]
    assert closed["dialog"] == ""
    # the same command again starts fresh: the safety-check view with its own buttons, not the earlier 'Result' screen
    assert "PRACTICE RUN done" not in second["dialog"] and "Result" not in second["dialog"]
    assert "Practice run" in second["buttons"]
    assert [c for c in out["calls"] if c[0] == "/command"].__len__() == 1
