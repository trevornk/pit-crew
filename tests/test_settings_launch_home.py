"""The settings provider reads the launch home, not the profile scope bound when the plugin registered.

Regression: a multiplex gateway loaded plugins under a transient profile's scope; when that profile was deleted every
settings read raised "Named profile home does not exist" and silently fell back to defaults, on every poll tick.
Runs against REAL Hermes (``tests/_launch_home_probe.py`` in the Hermes venv, v0.23.0 and v0.23.2)."""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERMES_PY = Path(os.environ.get("HERMES_PY", Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"))

pytestmark = pytest.mark.skipif(not HERMES_PY.exists(), reason="Hermes venv not available on this machine")


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    home = tmp_path_factory.mktemp("launch-home") / "home"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run([str(HERMES_PY), "-I", str(ROOT / "tests" / "_launch_home_probe.py"), str(home), str(ROOT)],
                          capture_output=True, text=True, timeout=180, env=env)
    for line in (proc.stdout + "\n" + proc.stderr).splitlines():
        if line.startswith("PROBE_JSON:"):
            return json.loads(line[len("PROBE_JSON:"):])
    pytest.fail(f"probe produced no result:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")


def test_reads_launch_home_after_registering_profile_is_deleted(probe):
    want = {"poll_interval_s": 123}
    assert probe["before_delete"] == want
    assert probe["after_delete"] == [want] * 5
    assert not [w for w in probe["ghost_warnings"] if "could not read" in w], probe["ghost_warnings"]


def test_unreadable_launch_home_warns_once_then_recovers(probe):
    # Hermes may serve its last-known-good copy for a broken file (no error) or fail; either way: at most one warning.
    assert all(r in ({}, {"poll_interval_s": 123}) for r in probe["broken"]), probe["broken"]
    assert len(probe["broken_warnings"]) <= 1, probe["broken_warnings"]
    assert probe["recovered"] == {"poll_interval_s": 123}
    assert probe["recovery_logged"] is bool(probe["broken_warnings"])
