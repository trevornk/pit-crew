"""Render the Desktop plugin (page + chip) in Node against real backend JSON from the simulator."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
NODE_MODULES = Path(os.environ.get("HERMES_NODE_MODULES", str(Path.home() / ".hermes" / "hermes-agent" / "node_modules")))

pytestmark = pytest.mark.skipif(not NODE or not (NODE_MODULES / "react-dom").exists(),
                                reason="needs node and a node_modules with react + react-dom")

pytest.importorskip("fastapi")


def render(tmp_path, summary, **extra):
    data = {"summary": summary, "rules": {"ok": True, "rules": []}, "log": {"ok": True, "rows": []}, **extra}
    f = tmp_path / "data.json"
    f.write_text(json.dumps(data))
    env = {**os.environ, "HERMES_NODE_MODULES": str(NODE_MODULES)}
    proc = subprocess.run([NODE, str(ROOT / "tests" / "desktop" / "harness.mjs"), str(f)], capture_output=True, text=True,
                          env=env, timeout=60, cwd=str(ROOT / "tests" / "desktop"))
    lines = [l for l in proc.stdout.splitlines() if l.startswith("HARNESS_JSON:")]
    assert lines, f"harness failed:\n{proc.stdout[-1500:]}\n{proc.stderr[-2500:]}"
    return json.loads(lines[-1][len("HARNESS_JSON:"):])


def backend_summary(service):
    import importlib.util

    spec = importlib.util.spec_from_file_location("car_plugin_api", ROOT / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, mod.summary()


def test_page_renders_with_simulator(service, tmp_path):
    mod, summary = backend_summary(service)
    assert summary["simulated"] is True
    rules = mod.rules()
    out = render(tmp_path, summary, rules=rules, log={"ok": True, "rows": service.audit_log(10)})
    assert out["id"] == "pit-crew"
    assert out["nav"] == {"path": "/car", "label": "Pit Crew", "codicon": "zap"}
    assert set(out["areas"]) == {"page:routes", "nav:sidebarNav", "chip:statusbar.right"}
    page, chip = out["page"], out["chip"]
    # hero, tiles, readiness, alerts, commands, audit, settings, simulator
    for needle in ("Simulated car", "Practice mode", "Go live…", "of range", "Climate", "Doors", "Windows", "Trunk",
                   "Frunk", "Sentry", "Will I be ready?", "Alerts", "All clear", "More controls", "History of commands", "Safety &amp; settings",
                   "Alert rules", "Try it: simulator controls", "Fast-forward 30 min", "Practice mode · nothing is sent", "charge limit",
                   "Hermes asks you first", "Lock", "Warm up", "Charge", "Ask Hermes", "Unlock", "Honk", "Connect my real car",
                   "This is a simulated car"):
        assert needle in page, needle
    assert "%" in chip and "codicon-lock" in chip


def test_page_shows_armed_unlocked_alert_and_stale(service, tmp_path):
    service.arm(2, by="t")
    service.adapter.control("leave_unlocked")
    service.adapter.control("drive", miles=10, to="away")
    service.watcher.poll_once(force=True)           # condition starts now ...
    service.adapter.control("advance", minutes=12)   # ... and has held for longer than the 10 min threshold
    service.watcher.poll_once(force=True)
    mod, summary = backend_summary(service)
    out = render(tmp_path, summary, rules=mod.rules())
    assert "LIVE until" in out["page"] and "Stop" in out["page"] and "Unlocked" in out["page"] and "unlocked and away from home" in out["page"]
    assert "codicon-unlock" in out["chip"] and 'data-dot="warn"' in out["chip"]


def test_page_handles_unreachable_car(make_service, tmp_path):
    svc = make_service({"adapter": "homeassistant", "ha_url": "http://127.0.0.1:9", "ha_slug": "x"},
                       secrets={"PIT_CREW_HA_TOKEN": "t"})
    mod, summary = backend_summary(svc)
    out = render(tmp_path, summary, rules={"ok": True, "rules": []})
    page = out["page"]
    assert "Connect your car" in page and "Open Pit Crew settings" in page and "Find my car in Home Assistant" in page
    assert "Cannot read the car" not in page and "All clear" not in page and "Needs attention" not in page
    assert page.count("Pit Crew can&#x27;t read your car yet.") + page.count("Pit Crew can’t read your car yet.") == 1   # the error is said once
    assert "Commands are unavailable until Pit Crew can read your car." in page and "Can’t check yet" in page
    assert "Car ?" in out["chip"]


def test_plugin_file_follows_catalog_rule_8():
    src = (ROOT / "desktop" / "plugin.js").read_text()
    import re

    assert not re.search(r"\beval\s*\(|new Function|dangerouslySetInnerHTML|document\.|window\.hermesDesktop|innerHTML", src)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(", src), "hard-coded color"
    imports = re.findall(r"from '([^']+)'", src)
    assert set(imports) <= {"@hermes/plugin-sdk", "react"}


def test_page_shows_motion_warning_for_cars_without_a_motion_source(stub_service, stub, tmp_path):
    for k in ("moving", "speed_mph", "shift_state"):
        stub.values.pop(k)
    svc = stub_service()
    mod, summary = backend_summary(svc)
    assert summary["status"]["warnings"]
    out = render(tmp_path, summary, rules=mod.rules())
    assert "Needs attention" in out["page"] and "reports no speed, gear or driving state" in out["page"]


def test_stale_readings_say_last_known_with_humane_ages_and_never_live(service, tmp_path):
    mod, summary = backend_summary(service)
    fr = summary["status"]["freshness"]
    fr["stale_fields"] = {"doors_open": 17280, "windows_open": 17280, "locked": 17280, "plugged_in": 17280}
    fr["newest_update_age_s"] = 420
    out = render(tmp_path, summary, rules=mod.rules())["page"]
    assert "last known · 5 h ago" in out and "as of 5 h ago" in out
    assert "4.8" not in out and "live ·" not in out and "stale ·" not in out
    assert "confirmed 7 min ago" in out and "car last reported 7 min ago" in out


def test_commands_explain_inapplicable_ones_and_risky_ones_go_through_hermes(service, tmp_path):
    mod, summary = backend_summary(service)
    st = summary["status"]
    st["security"].update(locked=True, sentry_on=False)
    st["charging"].update(plugged_in=False, charging_state="disconnected")
    out = render(tmp_path, summary, rules=mod.rules())["page"]
    for reason in ("Already locked", "Sentry is already off", "Not plugged in", "Not charging right now"):
        assert reason in out, reason
    assert "never sent from a button" in out and "Hermes asks you first" in out
    assert "Practice mode · nothing is sent" in out


def test_single_alert_has_one_acknowledge_and_a_fix_button(service, tmp_path):
    mod, summary = backend_summary(service)
    alert = {"id": 1, "rule": "left_unlocked_away", "severity": "high", "state": "new", "title": "Your car is unlocked and away from home",
             "body": "Seen unlocked at 'away' at 10:06 PM.", "suggestion": "Want me to lock it?", "when": "2026-11-08T22:06:00-06:00",
             "action": {"command": "lock", "label": "Lock it", "via_hermes": False, "ask": "Lock"}}
    summary["alerts"] = [alert]
    out = render(tmp_path, summary, rules=mod.rules())["page"]
    assert "Acknowledge all" not in out and out.count(">Got it<") == 1 and "Lock it" in out
    alert2 = dict(alert, id=2, action={"command": "unlock", "label": "x", "via_hermes": True, "ask": "Unlock my car."})
    summary["alerts"] = [alert, alert2]
    out = render(tmp_path, summary, rules=mod.rules())["page"]
    assert "Acknowledge all" in out and ">Ask Hermes<" in out.replace("</i>", "")


def test_readiness_box_prefills_settings_and_explains_itself(service, tmp_path):
    mod, summary = backend_summary(service)
    out = render(tmp_path, summary, rules=mod.rules())["page"]
    assert ('value="07:30"' in out or 'value="tomorrow 07:30"' in out) and "Miles (one way)" in out and "Say when you&#x27;re leaving" in out


# -- the web-dashboard page (dashboard/dist/index.js) --------------------------------------------------------------
def render_web(tmp_path, data, clicks=()):
    f = tmp_path / "web.json"
    f.write_text(json.dumps(data))
    env = {**os.environ, "HERMES_NODE_MODULES": str(NODE_MODULES)}
    proc = subprocess.run([NODE, str(ROOT / "tests" / "dashboard_web" / "harness.mjs"), str(f), json.dumps(list(clicks))], capture_output=True, text=True,
                          env=env, timeout=60)
    lines = [l for l in proc.stdout.splitlines() if l.startswith("HARNESS_JSON:")]
    assert lines, f"web harness failed:\n{proc.stdout[-1500:]}\n{proc.stderr[-2500:]}"
    return json.loads(lines[-1][len("HARNESS_JSON:"):])


def test_web_dashboard_manifest_has_a_real_entry_so_the_tab_never_404s():
    m = json.loads((ROOT / "dashboard" / "manifest.json").read_text())
    assert m["entry"] == "dist/index.js" and (ROOT / "dashboard" / m["entry"]).is_file()
    assert m["tab"]["path"] == "/pit-crew" and not m["tab"].get("hidden")
    src = (ROOT / "dashboard" / "dist" / "index.js").read_text()
    assert '__HERMES_PLUGINS__.register("pit-crew"' in src
    import re
    assert not re.search(r"\beval\s*\(|new Function|innerHTML|dangerouslySetInnerHTML|document\.write", src)


def test_web_dashboard_page_renders_real_backend_json(service, tmp_path):
    mod, summary = backend_summary(service)
    out = render_web(tmp_path, {"/summary": summary, "/log": {"ok": True, "rows": service.audit_log(5)}})
    html = out["html"]
    assert out["name"] == "pit-crew"
    for needle in ("Pit Crew", "Simulated car", "of range", "Doors", "Will I be ready?", "Alerts", "Controls",
                   "Hermes asks you first", "History of commands", "Try it: simulator controls", "Practice mode · nothing is sent", "Go live…"):
        assert needle in html, needle
    # approval-class commands are preview-only on this page: no way to send them
    assert "never sent from a button" in html
    assert [c for c in out["calls"] if c[1] == "POST"] == []        # rendering alone sends nothing


def test_web_dashboard_unreachable_backend_shows_help_not_a_blank_page(tmp_path):
    out = render_web(tmp_path, {})
    assert "isn’t running on this Hermes" in out["html"] and "hermes plugins enable pit-crew" in out["html"]


def test_web_dashboard_risky_command_only_previews_and_offers_the_hermes_sentence(service, tmp_path):
    mod, summary = backend_summary(service)
    summary["status"]["security"]["locked"] = True
    pf = mod.preflight({"command": "unlock"})
    out = render_web(tmp_path, {"/summary": summary, "/log": {"ok": True, "rows": []}, "/preflight": pf,
                                "/command": {"ok": False, "status": "ask_hermes", "message": "no"}}, clicks=["Unlock"])
    html = out["html"]
    assert "Unlock my car." in html and "Unlock?" in html
    posts = [c for c in out["calls"] if c[1] == "POST"]
    assert [c[0] for c in posts] == ["/api/plugins/pit-crew/preflight"]      # a preview, never a /command
