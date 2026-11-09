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
if scenario in ("once", "deny", "timeout", "always"):
    os.environ["HERMES_INTERACTIVE"] = "1"
if scenario == "yolo":
    os.environ["HERMES_YOLO_MODE"] = "1"
os.makedirs(os.path.join(home, "plugins"), exist_ok=True)
link = os.path.join(home, "plugins", "pit-crew")
if not os.path.exists(link):
    os.symlink(repo, link)
with open(os.path.join(home, "config.yaml"), "w") as fh:
    fh.write("plugins:\n  enabled:\n    - pit-crew\n  entries:\n    pit-crew:\n      settings:\n        armed: true\n")

from hermes_cli.plugins import discover_plugins, resolve_pre_tool_block  # noqa: E402
from tools.registry import registry  # noqa: E402
from tools.terminal_tool import set_approval_callback  # noqa: E402

discover_plugins(force=True)
prompts = []


def human(command, description, **kw):
    prompts.append({"command": command, "description": description, "kw": sorted(kw)})
    return {"once": "once", "always": "always", "deny": "deny", "timeout": "timeout"}[scenario]


if scenario in ("once", "deny", "timeout", "always"):
    set_approval_callback(human)

out = {"scenario": scenario, "bridge": BRIDGE}
args = {"command": "unlock", "reason": "e2e test"}
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
