"""Automated slices of docs/CATALOG_COMPLIANCE.md (the rules that can be checked statically)."""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def tracked():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split("\n")
        return [ROOT / f for f in out if f]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts and ".venv" not in p.parts]


def package_sources():
    return [p for p in tracked() if p.suffix in (".py", ".js") and "tests" not in p.relative_to(ROOT).parts]


def test_rule11_no_environment_or_credential_file_access():
    banned = re.compile(r"os\.environ|os\.getenv|\bgetenv\(|dotenv|auth\.json|\.env['\"]|keyring|keychain|"
                        r"refresh_token|token_store|tokenstore|TeslaFleetOAuth|get_private_key|client_secret")
    for p in package_sources():
        text = p.read_text(encoding="utf-8")
        # the docs in these modules say what they DON'T do; only code lines count
        code = "\n".join(l for l in text.splitlines() if not l.strip().startswith(("#", '"""', "*")) and '"""' not in l)
        hits = banned.findall(code)
        assert not hits, f"{p.relative_to(ROOT)} touches credentials outside the secret scope: {hits}"


def test_rule11_every_declared_secret_is_read_via_the_secret_scope():
    import yaml

    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    envs = {v["env"] for v in manifest["config_schema"].values() if v.get("type") == "secret"}
    src = (ROOT / "service.py").read_text()
    assert "from agent.secret_scope import get_secret" in src
    # adapters receive credentials only through ctx.secret(<constant>)
    used = set()
    for p in (ROOT / "adapters").glob("*.py"):
        used |= set(re.findall(r"ctx\.secret\((ENV_[A-Z_]+)\)", p.read_text()))
    from pit_crew import constants

    assert {getattr(constants, name) for name in used} == envs - set()


def test_rule3_no_self_update_or_process_spawning():
    banned = re.compile(r"\bsubprocess\b|urlretrieve|\bpip install\b|ensurepip|shutil\.rmtree|importlib\.reload|exec\(|eval\(")
    for p in package_sources():
        code = "\n".join(l for l in p.read_text(encoding="utf-8").splitlines() if not l.strip().startswith(("#", '"""')))
        # the only 'pip install' text allowed is the human-facing hint in an error message
        code = re.sub(r"`pip install[^`]*`", "", code)
        hits = banned.findall(code)
        assert not hits, f"{p.relative_to(ROOT)}: {hits}"


def test_rule17_no_binaries_bytecode_or_archives_tracked():
    magic = (b"\x7fELF", b"MZ", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\x00asm", b"PK\x03\x04",
             b"\x1f\x8b")
    for p in tracked():
        assert p.suffix not in (".pyc", ".pyo", ".so", ".dll", ".dylib", ".wasm", ".zip", ".tar", ".gz", ".whl"), p
        head = p.read_bytes()[:4]
        assert not any(head.startswith(m) for m in magic), f"{p.relative_to(ROOT)} looks like a binary/archive"


def test_rule10_dependency_is_bounded_and_ci_quarantines_releases():
    import yaml

    deps = yaml.safe_load((ROOT / "plugin.yaml").read_text())["python_dependencies"]
    assert deps == ["tesla-fleet-api>=1.5,<2"]
    assert all(re.search(r">=[\d.]+,<[\d.]+$", d) for d in deps)
    assert "--exclude-newer" in (ROOT / ".github" / "workflows" / "ci.yml").read_text()


def test_rule6_catalog_entry_matches_manifest():
    import yaml

    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    entry = yaml.safe_load((ROOT / "catalog" / "pit-crew.yaml").read_text())
    caps = entry["capabilities"]
    assert caps["provides_tools"] == manifest["provides_tools"]
    assert caps["provides_hooks"] == manifest["provides_hooks"]
    assert caps["provides_middleware"] == [] and caps["requires_env"] == [] and "requires_env" not in manifest
    assert entry["requires_hermes"] == manifest["requires_hermes"] and entry["version"] == manifest["version"]
    assert re.fullmatch(r"[a-z0-9_-]{1,64}", entry["name"]) and entry["name"] == manifest["name"]
    assert entry["tier"] == "community" and entry["category"] in {"desktop", "memory", "platform", "web", "tools", "voice",
                                                                  "automation", "models", "general"}


def test_rule13_disclosure_mentions_what_the_rule_asks_for():
    """The disclosure moved from the (one-line) catalog description to README + docs/PR_BODY.md (the PR description)."""
    readme = (ROOT / "README.md").read_text().lower()
    body = " ".join((ROOT / "docs" / "PR_BODY.md").read_text().lower().split())
    for needle in ("network", "background", "storage", "telemetry"):
        assert needle in readme, needle
    for needle in ("disclosure", "network calls", "background", "no stored credential", "telemetry", "sys.modules", "unlock"):
        assert needle in body, needle
    assert "no refresh" in body or "does no refresh" in body        # states plainly that no OAuth tokens are held
    assert "oauth" in body


def test_catalog_entry_is_one_line_with_images_pinned_to_a_sha_placeholder():
    import yaml
    entry = yaml.safe_load((ROOT / "catalog" / "pit-crew.yaml").read_text())
    assert "\n" not in entry["description"].strip() and len(entry["description"]) <= 260
    urls = [entry["image"], *entry["screenshots"]]
    assert 1 <= len(entry["screenshots"]) <= 6
    for url in urls:
        assert url.startswith("https://raw.githubusercontent.com/trevornk/pit-crew/") and "/docs/screenshots/" in url
        sha = url.split("/pit-crew/")[1].split("/")[0]
        assert sha == "TODO-SHA" or re.fullmatch(r"[0-9a-f]{40}", sha)     # the owner pins the real sha at submission
    assert entry["readme"] is True


def test_rule8_desktop_file_is_sdk_only():
    src = (ROOT / "desktop" / "plugin.js").read_text()
    assert set(re.findall(r"from '([^']+)'", src)) <= {"@hermes/plugin-sdk", "react"}
    assert not re.search(r"\bdocument\.|\bwindow\.|\beval\(|new Function|import\(|data-slot|data-testid|prototype", src)
