"""Run inside the Hermes venv: the plugin's settings provider must read the LAUNCH home, not the profile scope bound when
the plugin registered, and must warn once (not per read) on an unreadable config. Prints one JSON line.
Invoked by test_settings_launch_home.py."""
import importlib.util
import json
import logging
import os
import shutil
import sys

home, repo = sys.argv[1], sys.argv[2]
shutil.rmtree(home, ignore_errors=True)
ghost = os.path.join(home, "profiles", "ghost")
os.makedirs(ghost)
os.environ["HERMES_HOME"] = home
cfg = os.path.join(home, "config.yaml")
GOOD = "plugins:\n  entries:\n    pit-crew:\n      settings:\n        poll_interval_s: 123\n"
with open(os.path.join(ghost, "config.yaml"), "w") as fh:
    fh.write("plugins: {}\n")
with open(cfg, "w") as fh:
    fh.write(GOOD)

try:
    from hermes_constants import reset_hermes_home_key_cache
    reset_hermes_home_key_cache()
except ImportError:
    pass

spec = importlib.util.spec_from_file_location("pit_crew", os.path.join(repo, "__init__.py"), submodule_search_locations=[repo])
mod = importlib.util.module_from_spec(spec)
sys.modules["pit_crew"] = mod
spec.loader.exec_module(mod)
from pit_crew import settings as S  # noqa: E402


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


cap = Capture()
S.logger.addHandler(cap)
S.logger.setLevel(logging.DEBUG)
S.logger.propagate = False


def warnings():
    return [r.getMessage() for r in cap.records if r.levelno == logging.WARNING]


def bind_ghost():
    """Simulate 'registered under the ghost profile's scope'; returns the undo."""
    try:
        from hermes_scope import pop_state, push_state
        token = push_state(home=ghost)
        return lambda: pop_state(token)
    except ImportError:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        token = set_hermes_home_override(ghost)
        return lambda: reset_hermes_home_override(token)


out = {}
# 1. registering profile deleted
provider = S.hermes_settings_provider()
undo = bind_ghost()
try:
    out["before_delete"] = provider()
    shutil.rmtree(ghost)
    out["after_delete"] = [provider() for _ in range(5)]
    out["ghost_warnings"] = warnings()
finally:
    undo()

# 2. unreadable launch config: warn once, then recover
cap.records.clear()
provider = S.hermes_settings_provider()
with open(cfg, "w") as fh:
    fh.write("plugins: [unclosed\n")
out["broken"] = [provider() for _ in range(6)]
out["broken_warnings"] = warnings()
with open(cfg, "w") as fh:
    fh.write(GOOD + "\n# touched\n")
out["recovered"] = provider()
out["recovery_logged"] = any("readable again" in r.getMessage() for r in cap.records)
print("PROBE_JSON:" + json.dumps(out))
