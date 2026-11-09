"""Alert delivery against the REAL Hermes ``send_message_tool`` (target parsing, platform config, home channel, relay guard,
secret scope, session context). Only the network sender is replaced, inside ``tests/_delivery_probe.py`` (runs in the Hermes
venv). The fake-sender unit tests are in test_watcher_loop_delivery.py."""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERMES_PY = Path(os.environ.get("HERMES_PY", Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"))

pytestmark = pytest.mark.skipif(not HERMES_PY.exists(), reason="Hermes venv not available on this machine")


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    home = tmp_path_factory.mktemp("delivery-home") / "home"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run([str(HERMES_PY), "-I", str(ROOT / "tests" / "_delivery_probe.py"), str(home), str(ROOT)],
                          capture_output=True, text=True, timeout=180, env=env)
    for line in proc.stdout.splitlines():
        if line.startswith("PROBE_JSON:"):
            return json.loads(line[len("PROBE_JSON:"):])
    pytest.fail(f"probe produced no result:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")


def _only_call(r):
    assert r["ok"] is True and r["reason"] == "" and len(r["calls"]) == 1, r
    return r["calls"][0]


def test_send_message_is_not_a_registered_tool_so_dispatch_tool_cannot_be_used(probe):
    assert probe["registered"]["send_message_in_registry"] is False


def test_telegram_targets_reach_the_telegram_sender_with_chat_thread_and_token(probe):
    c = _only_call(probe["telegram_chat"])
    assert (c["platform"], c["chat_id"], c["thread_id"], c["token"]) == ("telegram", "-1001234567890", None, "123456:ABCDEF")
    assert c["message"] == "hello from pit crew"
    assert _only_call(probe["telegram_thread"])["thread_id"] == "17585"


def test_a_bare_platform_goes_to_its_home_channel_and_says_what_to_set_when_there_is_none(probe):
    assert _only_call(probe["telegram_home"])["chat_id"] == "-1009999"
    r = probe["telegram_no_home"]
    assert r["ok"] is False and "home channel" in r["reason"] and r["calls"] == []


def test_matrix_room_target_keeps_the_room_id_intact(probe):
    c = _only_call(probe["matrix_room"])
    assert (c["platform"], c["chat_id"], c["thread_id"]) == ("matrix", "!abcDEF:example.org", None)


def test_discord_id_works_and_a_channel_name_needs_hermes_channel_directory(probe):
    assert _only_call(probe["discord_id"])["chat_id"] == "38472910562837"
    r = probe["discord_channel_by_name"]
    assert r["ok"] is False and "Could not resolve '#car'" in r["reason"] and r["kind"] == "refused" and r["calls"] == []


def test_signal_number_target(probe):
    assert _only_call(probe["signal_number"])["chat_id"] == "+15551234567"


def test_unknown_platform_unconfigured_platform_and_platform_errors_are_reported_not_swallowed(probe):
    assert "Unknown or unregistered" in probe["unknown_platform"]["reason"] and probe["unknown_platform"]["ok"] is False
    assert "not configured" in probe["telegram_unconfigured"]["reason"] and probe["telegram_unconfigured"]["calls"] == []
    r = probe["platform_error"]
    assert r["ok"] is False and "bot was blocked by the user" in r["reason"] and r["kind"] == "refused"


def test_platform_token_is_read_in_the_secret_scope_the_calling_thread_carries(probe):
    assert probe["scope_thread_with_copied_context"] == {"ok": True, "reason": "", "tokens": ["scoped-token-9"]}
    assert probe["scope_via_asyncio_to_thread"] == {"ok": True, "tokens": ["scoped-token-9"]}      # how Watcher.run delivers
    r = probe["scope_thread_without_context"]
    assert r["ok"] is False and r["calls"] == 0 and "not configured" in r["reason"]


def test_the_slash_context_becomes_a_send_message_target_for_each_platform(probe):
    t = probe["session_targets"]
    assert t["telegram"] == ["telegram:-1001234567890:17585", ""]
    assert t["matrix"] == ["matrix:!abcDEF:example.org", ""]
    assert t["discord"] == ["discord:38472910562837", ""] and t["signal"] == ["signal:+15551234567", ""]
    assert t["slack"] == ["slack:C0123ABCD", ""]
    assert t["tui"][0] is None and "Desktop/CLI" in t["tui"][1]


def test_list_targets_uses_hermes_own_list(probe):
    assert "No messaging platforms connected" in probe["list"]


def test_the_plugin_no_longer_calls_inject_message():
    for f in list(ROOT.glob("*.py")) + list((ROOT / "watcher").glob("*.py")) + list((ROOT / "dashboard").glob("*.py")):
        assert "inject_message(" not in f.read_text(), f
