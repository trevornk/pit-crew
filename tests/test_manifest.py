"""plugin.yaml must stay truthful: schema == real settings, secrets == constants, identity in one place."""

import json
from pathlib import Path

import yaml

from pit_crew import constants
from pit_crew.settings import DEFAULTS

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))


def test_identity_in_one_place():
    assert MANIFEST["name"] == constants.PLUGIN_ID
    assert json.loads((ROOT / "dashboard" / "manifest.json").read_text())["name"] == constants.PLUGIN_ID
    assert f"const PLUGIN_ID = '{constants.PLUGIN_ID}'" in (ROOT / "desktop" / "plugin.js").read_text()


def test_manifest_v2_fields():
    assert MANIFEST["manifest_version"] == 2 and MANIFEST["license"] == "MIT"
    assert MANIFEST["requires_hermes"].startswith(">=")
    assert "requires_env" not in MANIFEST            # nothing gates loading on an env var
    assert MANIFEST["python_dependencies"] == ["tesla-fleet-api>=1.5,<2"]    # bounded; imported lazily


def test_config_schema_matches_settings_defaults():
    schema = MANIFEST["config_schema"]
    secrets = {k for k, v in schema.items() if v["type"] == "secret"}
    assert set(schema) - secrets == set(DEFAULTS)
    for key, default in DEFAULTS.items():
        spec = schema[key]
        assert spec["default"] == default, key
        assert spec["label"] and spec["description"]
        want = {bool: "bool", int: "int", float: "float", str: "str", list: "list", dict: "dict"}[type(default)]
        assert spec["type"] == want, key
    assert {schema[k]["env"] for k in secrets} == {constants.ENV_HA_TOKEN, constants.ENV_TESLAMATE_TOKEN, constants.ENV_TESLA_TOKEN}


def test_declared_tools_and_hooks_equal_registered(monkeypatch):
    import pit_crew

    class Ctx:
        def __init__(self):
            self.tools, self.hooks, self.cmds = [], [], []

        def __getattr__(self, name):
            return lambda *a, **k: None

        def register_tool(self, name, **kw):
            self.tools.append(name)

        def register_hook(self, hook, cb, **kw):
            self.hooks.append(hook)

        def register_command(self, name, *a, **k):
            self.cmds.append(name)

    monkeypatch.setattr(pit_crew, "start_watcher", lambda *a, **k: "stub")
    ctx = Ctx()
    pit_crew.register(ctx)
    assert sorted(ctx.tools) == sorted(MANIFEST["provides_tools"])
    assert sorted(ctx.hooks) == sorted(MANIFEST["provides_hooks"])
    assert ctx.cmds == [constants.SLASH_COMMAND]


def test_no_secrets_or_private_data_in_repo():
    import re

    bad = re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.|\b5YJ[A-HJ-NPR-Z0-9]{14}\b|-?\d{1,2}\.\d{5,},\s*-?\d{1,3}\.\d{5,}")
    for path in ROOT.rglob("*"):
        if path.is_dir() or any(p in path.parts for p in (".git", ".venv", "node_modules", "__pycache__")):
            continue
        if "tests" in path.parts and path.suffix == ".py":
            continue  # redaction tests deliberately contain synthetic VIN/coordinate/token strings
        if path.suffix in (".py", ".js", ".md", ".yaml", ".yml", ".json", ".mjs", ".txt"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert not bad.search(text), f"secret-looking content in {path.relative_to(ROOT)}"


def test_tesla_mode_choices_exclude_the_unsupported_fleet_mode():
    assert MANIFEST["config_schema"]["tesla_mode"]["choices"] == ["teslemetry", "tessie"]
    assert DEFAULTS["charge_schedule_start"] == "" and "charge_schedule_start" in MANIFEST["config_schema"]


def test_go_live_defaults_to_one_hour_everywhere(service):
    """B2: the default duration is 1 h in the setting, the manifest, the help, the confirmation and the first arm."""
    import re
    from pathlib import Path

    import yaml
    from pit_crew import settings as S
    from pit_crew import slash

    root = Path(__file__).resolve().parent.parent
    manifest = yaml.safe_load((root / "plugin.yaml").read_text())
    assert manifest["config_schema"]["arm_default_hours"]["default"] == 1
    assert S.DEFAULTS["arm_default_hours"] == 1
    assert "1 h default" in slash.HELP
    assert service.clamp_hours(None) == 1.0
    assert service.clamp_hours(1000) == 72.0 and service.clamp_hours(0.01) == 0.25      # the limits did not move
    text = slash.handle("live")
    assert "Go live for 1 hour?" in text and "/car live 1 confirm" in text
    assert not re.search(r"\b8 ?h(ours)?\b", (root / "README.md").read_text().split("/car live")[1][:200])


def test_unknown_subcommand_and_help_name_the_running_version(service):
    import yaml
    from pathlib import Path

    from pit_crew import slash
    from pit_crew.constants import VERSION

    manifest = yaml.safe_load((Path(__file__).resolve().parent.parent / "plugin.yaml").read_text())
    assert VERSION == str(manifest["version"])                           # read from plugin.yaml, not written twice
    assert f"Unknown subcommand 'x' (Pit Crew {VERSION})." in slash.handle("x")
    assert slash.handle("help").startswith(f"Pit Crew {VERSION}\n")
