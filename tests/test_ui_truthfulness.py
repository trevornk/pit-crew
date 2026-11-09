"""The pages must not say things that are no longer (or never were) true: stale command results, mixed clocks, wrong
"already in that state" notes, "nothing new" above open alerts, and the approval wording."""

import copy

import pytest

from pagerender import backend_summary, interact, needs_desktop_render, needs_web_render, render, render_web

pytest.importorskip("fastapi")

ALERT = {"id": 1, "rule": "left_unlocked_away", "severity": "high", "state": "new", "title": "Your car is unlocked and away from home",
         "body": "b", "suggestion": "Want me to lock it?", "when": "2026-11-08T22:06:00-06:00"}


def _with_locked(summary, locked):
    s = copy.deepcopy(summary)
    s["status"]["security"]["locked"] = locked
    return s


# 1. a finished command's panel is marked outdated once a different car state arrives
@needs_web_render
def test_web_command_result_is_marked_outdated_when_the_car_state_changes(service, tmp_path):
    mod, summary = backend_summary(service)
    unlocked, locked = _with_locked(summary, False), _with_locked(summary, True)
    data = {"/log": {"ok": True, "rows": []}, "/preflight": mod.preflight({"command": "lock"}),
            "/command": {"ok": True, "message": "Sent lock, car confirmed"},
            "__summary_sequence": [unlocked, locked, unlocked]}      # load, re-read after the command, then a sim Reset
    after_cmd = render_web(tmp_path, data, clicks=["Lock", "Practice run"])["html"]
    assert "Sent lock, car confirmed" in after_cmd and "out of date" not in after_cmd
    later = render_web(tmp_path, data, clicks=["Lock", "Practice run", "Refresh"])["html"]
    assert "out of date" in later and "Sent lock, car confirmed" not in later


@needs_desktop_render
def test_desktop_command_result_is_marked_outdated_when_the_car_state_changes(service, tmp_path):
    mod, summary = backend_summary(service)
    unlocked, locked = _with_locked(summary, False), _with_locked(summary, True)
    data = {"/rules": mod.rules(), "/log": {"ok": True, "rows": []}, "/preflight": mod.preflight({"command": "lock"}),
            "/command": {"ok": True, "status": "confirmed", "message": "Sent lock, car confirmed"},
            "__summary_sequence": [unlocked, locked, unlocked]}
    out = interact(tmp_path, data, clicks=["Lock", "Practice run", "!poll"])
    _, ran, polled = out["shots"]
    assert "Sent lock, car confirmed" in ran["dialog"] and "out of date" not in ran["dialog"]
    assert "out of date" in polled["dialog"] and "Sent lock, car confirmed" not in polled["dialog"]


# 2. simulated alert times are labelled; History says it is real time
@needs_desktop_render
def test_desktop_labels_simulated_alert_times_and_history_clock(service, tmp_path):
    mod, summary = backend_summary(service)
    summary["alerts"] = [ALERT]
    out = render(tmp_path, summary, rules=mod.rules())["page"]
    assert summary["simulated"] and "simulated " in out and "real clock time" in out


@needs_web_render
def test_web_labels_simulated_alert_times_and_history_clock(service, tmp_path):
    mod, summary = backend_summary(service)
    summary["alerts"] = [ALERT]
    html = render_web(tmp_path, {"/summary": summary, "/log": {"ok": True, "rows": []}})["html"]
    assert "simulated " in html and "real clock time" in html
    real = copy.deepcopy(summary)
    real["simulated"] = False
    html = render_web(tmp_path, {"/summary": real, "/log": {"ok": True, "rows": []}})["html"]
    assert "real clock time" not in html


def test_live_expiry_is_wall_clock_not_simulator_time(service):
    """Going live expires on the real wall clock even when the simulator's own clock is fast-forwarded."""
    service.adapter.control("advance", minutes=600)
    service.arm(hours=1) if hasattr(service, "arm") else None
    info = service.armed_info()
    assert info["armed"] is True
    assert abs(info["until"] - (service._wall() + 3600)) < 5


# 3. one reason per disabled button, and the generic "already in that state" claim is gone
@needs_desktop_render
def test_disabled_buttons_give_their_own_reason_not_a_blanket_claim(service, tmp_path):
    mod, summary = backend_summary(service)
    summary["status"]["security"]["locked"] = False
    summary["status"]["charging"].update(plugged_in=False, charging_state="disconnected")
    out = render(tmp_path, summary, rules=mod.rules())["page"]
    assert "Not plugged in: plug the car in first" in out
    assert "are already in that state" not in out and "says why" in out


@needs_web_render
def test_web_disabled_buttons_give_their_own_reason(service, tmp_path):
    mod, summary = backend_summary(service)
    summary["status"]["charging"].update(plugged_in=False, charging_state="disconnected")
    html = render_web(tmp_path, {"/summary": summary, "/log": {"ok": True, "rows": []}})["html"]
    assert "Not plugged in: plug the car in first" in html
    assert "are already in that state" not in html and "says why" in html


# 4. "nothing new" never sits above open alerts
@needs_web_render
def test_web_check_now_counts_open_alerts(service, tmp_path):
    mod, summary = backend_summary(service)
    summary["alerts"] = [ALERT, {**ALERT, "id": 2}]
    data = {"/summary": summary, "/log": {"ok": True, "rows": []}, "/alerts/check": {"ok": True, "raised": 0}}
    html = render_web(tmp_path, data, clicks=["Check now"])["html"]
    assert "No new alerts · 2 still open" in html and "nothing new" not in html
    summary["alerts"] = []
    html = render_web(tmp_path, data, clicks=["Check now"])["html"]
    assert "No new alerts · all clear" in html


@needs_desktop_render
def test_desktop_check_summary_wording():
    import re
    src = (__import__("pagerender").ROOT / "desktop" / "plugin.js").read_text()
    assert "nothing new" not in src and "No new alerts" in src and re.search(r"still open", src)


# 5. one true approval sentence
@needs_web_render
def test_approval_copy_is_one_true_sentence_on_both_pages(service, tmp_path):
    mod, summary = backend_summary(service)
    html = render_web(tmp_path, {"/summary": summary, "/log": {"ok": True, "rows": []}})["html"]
    desk = render(tmp_path, summary, rules=mod.rules())["page"]
    for text in (html, desk):
        assert "with approvals off it sends without asking and its reply says so" in text
        assert "never sent from a button" not in text and "Everything works" not in text
        assert "pretends to be a car" in text
