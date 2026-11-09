"""Shared helpers for the tests that render the Desktop page and the web dashboard page in Node.

Node modules come from ``HERMES_NODE_MODULES`` (CI points it at ``tests/node/node_modules``, installed with
``npm ci`` from the pinned lockfile; locally it defaults to a Hermes checkout's ``node_modules``). When Node or a
needed package is missing the tests skip with a message that says exactly what to install.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
NODE_MODULES = Path(os.environ.get("HERMES_NODE_MODULES", str(Path.home() / ".hermes" / "hermes-agent" / "node_modules")))
INSTALL_HINT = "run `npm ci` in tests/node and set HERMES_NODE_MODULES=tests/node/node_modules"


def _missing(*packages: str) -> str:
    if not NODE:
        return "node is not installed"
    absent = [p for p in packages if not (NODE_MODULES / p).is_dir()]
    if absent:
        return f"{', '.join(absent)} not found in HERMES_NODE_MODULES={NODE_MODULES}"
    return ""


def needs_node(*packages: str):
    """Skip marker: Node plus the named npm packages must be available."""
    reason = _missing(*packages)
    return pytest.mark.skipif(bool(reason), reason=f"{reason} ({INSTALL_HINT})")


# The Desktop page is server-rendered (react + react-dom); the web dashboard runs in jsdom.
needs_desktop_render = needs_node("react", "react-dom")
needs_web_render = needs_node("react", "react-dom", "jsdom")


def _run(harness: Path, args: list, cwd=None) -> dict:
    env = {**os.environ, "HERMES_NODE_MODULES": str(NODE_MODULES)}
    proc = subprocess.run([NODE, str(harness), *args], capture_output=True, text=True, env=env, timeout=60,
                          cwd=str(cwd) if cwd else None)
    lines = [l for l in proc.stdout.splitlines() if l.startswith("HARNESS_JSON:")]
    assert lines, f"{harness.parent.name} harness failed:\n{proc.stdout[-1500:]}\n{proc.stderr[-2500:]}"
    return json.loads(lines[-1][len("HARNESS_JSON:"):])


def render(tmp_path, summary, **extra):
    data = {"summary": summary, "rules": {"ok": True, "rules": []}, "log": {"ok": True, "rows": []}, **extra}
    f = tmp_path / "data.json"
    f.write_text(json.dumps(data))
    return _run(ROOT / "tests" / "desktop" / "harness.mjs", [str(f)], cwd=ROOT / "tests" / "desktop")


def render_web(tmp_path, data, clicks=()):
    f = tmp_path / "web.json"
    f.write_text(json.dumps(data))
    return _run(ROOT / "tests" / "dashboard_web" / "harness.mjs", [str(f), json.dumps(list(clicks))])


def interact(tmp_path, data, clicks=()):
    """Click through the Desktop page in jsdom (stateful SDK stub): returns {'shots': [...], 'calls': [...]}."""
    f = tmp_path / "interact.json"
    f.write_text(json.dumps(data))
    return _run(ROOT / "tests" / "desktop" / "interact.mjs", [str(f), json.dumps(list(clicks))], cwd=ROOT / "tests" / "desktop")


def backend_summary(service):
    spec = importlib.util.spec_from_file_location("car_plugin_api", ROOT / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, mod.summary()
