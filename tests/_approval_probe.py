"""Run inside the Hermes venv. Exercises the REAL approval pipeline end to end for car_command:
pre_tool_call hook -> Hermes' approval gate (with a scripted 'human') -> post_approval_response -> tool.
Prints one JSON line. Invoked by test_hermes_approval_e2e.py."""
import json
import os
import sys

home, repo, scenario = sys.argv[1], sys.argv[2], sys.argv[3]
os.environ["HERMES_HOME"] = home
BRIDGE = scenario.startswith("bridge_")
if BRIDGE:
    scenario = scenario[len("bridge_"):]
# "rhf_<scenario>": the user listed unlock under require_human_for ("Always ask a person for these")
RHF = scenario.startswith("rhf_")
if RHF:
    scenario = scenario[len("rhf_"):]
# "off" = Hermes config approvals.mode: off (the real _run_approval_gate returns approved before any prompt or hook)
APPROVALS_OFF = scenario in ("off", "off_bridge")
if scenario in ("once", "deny", "timeout", "always", "session", "executor"):
    os.environ["HERMES_INTERACTIVE"] = "1"
if scenario == "yolo":
    os.environ["HERMES_YOLO_MODE"] = "1"
os.makedirs(os.path.join(home, "plugins"), exist_ok=True)
link = os.path.join(home, "plugins", "pit-crew")
if not os.path.exists(link):
    os.symlink(repo, link)
with open(os.path.join(home, "config.yaml"), "w") as fh:
    fh.write("plugins:\n  enabled:\n    - pit-crew\n  entries:\n    pit-crew:\n      settings:\n        armed: true\n"
             + ("        require_human_for:\n          - unlock\n" if RHF else "")
             + ("approvals:\n  mode: \"off\"\n" if APPROVALS_OFF else ""))

from hermes_cli.plugins import discover_plugins, resolve_pre_tool_block  # noqa: E402
from tools.registry import registry  # noqa: E402
from tools.terminal_tool import set_approval_callback  # noqa: E402

discover_plugins(force=True)
prompts = []


def human(command, description, **kw):
    prompts.append({"command": command, "description": description, "kw": sorted(kw)})
    return {"once": "once", "always": "always", "session": "session", "deny": "deny", "timeout": "timeout",
            "executor": "once"}[scenario]


if scenario in ("once", "deny", "timeout", "always", "session", "executor"):
    set_approval_callback(human)

out = {"scenario": scenario, "bridge": BRIDGE}
args = {"command": "unlock", "reason": "e2e test"}
if scenario == "executor":
    # The REAL agent-loop sequence for a Tool Search call: _parse_tool_call unwraps `tool_call` -> `car_command`
    # BEFORE _pre_tool_block fires pre_tool_call, so the hook sees car_command itself (agent/tool_executor.py).
    from types import SimpleNamespace  # noqa: E402
    from agent import tool_executor as te  # noqa: E402
    agent = SimpleNamespace(session_id="s", enabled_toolsets=None, disabled_toolsets=None, _current_turn_id="", _current_api_request_id="")
    tc = SimpleNamespace(id="call_1", type="function", function=SimpleNamespace(
        name="tool_call", arguments=json.dumps({"calls": [{"name": "car_command", "arguments": args}]})))
    parsed = te._parse_tool_call(agent, tc)
    out["unwrapped_name"], out["unwrapped_args"], out["scope_block"] = parsed.name, parsed.args, parsed.scope_block
    block, final_args = te._pre_tool_block(agent, parsed.ref("e2e"))
    out["block_message"] = block
    out["prompted"] = len(prompts)
    out["prompt_text"] = prompts[0]["description"] if prompts else None
    out["result"] = (json.loads(registry.dispatch(parsed.name, final_args, task_id="e2e", session_id="s"))
                     if block is None and parsed.name == "car_command" else None)
    out["second_prompted"] = 0
    print("PROBE_JSON:" + json.dumps(out, default=str))
    sys.exit(0)
if BRIDGE:
    # Tool Search path: Hermes fires pre_tool_call ONCE for the bridge name, then re-dispatches the underlying tool
    # with skip_pre_tool_call_hook=True (model_tools._dispatch_bridge_tool). Reproduce exactly that sequence.
    from model_tools import handle_function_call  # noqa: E402
    entries = [{"name": "car_command", "arguments": args}]
    if scenario == "batch":
        entries.append({"name": "car_command", "arguments": {"command": "honk"}})
    bridge_args = {"calls": entries}

    def run_bridge(task):
        blk = resolve_pre_tool_block("tool_call", bridge_args, task_id=task, session_id="s")
        if blk is not None:
            return blk, None
        res = handle_function_call("tool_call", bridge_args, task_id=task, session_id="s", skip_pre_tool_call_hook=True)
        try:
            return None, json.loads(res)
        except ValueError:
            return None, {"raw": res}

    block, result = run_bridge("e2e")
    out["block_message"], out["result"] = block, result
    out["prompted"] = len(prompts)
    out["prompt_text"] = prompts[0]["description"] if prompts else None
    block2, _ = run_bridge("e2e2")
    out["second_prompted"] = len(prompts) - out["prompted"]
    out["second_block"] = block2
    print("PROBE_JSON:" + json.dumps(out, default=str))
    sys.exit(0)
args = {"command": "unlock", "reason": "e2e test"}
block = resolve_pre_tool_block("car_command", args, task_id="e2e", session_id="s")
out["block_message"] = block
out["prompted"] = len(prompts)
out["prompt_text"] = prompts[0]["description"] if prompts else None
if block is None:
    out["result"] = json.loads(registry.dispatch("car_command", args, task_id="e2e", session_id="s"))
else:
    out["result"] = None
# a second identical call must need a fresh approval ("always"/"session" must not stick)
block2 = resolve_pre_tool_block("car_command", args, task_id="e2e2", session_id="s")
out["second_prompted"] = len(prompts) - out["prompted"]
out["second_block"] = block2
print("PROBE_JSON:" + json.dumps(out, default=str))
