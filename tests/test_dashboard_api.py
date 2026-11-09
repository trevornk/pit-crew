"""The Desktop backend (dashboard/plugin_api.py) through FastAPI's TestClient."""

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(service):
    spec = importlib.util.spec_from_file_location("car_plugin_api_t", ROOT / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/pit-crew")
    c = TestClient(app)
    c.base = "/api/plugins/pit-crew"
    c.svc = service
    return c


def get(c, path, **kw):
    return c.get(c.base + path, **kw).json()


def post(c, path, body=None):
    return c.post(c.base + path, json=body or {}).json()


def test_summary_shape(client):
    s = get(client, "/summary")
    assert s["ok"] and s["simulated"] is True and s["armed"]["armed"] is False
    assert s["status"]["vehicle"]["simulated"] and "battery" in s["status"]
    assert s["settings"]["adapter"] == "simulator" and "notify" in s and isinstance(s["alerts"], list)
    assert "latitude" not in str(s).lower()


def test_preflight_shows_safety_decision(client):
    pf = post(client, "/preflight", {"command": "unlock"})
    assert pf["ok"] and pf["needs_human_approval"] and pf["risk"] == "security" and pf["mode"] == "dry_run"
    assert {c["check"] for c in pf["policy"]["checks"]} >= {"state_fresh", "parked", "trusted_zone"}
    bad = post(client, "/preflight", {"command": "set_temp", "args": {"temp_f": 120}})
    assert bad["ok"] is False and bad["status"] == "invalid"
    client.svc.adapter.control("drive", miles=5, to="away")
    away = post(client, "/preflight", {"command": "unlock"})
    assert away["ok"] is False and "trusted zone" in away["message"]


def test_command_requires_confirmation_flag(client):
    r = post(client, "/command", {"command": "lock"})
    assert r["ok"] is False and r["status"] == "needs_confirmation"
    assert client.svc.store.audit_recent(1) == []


def test_dry_run_then_armed_flow_hands_approval_class_to_hermes(client):
    dry = post(client, "/command", {"command": "unlock", "confirmed": True})
    assert dry["status"] == "dry_run" and dry["dry_run"]
    assert post(client, "/arm", {"hours": 1})["ok"] is False                 # arming needs the dialog confirmation
    assert post(client, "/arm", {"hours": 1, "confirmed": True})["armed"] is True
    live = post(client, "/command", {"command": "unlock", "confirmed": True})
    assert live["status"] == "ask_hermes"                                    # the page cannot approve it
    ok = post(client, "/command", {"command": "close_windows", "confirmed": True})
    assert ok["status"] in ("confirmed", "duplicate") and ok["ok"]
    rows = get(client, "/log")["rows"]
    assert rows[0]["command"] == "close_windows" and rows[0]["surface"] == "desktop"
    assert post(client, "/disarm")["armed"] is False
    assert get(client, "/summary")["armed"]["armed"] is False


def test_readiness_and_history_endpoints(client):
    r = get(client, "/readiness", params={"departure": "tomorrow 7:30 AM", "trip_miles": 25})
    assert r["ok"] and r["verdict"] in ("ready", "tight", "not_ready")
    h = get(client, "/history", params={"kind": "summary"})
    assert h["ok"] and h["driving"]["trips"] > 0


def test_alert_check_ack_and_rules(client):
    svc = client.svc
    svc.adapter.control("event", kind="software_update")
    assert post(client, "/alerts/check")["ok"]
    alerts = get(client, "/summary")["alerts"]
    assert any(a["rule"] == "software_update" for a in alerts)
    assert post(client, "/alerts/ack", {"id": "all"})["acknowledged"]
    rules = get(client, "/rules")["rules"]
    assert len(rules) == 9
    r = post(client, "/rules/left_unlocked_away", {"enabled": False, "settings": {"minutes": 30}})
    assert r["ok"] and r["now"]["enabled"] is False and r["now"]["minutes"] == 30
    assert post(client, "/rules/nope", {"enabled": True})["ok"] is False
    assert post(client, "/rules/left_unlocked_away", {"settings": {"bogus": 1}})["ok"] is False


def test_sim_controls_and_discover(client):
    r = post(client, "/sim", {"action": "advance", "params": {"minutes": 30}})
    assert r["ok"] and "30" in r["note"]
    assert post(client, "/sim", {"action": "explode"})["ok"] is False
    assert post(client, "/discover")["ok"] is False        # no HA configured


def test_import_endpoint_reports_clean_error_when_teslamate_not_configured(client):
    r = post(client, "/import", {"source": "teslamate", "days": 30})
    assert r["ok"] is False and "TeslaMate" in r["error"]


def test_approval_class_commands_are_never_executed_from_the_page(client):
    """P1-2 / catalog rule 12: Hermes' approval prompt is the only live approval."""
    post(client, "/arm", {"hours": 1, "confirmed": True})
    pf = post(client, "/preflight", {"command": "unlock"})
    assert pf["ok"] and pf["via_hermes"] is True and "confirm_token" not in pf and pf["ask_hermes"].startswith("Unlock")
    # no click, token or forged flag can make the page send it
    for body in ({"command": "unlock", "confirmed": True},
                 {"command": "unlock", "confirmed": True, "confirm_token": "anything", "approved": True}):
        r = post(client, "/command", body)
        assert r["ok"] is False and r["status"] == "ask_hermes" and r["ask_hermes"]
    assert client.svc.adapter.fetch_state(client.svc.now()).value("locked") is True       # untouched
    assert not [x for x in client.svc.audit_log(20) if x["command"] == "unlock" and x["mode"] == "live"]
    # a safe-direction command still runs from the page
    r = post(client, "/command", {"command": "lock", "confirmed": True})
    assert r["ok"] and r["status"] in ("confirmed", "duplicate")


def test_dry_run_preview_of_an_approval_class_command_sends_nothing(client):
    r = post(client, "/command", {"command": "open_frunk", "confirmed": True})
    assert r["status"] == "dry_run" and r["dry_run"] is True
    assert client.svc.adapter.fetch_state(client.svc.now()).value("frunk_open") is False


def test_alerts_carry_a_suggested_action_routed_by_risk(client):
    client.svc.adapter.control("drive", miles=5, to="away")
    client.svc.store.alert_add(ts=client.svc.now(), rule="left_unlocked_away", dedupe_key="k", severity="high", title="t",
                               body="b", suggestion="s", data={})
    alerts = get(client, "/summary")["alerts"]
    act = [a for a in alerts if a["rule"] == "left_unlocked_away"][0]["action"]
    assert act["command"] == "lock" and act["via_hermes"] is False
