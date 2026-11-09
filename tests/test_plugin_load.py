"""Load the plugin through the real Hermes plugin manager in an isolated scratch HERMES_HOME."""

import json
import shutil
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

HERMES_PY = Path(os.environ.get("HERMES_PY", Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"))
SCRATCH = Path(tempfile.gettempdir()) / "pit-crew-home"
ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(not HERMES_PY.exists(), reason="Hermes venv not available on this machine")


def probe(armed: bool, tmp_name: str):
    home = SCRATCH / tmp_name
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True)
    proc = subprocess.run([str(HERMES_PY), str(ROOT / "tests" / "_load_probe.py"), str(home), str(ROOT), "1" if armed else "0"],
                          capture_output=True, text=True, timeout=180)
    combined = proc.stdout + "\n" + proc.stderr  # Hermes may route the child's stdout to stderr
    lines = [l for l in combined.splitlines() if l.startswith("PROBE_JSON:")]
    assert lines, f"probe failed:\nSTDOUT:\n{proc.stdout[-2000:]}\nSTDERR:\n{proc.stderr[-3000:]}"
    return json.loads(lines[-1][len("PROBE_JSON:"):])


@pytest.fixture(scope="module")
def dry():
    return probe(False, "load-dry")


@pytest.fixture(scope="module")
def live():
    return probe(True, "load-armed")


def test_plugin_loads_enabled_without_error(dry):
    assert dry["plugins"], "plugin not discovered"
    for info in dry["plugins"].values():
        assert info["enabled"] and not info["error"], info


def test_tools_registered_and_working(dry):
    assert {"car_status", "car_history", "car_readiness", "car_command"} <= set(dry["tools"])
    assert dry["status"]["ok"] and dry["status"]["vehicle"]["simulated"] is True
    assert dry["dry"]["status"] == "dry_run"


def test_all_surfaces_registered(dry):
    assert dry["tools"] == ["car_alerts", "car_command", "car_history", "car_readiness", "car_rules", "car_status"]
    assert dry["commands"] == ["car"]
    assert dry["blueprints"] == ["pit-crew:morning-brief", "pit-crew:nightly-security", "pit-crew:precondition"]
    assert dry["skills"] == ["pit-crew:car-agent"]
    assert dry["hooks"] == ["post_approval_response", "pre_llm_call", "pre_tool_call"]


def test_dry_run_default_needs_no_approval(dry):
    assert dry["directive"][0] is None  # nothing to approve when nothing will be sent


def test_armed_unlock_hook_returns_the_approve_directive(live):
    # The real hook returns the 'approve' directive (Hermes' gate then applies the user's approval setting; the real gate
    # with no human present is covered in test_hermes_approval_e2e). This probe never ran the gate, so the ticket is still
    # pending when the tool runs: that counts as "Hermes let it through" and is labelled so.
    assert live["directive_lock"][0] is None  # safe-direction lock needs no approval
    assert live["directive"][0] == "approve" and "UNLOCK" in live["directive"][1]
    assert live["unlock_result"]["status"] == "confirmed" and live["unlock_result"]["approval"] == "hermes_auto"
