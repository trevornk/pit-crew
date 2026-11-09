"""Render the Desktop plugin (page + chip) in Node against real backend JSON from the simulator."""

import json

import pytest

from pagerender import ROOT, ask_hermes, backend_summary, needs_desktop_render, needs_web_render, render, render_web  # noqa: E402

pytest.importorskip("fastapi")


@needs_desktop_render
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
                   "Asked in chat, under your Hermes approval setting", "Lock", "Warm up", "Charge", "Ask Hermes", "Unlock", "Honk", "Connect my real car",
                   "This is a simulated car"):
        assert needle in page, needle
    assert "%" in chip and "codicon-lock" in chip
    # the chip is a data-form status-bar item (hideable from the bar's right-click menu), registered once, with its own guard
    chip_c = next(c for c in out["contributions"] if c["id"] == "chip")
    assert not chip_c["hasRender"] and chip_c["data"]["toggleLabel"] == "Pit Crew" and chip_c["data"]["id"] == "pit-crew:chip"
    assert chip_c["data"]["to"] == "/car" and chip_c["data"]["label"] == "[element]"
    assert sum(1 for c in out["contributions"] if c["area"] == "statusbar.right") == 1
    assert "plugin.pit-crew.state.changed" in out["events"] and "plugin.pit-crew.settings.changed" in out["events"]


@needs_desktop_render
def test_page_shows_armed_unlocked_alert_and_stale(service, tmp_path):
    service.arm(2, by="t")
    service.adapter.control("leave_unlocked")
    service.adapter.control("drive", miles=10, to="away")
    service.watcher.poll_once(force=True)           # condition starts now ...
    service.adapter.control("advance", minutes=12)   # ... and has held for longer than the 10 min threshold
    service.watcher.poll_once(force=True)
    mod, summary = backend_summary(service)
    out = render(tmp_path, summary, rules=mod.rules())
    assert "Live until" in out["page"] and "Stop" in out["page"] and "Unlocked" in out["page"] and "unlocked and away from home" in out["page"]
    assert "codicon-unlock" in out["chip"] and 'data-dot="warn"' in out["chip"]


@needs_desktop_render
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


@needs_desktop_render
def test_page_shows_motion_warning_for_cars_without_a_motion_source(stub_service, stub, tmp_path):
    for k in ("moving", "speed_mph", "shift_state"):
        stub.values.pop(k)
    svc = stub_service()
    mod, summary = backend_summary(svc)
    assert summary["status"]["warnings"]
    out = render(tmp_path, summary, rules=mod.rules())
    assert "Needs attention" in out["page"] and "reports no speed, gear or driving state" in out["page"]


@needs_desktop_render
def test_stale_readings_say_last_known_with_humane_ages_and_never_live(service, tmp_path):
    mod, summary = backend_summary(service)
    fr = summary["status"]["freshness"]
    fr["stale_fields"] = {"doors_open": 17280, "windows_open": 17280, "locked": 17280, "plugged_in": 17280}
    fr["newest_update_age_s"] = 420
    out = render(tmp_path, summary, rules=mod.rules())["page"]
    assert "last known · 5 h ago" in out and "as of 5 h ago" in out
    assert "4.8" not in out and "live ·" not in out and "stale ·" not in out
    assert "confirmed 7 min ago" in out and "car last reported 7 min ago" in out


@needs_desktop_render
def test_commands_explain_inapplicable_ones_and_risky_ones_go_through_hermes(service, tmp_path):
    mod, summary = backend_summary(service)
    st = summary["status"]
    st["security"].update(locked=True, sentry_on=False)
    st["charging"].update(plugged_in=False, charging_state="disconnected")
    out = render(tmp_path, summary, rules=mod.rules())["page"]
    for reason in ("Already locked", "Sentry is already off", "Not plugged in", "Not charging right now"):
        assert reason in out, reason
    assert "requested in chat, not sent by these buttons" in out and "Asked in chat, under your Hermes approval setting" in out
    assert "Practice mode · nothing is sent" in out


@needs_desktop_render
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


def _ask_data(service):
    mod, summary = backend_summary(service)
    summary["alerts"] = [{"id": 2, "rule": "left_unlocked_away", "severity": "high", "state": "new", "title": "Unlocked and away",
                          "body": "b", "suggestion": "Want me to unlock it?", "when": "2026-11-08T22:06:00-06:00",
                          "action": {"command": "unlock", "label": "x", "via_hermes": True, "ask": "Unlock my car."}}]
    return {"/summary": summary, "/rules": mod.rules(), "/log": {"ok": True, "rows": []}}


@needs_desktop_render
def test_ask_hermes_starts_a_new_session_with_the_hosts_action_and_retries_a_bounded_number_of_times(service, tmp_path):
    out = ask_hermes(tmp_path, _ask_data(service), "mounts_after_2")
    assert out["actions"] == ["session.new"] and out["navigated"] == 0
    assert out["inserts"] == [None, "new", "new", "new"]                 # succeeded on the third retry, then stopped
    assert out["timers"] == [250, 250, 250] and out["clipboard"] == []   # host timers, no hand-rolled sleep


@needs_desktop_render
def test_ask_hermes_gives_up_after_five_retries_and_copies_the_text(service, tmp_path):
    out = ask_hermes(tmp_path, _ask_data(service), "never")
    assert out["actions"] == ["session.new"] and out["inserts"] == [None] + ["new"] * 5 and len(out["timers"]) == 5
    assert len(out["clipboard"]) == 1 and out["clipboard"][0]   # the request text, for pasting


@needs_desktop_render
def test_ask_hermes_falls_back_to_navigating_when_the_host_has_no_run_action(service, tmp_path):
    out = ask_hermes(tmp_path, _ask_data(service), "no_runaction")
    assert out["actions"] == [] and out["navigated"] == 1 and out["inserts"][0] is None


@needs_desktop_render
def test_readiness_box_prefills_settings_and_explains_itself(service, tmp_path):
    mod, summary = backend_summary(service)
    out = render(tmp_path, summary, rules=mod.rules())["page"]
    assert ('value="07:30"' in out or 'value="tomorrow 07:30"' in out) and "Miles (one way)" in out and "Say when you&#x27;re leaving" in out


# -- the web-dashboard page (dashboard/dist/index.js) --------------------------------------------------------------
def test_web_dashboard_manifest_has_a_real_entry_so_the_tab_never_404s():
    m = json.loads((ROOT / "dashboard" / "manifest.json").read_text())
    assert m["entry"] == "dist/index.js" and (ROOT / "dashboard" / m["entry"]).is_file()
    assert m["tab"]["path"] == "/pit-crew" and not m["tab"].get("hidden")
    src = (ROOT / "dashboard" / "dist" / "index.js").read_text()
    assert '__HERMES_PLUGINS__.register("pit-crew"' in src
    import re
    assert not re.search(r"\beval\s*\(|new Function|innerHTML|dangerouslySetInnerHTML|document\.write", src)


@needs_web_render
def test_web_dashboard_page_renders_real_backend_json(service, tmp_path):
    mod, summary = backend_summary(service)
    out = render_web(tmp_path, {"/summary": summary, "/log": {"ok": True, "rows": service.audit_log(5)}})
    html = out["html"]
    assert out["name"] == "pit-crew"
    for needle in ("Pit Crew", "Simulated car", "of range", "Doors", "Will I be ready?", "Alerts", "Controls",
                   "Asked in chat, under your Hermes approval setting", "History of commands", "Try it: simulator controls", "Practice mode · nothing is sent", "Go live…"):
        assert needle in html, needle
    # approval-class commands are preview-only on this page: no way to send them
    assert "requested in chat, not sent by these buttons" in html
    assert [c for c in out["calls"] if c[1] == "POST"] == []        # rendering alone sends nothing


@needs_web_render
def test_web_dashboard_unreachable_backend_shows_help_not_a_blank_page(tmp_path):
    out = render_web(tmp_path, {})
    assert "isn’t running on this Hermes" in out["html"] and "hermes plugins enable pit-crew" in out["html"]


@needs_web_render
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
