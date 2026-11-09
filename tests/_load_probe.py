"""Run inside the Hermes venv: load the plugin through the REAL plugin manager in an isolated HERMES_HOME
and exercise tools + the approval hook. Prints one JSON line. Invoked by test_plugin_load.py."""
import json
import os
import sys

home, repo, armed = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
os.environ["HERMES_HOME"] = home
os.makedirs(os.path.join(home, "plugins"), exist_ok=True)
link = os.path.join(home, "plugins", "pit-crew")
if not os.path.exists(link):
    os.symlink(repo, link)
with open(os.path.join(home, "config.yaml"), "w") as fh:
    fh.write("plugins:\n  enabled:\n    - pit-crew\n  entries:\n    pit-crew:\n      settings:\n"
             f"        armed: {'true' if armed else 'false'}\n")

from hermes_cli.plugins import discover_plugins, get_plugin_manager, get_pre_tool_call_directive  # noqa: E402
from tools.registry import registry  # noqa: E402

discover_plugins(force=True)
mgr = get_plugin_manager()
out = {"plugins": {k: {"enabled": v.enabled, "error": v.error} for k, v in mgr._plugins.items() if "crew" in k}}
out["tools"] = sorted(n for n in ("car_status", "car_history", "car_readiness", "car_command", "car_alerts", "car_rules")
                      if registry.get_entry(n) is not None)
out["status"] = json.loads(registry.dispatch("car_status", {}))
out["dry"] = json.loads(registry.dispatch("car_command", {"command": "lock"}))
out["directive"] = list(get_pre_tool_call_directive("car_command", {"command": "unlock"}, task_id="probe"))
out["directive_lock"] = list(get_pre_tool_call_directive("car_command", {"command": "lock"}, task_id="probe"))
out["unlock_result"] = json.loads(registry.dispatch("car_command", {"command": "unlock"}, task_id="probe"))
out["commands"] = sorted(mgr._plugin_commands)
out["blueprints"] = sorted(str(k) for k in getattr(mgr, "_automation_blueprints", {}))
out["skills"] = sorted(getattr(mgr, "_plugin_skills", {}))
out["hooks"] = sorted(h for h, cbs in mgr._hooks.items() if cbs and any("crew" in getattr(cb, "__module__", "") for cb in cbs))
print("PROBE_JSON:" + json.dumps(out, default=str))
