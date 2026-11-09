"""Pass 9 UX regressions (AUDIT2-UX): vocabulary, shortcuts, honest numbers, setup guidance."""

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from pit_crew import slash

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))


# -- /car -----------------------------------------------------------------------------------------------------
def test_arm_hours_are_reported_truthfully(service):
    out = slash.handle("live 500 confirm")
    assert "72" in out and "500 became 72" in out
    assert service.armed_info()["armed"]
    slash.handle("practice")
    assert "0.25" in slash.handle("live 0 confirm")          # clamped up, and it says so


def test_alerts_ack_validates_and_acks_all_with_no_id(service):
    assert "numbers" in slash.handle("alerts ack abc")
    assert "no alert #99" in slash.handle("alerts ack 99")
    assert "No open alerts" in slash.handle("alerts ack")
    aid = service.store.alert_add(ts=1.0, rule="r", dedupe_key="k", severity="high", title="[Simulated] T", body="b", suggestion="Want me to lock it? (lock needs no approval)")
    text = slash.handle("alerts")
    assert "T: b" in text and "[Simulated]" not in text and "Say *lock it*" in text and "approval" not in text
    assert "Acknowledged 1 alert." in slash.handle("alerts ack")
    assert service.store.alerts_list(("acked",))[0]["id"] == aid


def test_lock_and_warm_shortcuts_go_through_the_engine(service):
    out = slash.handle("lock")
    assert "NOT contacted" in out and "/car live" in out            # practice mode: checked, logged, not sent
    assert service.store.audit_recent(1)[0]["surface"] == "slash"
    service.arm(1, by="t")
    out = slash.handle("warm 72")
    assert "72" in out
    assert service.store.audit_recent(1)[0]["command"] == "climate_on"


def test_ready_shortcut_and_help_groups(service):
    out = slash.handle("ready tomorrow 7:30 AM 40")
    assert any(w in out for w in ("Ready", "Tight", "Not ready"))
    assert "now →" in out
    help_ = slash.handle("help")
    for needle in ("Everyday", "Safety", "Setup", "/car ready", "/car lock", "/car warm", "/car live", "/car practice"):
        assert needle in help_
    assert max(len(l) for l in help_.splitlines()) < 110


def test_status_card_wording(service):
    card = slash.handle("")
    assert "Practice" in card and "DRY" not in card and "[SIMULATED]" not in card and "Data age" not in card
    assert "Updated" in card


def test_discover_without_url_says_what_to_do(make_service):
    make_service({"adapter": "homeassistant"})
    out = slash.handle("discover")
    assert "Home Assistant · URL" in out and "Settings" in out


# -- adapters / connection ---------------------------------------------------------------------------------------
def test_teslemetry_and_tessie_are_accepted_as_connection_names(store, clock):
    from pit_crew.adapters import base
    from pit_crew.adapters.base import AdapterError, BuildContext
    seen = {}

    def fake(ctx):
        seen["mode"] = ctx.settings["tesla_mode"]
        raise AdapterError("stop here")
    saved = base._ADAPTERS["tesla"]
    base._ADAPTERS["tesla"] = fake
    try:
        for name in ("teslemetry", "Tessie"):
            with pytest.raises(AdapterError):
                base.build_adapter(name, BuildContext(settings={"tesla_mode": "teslemetry"}, secret=lambda k: None))
            assert seen["mode"] == name.lower()
    finally:
        base._ADAPTERS["tesla"] = saved


def test_connection_info_explains_next_step(make_service):
    svc = make_service({"adapter": "homeassistant"})
    info = svc.connection_info(svc.status())
    assert info["connected"] is False and "URL" in info["message"] and "Settings" in info["hint"]
    sim = make_service()
    assert sim.connection_info(sim.status())["connected"] is True


def test_manifest_settings_are_grouped_labelled_and_choice_based():
    schema = MANIFEST["config_schema"]
    assert list(schema)[0] == "adapter" and "teslemetry" in schema["adapter"]["choices"]
    assert "Fleet API" not in schema["tesla_mode"]["description"] + MANIFEST["description"]
    assert "Teslemetry" in MANIFEST["description"]
    for k, v in schema.items():
        if v["type"] == "secret":
            assert "Stored as" not in v["description"], k       # Hermes appends that sentence itself
    groups = {v["label"].split(" · ")[0] for v in schema.values() if " · " in v["label"]}
    assert {"Connection", "Home Assistant", "Tesla", "Safety", "Alerts", "Advanced"} <= groups
    assert schema["live_mode"]["label"].startswith("Safety") and "armed" not in schema
    # nothing the user must edit is a raw key
    assert not any(v["label"].islower() and "_" in v["label"] for v in schema.values())


# -- backend ----------------------------------------------------------------------------------------------------------
@pytest.fixture
def api(service):
    spec = importlib.util.spec_from_file_location("car_api_ux", ROOT / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bad_bodies_are_errors_not_500s(api):
    assert api.arm({"confirmed": True, "hours": "soon"})["ok"] is False
    assert api.ack({"id": "abc"})["ok"] is False
    assert api.import_history({"days": "lots"})["ok"] is False


def test_arm_reports_the_real_hours(api):
    out = api.arm({"confirmed": True, "hours": 500})
    assert out["ok"] and out["hours"] == 72 and out["until_local"]


def test_summary_has_mode_and_connection(api):
    s = api.summary()
    assert s["armed"]["mode"] == "practice" and s["connection"]["connected"] is True
    assert s["armed"]["default_hours"] == 1


def test_tool_results_use_practice_wording(service):
    res = service.run_command("climate_on", {"temp_f": 70}, surface="tool")
    assert "PRACTICE RUN" in res["message"] and "climate on" in res["message"]
    pf = service.engine.preflight("climate_on", {"temp_f": 70})
    assert all("outside" not in c.detail for c in pf["policy"].checks)
    assert "Uses the history Pit Crew records itself" in __import__("pit_crew.schemas", fromlist=["x"]).CAR_HISTORY["description"]


# -- web page flows (jsdom) ---------------------------------------------------------------------------------------------
from pagerender import backend_summary, needs_web_render, render_web  # noqa: E402


@needs_web_render
def test_web_alert_fix_asks_for_confirmation_first(service, tmp_path):
    mod, summary = backend_summary(service)
    summary["alerts"] = [{"id": 1, "rule": "left_unlocked_away", "severity": "high", "state": "new", "title": "Your car is unlocked and away from home",
                          "body": "b", "suggestion": "Want me to lock it?", "when": "2026-11-08T22:06:00-06:00",
                          "action": {"command": "lock", "label": "Lock it", "via_hermes": False, "ask": "Lock"}}]
    pf = mod.preflight({"command": "lock"})
    out = render_web(tmp_path, {"/summary": summary, "/log": {"ok": True, "rows": []}, "/preflight": pf,
                                "/command": {"ok": True, "message": "sent"}, "/arm": {"ok": True}}, clicks=["Lock it"])
    posts = [c[0].rsplit("/", 1)[-1] for c in out["calls"] if c[1] == "POST"]
    assert posts == ["preflight"]                       # a click on the alert only previews; nothing was sent
    assert "Go live for 1 h &amp; send" in out["html"] and "Practice run" in out["html"]


@needs_web_render
def test_web_go_live_is_inline_with_cancel(service, tmp_path):
    mod, summary = backend_summary(service)
    out = render_web(tmp_path, {"/summary": summary, "/log": {"ok": True, "rows": []}}, clicks=["Go live…"])
    assert "Go live for 1 h?" in out["html"] and ">Cancel<" in out["html"] and "(simulated car)" in out["html"]
    assert [c for c in out["calls"] if c[1] == "POST"] == []
