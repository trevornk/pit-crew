"""End-to-end: car_command through Hermes' REAL approval pipeline, with a scripted human."""

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


def run(scenario: str):
    home = SCRATCH / f"approval-{scenario}"
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True)
    proc = subprocess.run([str(HERMES_PY), str(ROOT / "tests" / "_approval_probe.py"), str(home), str(ROOT), scenario],
                          capture_output=True, text=True, timeout=180)
    combined = proc.stdout + "\n" + proc.stderr
    lines = [l for l in combined.splitlines() if l.startswith("PROBE_JSON:")]
    assert lines, f"probe failed:\n{combined[-3000:]}"
    return json.loads(lines[-1][len("PROBE_JSON:"):])


def test_human_approves_once_then_unlock_is_confirmed_and_not_remembered():
    out = run("once")
    assert out["prompted"] == 1 and "UNLOCK" in out["prompt_text"] and "e2e test" in out["prompt_text"]
    assert out["block_message"] is None
    assert out["result"]["status"] == "confirmed" and out["result"]["approval"] == "confirmed"
    assert out["second_prompted"] == 1            # a new call asks again


def test_choosing_always_does_not_create_a_standing_permission():
    out = run("always")
    assert out["result"]["status"] == "confirmed"
    assert out["second_prompted"] == 1            # one-time rule key: "always" never matches again


def test_human_denies_means_nothing_is_sent():
    out = run("deny")
    assert out["prompted"] == 1 and out["block_message"] and out["result"] is None


def test_nobody_answers_means_nothing_is_sent():
    out = run("timeout")
    assert out["block_message"] and out["result"] is None


def test_no_human_present_fails_closed():
    out = run("nohuman")
    assert out["prompted"] == 0 and out["block_message"] and out["result"] is None


def test_yolo_mode_skips_hermes_prompt_but_strict_mode_still_refuses():
    out = run("yolo")
    assert out["prompted"] == 0 and out["block_message"] is None       # Hermes' gate auto-approved (user's YOLO choice)
    assert out["result"]["status"] == "blocked" and out["result"]["approval"] == "missing"   # ...our tool did not


# -- Same pipeline through Hermes Tool Search: hook fires for "tool_call", then the bridge re-dispatches car_command
#    with skip_pre_tool_call_hook=True. Real hook dispatch + real model_tools bridge re-dispatch. -----------------
def test_bridge_human_approves_once_then_unlock_is_confirmed():
    out = run("bridge_once")
    assert out["prompted"] == 1 and "UNLOCK" in out["prompt_text"]
    assert out["block_message"] is None
    assert out["result"]["status"] == "confirmed" and out["result"]["approval"] == "confirmed", out["result"]
    assert out["second_prompted"] == 1


def test_bridge_human_denies_means_nothing_is_sent():
    out = run("bridge_deny")
    assert out["prompted"] == 1 and out["block_message"] and out["result"] is None


def test_bridge_no_human_fails_closed():
    out = run("bridge_nohuman")
    assert out["prompted"] == 0 and out["block_message"] and out["result"] is None


def test_bridge_yolo_still_refused_by_strict_mode():
    out = run("bridge_yolo")
    assert out["prompted"] == 0 and out["block_message"] is None
    assert out["result"]["status"] == "blocked" and out["result"]["approval"] == "missing", out["result"]


def test_bridge_batch_with_approval_command_refused_before_any_prompt():
    out = run("bridge_batch")
    assert out["prompted"] == 0 and out["block_message"] and "one per tool_call" in out["block_message"]
