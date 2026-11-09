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


def test_choosing_always_means_what_hermes_says():
    out = run("always")
    assert out["result"]["status"] == "confirmed" and out["result"]["approval"] == "confirmed"
    # stable rule key (pit-crew:unlock): Hermes remembers "always", so the next call is not asked again and runs.
    # Hermes approved it without asking, and Pit Crew says so.
    assert out["second_prompted"] == 0 and out["second_block"] is None


def test_choosing_session_skips_later_prompts_in_that_session():
    out = run("session")
    assert out["result"]["approval"] == "confirmed" and out["second_prompted"] == 0 and out["second_block"] is None


def test_require_human_for_asks_again_even_after_always():
    out = run("rhf_always")
    assert out["result"]["status"] == "confirmed" and out["result"]["approval"] == "confirmed"
    assert out["second_prompted"] == 1            # one-time rule key: "always" never matches again


def test_require_human_for_with_a_person_answering_is_confirmed():
    out = run("rhf_once")
    assert out["prompted"] == 1 and out["result"]["approval"] == "confirmed"


def test_human_denies_means_nothing_is_sent():
    out = run("deny")
    assert out["prompted"] == 1 and out["block_message"] and out["result"] is None


def test_nobody_answers_means_nothing_is_sent():
    out = run("timeout")
    assert out["block_message"] and out["result"] is None


def test_no_human_present_fails_closed():
    out = run("nohuman")
    assert out["prompted"] == 0 and out["block_message"] and out["result"] is None


def test_yolo_mode_follows_hermes_and_is_labelled_hermes_auto():
    out = run("yolo")
    assert out["prompted"] == 0 and out["block_message"] is None       # Hermes' gate auto-approved (user's YOLO choice)
    assert out["result"]["status"] == "confirmed" and out["result"]["approval"] == "hermes_auto"   # ...and so did Pit Crew
    assert "Sent without asking" in out["result"]["message"]


def test_approvals_mode_off_in_hermes_config_runs_without_asking():
    """Real _run_approval_gate with approvals.mode: off in Hermes' own config.yaml: approved before any prompt or hook."""
    out = run("off")
    assert out["prompted"] == 0 and out["block_message"] is None
    assert out["result"]["status"] == "confirmed" and out["result"]["approval"] == "hermes_auto", out["result"]
    assert "Sent without asking" in out["result"]["message"]


def test_require_human_for_refuses_when_hermes_approvals_are_off():
    out = run("rhf_off")
    assert out["result"] is None and out["block_message"]
    assert "approvals are off" in out["block_message"] and "Always ask a person" in out["block_message"]


def test_require_human_for_refuses_under_yolo():
    out = run("rhf_yolo")
    assert out["result"] is None and out["block_message"] and "approvals are off" in out["block_message"]


# -- Hermes Tool Search, the REAL agent-loop sequence: agent.tool_executor._parse_tool_call unwraps `tool_call` ->
#    `car_command` before _pre_tool_block fires pre_tool_call, so the hook sees car_command itself (Hermes v0.23.0). ----
def test_tool_search_call_is_unwrapped_by_hermes_before_the_hook_and_gets_the_approval_prompt():
    out = run("executor")
    assert out["unwrapped_name"] == "car_command" and out["scope_block"] is None
    assert out["prompted"] == 1 and "UNLOCK" in out["prompt_text"] and out["block_message"] is None
    assert out["result"]["status"] == "confirmed" and out["result"]["approval"] == "confirmed"


# -- FALLBACK path only: if Hermes' unwrap ever fails, pre_tool_call fires for the raw "tool_call" name and the bridge then
#    re-dispatches car_command with skip_pre_tool_call_hook=True. This probe calls resolve_pre_tool_block("tool_call", ...)
#    directly, which is NOT what the v0.23.0 agent loop does; it proves the gate's defence-in-depth handling. ---------
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


def test_bridge_yolo_follows_hermes_and_is_labelled_hermes_auto():
    out = run("bridge_yolo")
    assert out["prompted"] == 0 and out["block_message"] is None
    assert out["result"]["status"] == "confirmed" and out["result"]["approval"] == "hermes_auto", out["result"]


def test_bridge_yolo_with_require_human_for_is_refused():
    out = run("bridge_rhf_yolo")
    assert out["result"] is None and out["block_message"] and "approvals are off" in out["block_message"]


def test_bridge_batch_with_approval_command_refused_before_any_prompt():
    out = run("bridge_batch")
    assert out["prompted"] == 0 and out["block_message"] and "one per tool_call" in out["block_message"]
