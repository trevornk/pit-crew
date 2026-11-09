"""Background tasks without a Hermes secret scope (gateway.multiplex_profiles): honest status, one warning, turn-time fallback."""

import contextvars
import json
import logging
import threading

import pytest

import pit_crew
from pit_crew import slash, tools
from pit_crew.adapters import base
from pit_crew.adapters.base import CREDENTIAL_BG_TEXT, AdapterError, SecretScopeUnavailable
from pit_crew.service import BG_STATE, CarService, hermes_secret_getter, set_service
from pit_crew.settings import Settings
from fakes import FakeHA
from conftest import CronCtx

SCOPE = contextvars.ContextVar("fake_secret_scope", default=None)


class UnscopedSecretError(RuntimeError):      # same class NAME as Hermes' (matched by name, no import)
    pass


def install_fake_scope(monkeypatch, secrets):
    """Fake agent.secret_scope: values only readable while SCOPE is set; otherwise UnscopedSecretError."""
    import sys
    import types

    mod = types.ModuleType("agent.secret_scope")

    def get_secret(name, default=None):
        if SCOPE.get() is None:
            raise UnscopedSecretError(name)
        return secrets.get(name, default)

    mod.get_secret = get_secret
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", mod)


@pytest.fixture
def ha():
    f = FakeHA()
    yield f
    f.stop()


def ha_service(store, clock, ha, **over):
    svc = CarService(Settings(overrides={"adapter": "homeassistant", "history": "none", "ha_url": ha.url,
                                         "ha_slug": "demo_car", **over}),
                     hermes_secret_getter, store, wall_clock=clock, sleep=clock.sleep)
    svc.ctx = CronCtx()
    set_service(svc)
    return svc


def test_getter_distinguishes_unscoped_from_not_set(monkeypatch):
    install_fake_scope(monkeypatch, {"PIT_CREW_HA_TOKEN": "tok"})
    with pytest.raises(SecretScopeUnavailable):
        hermes_secret_getter("PIT_CREW_HA_TOKEN")           # exists, but no scope here
    SCOPE.set(object())
    assert hermes_secret_getter("PIT_CREW_HA_TOKEN") == "tok"
    assert hermes_secret_getter("PIT_CREW_OTHER") is None    # genuinely not set
    SCOPE.set(None)


def test_no_environment_fallback_even_when_the_variable_exists(monkeypatch):
    monkeypatch.setenv("PIT_CREW_HA_TOKEN", "from-the-environment")
    install_fake_scope(monkeypatch, {})
    with pytest.raises(SecretScopeUnavailable):
        hermes_secret_getter("PIT_CREW_HA_TOKEN")


def test_unscoped_status_says_background_limitation_not_missing_token(store, clock, ha, monkeypatch):
    install_fake_scope(monkeypatch, {"PIT_CREW_HA_TOKEN": FakeHA.TOKEN})
    svc = ha_service(store, clock, ha)
    out = json.loads(tools.car_status({}))
    assert out["ok"] is False and out["error"] == CREDENTIAL_BG_TEXT and out["warnings"] == [CREDENTIAL_BG_TEXT]
    assert "no Home Assistant token" not in json.dumps(out) and "store a long-lived" not in json.dumps(out)
    assert "alerts are checked during chats" in slash.handle("")
    assert slash.handle("discover").endswith(CREDENTIAL_BG_TEXT)


def test_not_set_keeps_the_existing_missing_token_text(store, clock, ha, monkeypatch):
    install_fake_scope(monkeypatch, {})
    SCOPE.set(object())
    try:
        svc = ha_service(store, clock, ha)
        out = json.loads(tools.car_status({}))
        assert "Home Assistant access token is missing" in out["error"]
        assert svc.background_state is None
    finally:
        SCOPE.set(None)


def test_watcher_enters_degraded_state_warns_once_and_recovers(store, clock, ha, monkeypatch, caplog):
    install_fake_scope(monkeypatch, {"PIT_CREW_HA_TOKEN": FakeHA.TOKEN})
    svc = ha_service(store, clock, ha)
    w = svc.watcher
    with caplog.at_level(logging.WARNING, logger="pit_crew.watcher.watcher"):
        for _ in range(5):
            res = w.poll_once()
            clock.advance(130)
    assert res["degraded"] == BG_STATE and res["polled"] is False
    assert svc.background_state == BG_STATE
    msgs = [r for r in caplog.records if "secrets are not readable from background tasks" in r.getMessage()]
    assert len(msgs) == 1                                   # once per process, not per poll
    assert not any("no Home Assistant token" in r.getMessage() for r in caplog.records)
    assert CREDENTIAL_BG_TEXT in svc.warnings()
    # patched Hermes: background reads work -> state clears automatically
    SCOPE.set(object())
    try:
        res = w.poll_once()
    finally:
        SCOPE.set(None)
    assert res["polled"] is True and svc.background_state is None
    assert CREDENTIAL_BG_TEXT not in svc.warnings()


def test_turn_fallback_runs_in_a_copied_context_is_rate_limited_and_single_flight(store, clock, ha, monkeypatch):
    install_fake_scope(monkeypatch, {"PIT_CREW_HA_TOKEN": FakeHA.TOKEN})
    svc = ha_service(store, clock, ha)
    svc.watcher.poll_once()                                 # background attempt -> degraded
    assert svc.background_state == BG_STATE
    gate, started, seen = threading.Event(), threading.Event(), []
    real = svc.watcher.poll_once

    def slow_poll(**kw):
        seen.append((SCOPE.get() is not None, kw))
        started.set()
        gate.wait(5)
        return real(**kw)

    monkeypatch.setattr(svc.watcher, "poll_once", slow_poll)
    SCOPE.set(object())                                      # we are "inside a turn"
    try:
        assert svc.watcher.schedule_turn_check() is True
        assert started.wait(5)
        assert svc.watcher.schedule_turn_check() is False    # single flight
        gate.set()
        for _ in range(100):
            if not svc.watcher._turn_in_flight:
                break
            threading.Event().wait(0.05)
        clock.advance(30)
        assert svc.watcher.schedule_turn_check() is False    # rate limited (poll_interval_s = 120)
        clock.advance(130)
        gate.set()
        assert svc.watcher.schedule_turn_check() is True
        for _ in range(100):
            if not svc.watcher._turn_in_flight:
                break
            threading.Event().wait(0.05)
    finally:
        SCOPE.set(None)
    assert all(in_scope for in_scope, _ in seen)             # the worker saw the turn's secret scope (copied context)
    assert all(kw.get("from_turn") for _, kw in seen)
    assert svc.background_state == BG_STATE                  # a turn-time success does NOT clear the background state


def test_turn_fallback_raises_alerts_and_respects_dedupe(store, clock, ha, monkeypatch):
    install_fake_scope(monkeypatch, {"PIT_CREW_HA_TOKEN": FakeHA.TOKEN})
    svc = ha_service(store, clock, ha)
    svc.watcher.poll_once()
    ha.set_state("lock.demo_car_lock", "unlocked"); ha.set_state("device_tracker.demo_car_location", "not_home")
    ha.set_state("sensor.teslamate_demo_car_state", "online")
    SCOPE.set(object())
    try:
        from datetime import datetime, timezone
        clock.t = 1_790_000_000.0
        for _ in range(3):
            for e in ha.states.values():     # Pass 8 (R16): rules ignore stale readings, so keep HA's stamps fresh
                e["last_reported"] = e["last_updated"] = datetime.fromtimestamp(clock.t - 5, tz=timezone.utc).isoformat()
            svc.watcher.poll_once(from_turn=True)
            clock.advance(11 * 60)
    finally:
        SCOPE.set(None)
    alerts = svc.alerts()
    assert [a["rule"] for a in alerts].count("left_unlocked_away") == 1     # raised once, deduped thereafter


def test_pre_llm_call_hook_does_not_block_and_swallows_errors(store, clock, ha, monkeypatch):
    install_fake_scope(monkeypatch, {"PIT_CREW_HA_TOKEN": FakeHA.TOKEN})
    svc = ha_service(store, clock, ha)
    svc.watcher.poll_once()
    started = threading.Event()
    release = threading.Event()

    def blocking_poll(**kw):
        started.set()
        release.wait(5)
        raise RuntimeError("boom")

    monkeypatch.setattr(svc.watcher, "poll_once", blocking_poll)
    SCOPE.set(object())
    try:
        import time
        t0 = time.monotonic()
        out = pit_crew._alert_context_hook(platform="telegram")
        assert time.monotonic() - t0 < 1.0                   # returned while the check is still running
        assert out is None and started.wait(5)
        release.set()
        for _ in range(100):
            if not svc.watcher._turn_in_flight:
                break
            threading.Event().wait(0.05)
        assert svc.watcher._turn_in_flight is False          # the error was swallowed and the flag released
    finally:
        SCOPE.set(None)


def test_hook_does_nothing_when_not_degraded(service):
    assert service.background_state is None
    assert service.watcher.schedule_turn_check() is False


def test_fallback_cannot_reach_any_command_path(store, clock, ha, monkeypatch):
    install_fake_scope(monkeypatch, {"PIT_CREW_HA_TOKEN": FakeHA.TOKEN})
    svc = ha_service(store, clock, ha)
    svc.arm(1, by="t")
    svc.watcher.poll_once()
    monkeypatch.setattr(svc.adapter if False else CarService, "run_command",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("command path reached")))
    SCOPE.set(object())
    try:
        svc.watcher.poll_once(from_turn=True)
        svc.watcher.schedule_turn_check()
    finally:
        SCOPE.set(None)
    assert ha.service_calls == [] and all(m == "GET" for m, *_ in ha.requests)
    import inspect
    from pit_crew.watcher import watcher as W
    src = inspect.getsource(W)
    assert "run_command" not in src and "send_command" not in src and "engine.execute" not in src
