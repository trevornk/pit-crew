"""Test bootstrap: import the plugin directory as the package ``pit_crew``.

The plugin core is pure standard library, so these tests need neither Hermes nor network access.
Tests that exercise real Hermes loading live in ``test_plugin_load.py`` and run the ``hermes`` CLI
against an isolated scratch HERMES_HOME.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = "pit_crew"
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/fakes.py


def _load_package() -> None:
    if PKG in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(PKG, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = mod
    spec.loader.exec_module(mod)


_load_package()

FIXTURES = ROOT / "tests" / "fixtures" / "live-sanitized"


class FakeClock:
    """Controllable wall clock + sleep so time-dependent code is deterministic and instant."""

    def __init__(self, start: float = 1_790_000_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path):
    from pit_crew.store import open_store

    s = open_store(tmp_path / "data.db")
    yield s
    s.close()


@pytest.fixture
def make_service(store, clock):
    """Factory: build a CarService on the simulator (or any settings overrides)."""
    from pit_crew.service import CarService, set_service
    from pit_crew.settings import Settings

    created = []

    def factory(overrides: Optional[Dict[str, Any]] = None, secrets: Optional[Dict[str, str]] = None):
        settings = Settings(overrides=overrides or {})
        svc = CarService(settings, (secrets or {}).get, store, wall_clock=clock, sleep=clock.sleep)
        created.append(svc)
        set_service(svc)
        return svc

    yield factory
    set_service(None)


@pytest.fixture
def service(make_service):
    return make_service()


class CronCtx:
    """Stand-in for the Hermes PluginContext: reports whether we are inside a cron run."""

    def __init__(self, cron: bool = False):
        self.cron = cron

    def current_cron_execution(self):
        return object() if self.cron else None


class FakeSender:
    """Stands in for Hermes' ``send_message`` (``delivery.hermes_send``): records ``(text, target)``; scripted outcome."""

    def __init__(self):
        self.sent = []
        self.outcome = (True, "", None)
        self.exc = None

    def fail(self, reason="Send failed: boom", kind="refused"):
        self.outcome = (False, reason, kind)

    def ok(self):
        self.outcome = (True, "", None)

    def __call__(self, target, text):
        if self.exc:
            raise self.exc
        self.sent.append((text, target))
        return self.outcome


@pytest.fixture(autouse=True)
def sender(monkeypatch):
    """Every test gets a fake sender: no test can reach a real platform. Real ``send_message`` resolution is tested in
    ``test_delivery_hermes.py`` against the real Hermes with only the network sender mocked."""
    fs = FakeSender()
    monkeypatch.setattr("pit_crew.watcher.delivery.hermes_send", fs)
    return fs


@pytest.fixture
def stub(clock):
    from stubs import StubAdapter

    return StubAdapter(clock)


@pytest.fixture
def stub_service(store, clock, stub):
    """A service on the scriptable stub adapter. ``stub_service(armed=True, key=value…)``."""
    from pit_crew.adapters import base
    from pit_crew.service import CarService, set_service
    from pit_crew.settings import Settings

    if "stub" not in base._ADAPTERS:
        base.register_adapter("stub", lambda ctx: _STUB_HOLDER["adapter"])
    _STUB_HOLDER["adapter"] = stub

    def factory(**overrides):
        overrides.setdefault("adapter", "stub")
        overrides.setdefault("history", "none")
        armed = overrides.pop("armed", False)
        svc = CarService(Settings(overrides=overrides), {}.get, store, wall_clock=clock, sleep=clock.sleep)
        svc.ctx = CronCtx()
        if armed:
            svc.arm(8, by="test")
        set_service(svc)
        return svc

    yield factory
    set_service(None)


_STUB_HOLDER: Dict[str, Any] = {}
