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
        self.injected = []

    def current_cron_execution(self):
        return object() if self.cron else None

    def inject_message(self, content, role="user", *, session_key=None, origin=None):
        self.injected.append((content, session_key))
        return True


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
