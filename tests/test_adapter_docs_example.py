"""The adapter example in docs/ADAPTERS.md must really work (including the entry-point hook contract)."""

import re
from pathlib import Path

import pytest

from pit_crew.adapters import base

DOC = (Path(__file__).resolve().parents[1] / "docs" / "ADAPTERS.md").read_text()


def example_source() -> str:
    blocks = re.findall(r"```python\n(# my_car_adapter\.py.*?)```", DOC, flags=re.S)
    assert blocks, "example block missing from docs/ADAPTERS.md"
    return blocks[0]


@pytest.fixture
def clean_registry():
    saved = dict(base._ADAPTERS)
    yield
    base._ADAPTERS.clear()
    base._ADAPTERS.update(saved)


def test_doc_example_adapter_registers_and_runs_through_the_safety_engine(clean_registry, make_service, clock):
    ns = {}
    exec(compile(example_source(), "my_car_adapter.py", "exec"), ns)  # noqa: S102 - our own doc example
    ns["register"](base)              # what the entry-point loader does
    assert "mycar" in base.available_adapters()
    svc = make_service({"adapter": "mycar"})
    st = svc.get_state(force=True)
    assert st.value("battery_pct") == 63.0 and st.value("location_zone") == "home"
    assert svc.capabilities().commands == frozenset({"lock"})
    svc.arm(1)
    res = svc.run_command("lock", {}, surface="test")
    assert res["status"] == "confirmed"                    # locked already True -> read-back confirms
    assert svc.run_command("unlock", {}, surface="test")["status"] == "unsupported"   # never offered what it can't do
    assert svc.status()["ok"] is True


def test_broken_third_party_entry_point_cannot_break_loading(monkeypatch):
    class EP:
        name = "bad"

        def load(self):
            raise ImportError("boom")

    class EPs:
        def select(self, group):
            return [EP()]

    monkeypatch.setattr(base, "_entry_points_loaded", False)
    monkeypatch.setattr("importlib.metadata.entry_points", lambda: EPs())
    assert base.load_entry_point_adapters() == []      # logged and skipped
    base._entry_points_loaded = True
