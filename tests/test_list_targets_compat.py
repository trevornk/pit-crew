"""``list_targets()`` (``/car notify list``) across Hermes versions: the channel-directory formatter when present, the legacy
``send_message`` ``action=list`` on older Hermes, None otherwise. Mocks only; discovery must never send or bind a target."""

import json
import sys
import types

import pytest

from pit_crew import slash
from pit_crew.watcher.delivery import list_targets

DIRECTORY = "Available messaging targets:\n\ntelegram:\n  home\ndiscord:\n  #car"


class Legacy:
    """Fake ``send_message_tool`` that records every call."""

    def __init__(self, result):
        self.result, self.calls = result, []

    def __call__(self, args, **kw):
        self.calls.append(dict(args))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _install(monkeypatch, directory=None, legacy=None):
    """``directory``: a callable (module present) or None (module absent). ``legacy``: a Legacy or None (module absent)."""
    for name, obj, attr in (("gateway.channel_directory", directory, "format_directory_for_display"),
                            ("tools.send_message_tool", legacy, "send_message_tool")):
        pkg = name.split(".")[0]
        if obj is None:
            monkeypatch.setitem(sys.modules, pkg, None)
            monkeypatch.setitem(sys.modules, name, None)
        else:
            monkeypatch.setitem(sys.modules, pkg, types.ModuleType(pkg))
            mod = types.ModuleType(name)
            setattr(mod, attr, obj)
            monkeypatch.setitem(sys.modules, name, mod)


def test_directory_api_present_returns_its_text_and_never_touches_the_legacy_tool(monkeypatch):
    legacy = Legacy(json.dumps({"targets": "SHOULD NOT BE USED"}))
    _install(monkeypatch, directory=lambda: DIRECTORY, legacy=legacy)
    out = list_targets()
    assert "#car" in out and "SHOULD NOT" not in out and "\n" not in out
    assert legacy.calls == []


def test_directory_text_is_the_no_platforms_message_unchanged(monkeypatch):
    _install(monkeypatch, directory=lambda: "No messaging platforms connected or no channels discovered.", legacy=None)
    assert list_targets() == "No messaging platforms connected or no channels discovered."


def test_old_hermes_without_channel_directory_uses_the_legacy_list(monkeypatch):
    legacy = Legacy(json.dumps({"targets": "telegram:\n  home"}))
    _install(monkeypatch, directory=None, legacy=legacy)
    assert list_targets() == "telegram: home"
    assert legacy.calls == [{"action": "list"}]


@pytest.mark.parametrize("result", [json.dumps({"error": "Unknown action 'list'. Use 'send'."}), "unknown action",
                                    json.dumps({"error": "removed; use `hermes send --list`"}), RuntimeError("boom")])
def test_post_removal_hermes_gives_none(monkeypatch, result):
    legacy = Legacy(result)
    _install(monkeypatch, directory=None, legacy=legacy)
    assert list_targets() is None
    assert all(c.get("action") != "send" for c in legacy.calls)


def test_directory_raising_falls_back_to_legacy(monkeypatch):
    def boom():
        raise OSError("directory unreadable")
    legacy = Legacy(json.dumps({"targets": "signal:+1555"}))
    _install(monkeypatch, directory=boom, legacy=legacy)
    assert list_targets() == "signal:+1555"
    assert legacy.calls == [{"action": "list"}]


@pytest.mark.parametrize("bad", [None, "", "   ", 5, ["x"], {"a": 1}])
def test_unusable_directory_result_falls_back_and_then_gives_none(monkeypatch, bad):
    legacy = Legacy(json.dumps({"error": "unknown action"}))
    _install(monkeypatch, directory=lambda: bad, legacy=legacy)
    assert list_targets() is None
    assert legacy.calls == [{"action": "list"}]


@pytest.mark.parametrize("result", ["not json", "", None, "[]", "null", "5", json.dumps(["telegram"]), json.dumps({"targets": ""}),
                                    json.dumps({"targets": None}), json.dumps({"targets": ["telegram"]}), json.dumps({})])
def test_malformed_or_non_dict_legacy_results_give_none(monkeypatch, result):
    _install(monkeypatch, directory=None, legacy=Legacy(result))
    assert list_targets() is None


def test_neither_api_available_gives_none(monkeypatch):
    _install(monkeypatch, directory=None, legacy=None)
    assert list_targets() is None


def test_long_listing_is_capped(monkeypatch):
    _install(monkeypatch, directory=lambda: "x" * 5000, legacy=None)
    assert len(list_targets()) == 1500


def test_discovery_never_sends_and_binds_no_target(service, monkeypatch):
    legacy = Legacy(json.dumps({"targets": "telegram:\n  home"}))
    _install(monkeypatch, directory=None, legacy=legacy)
    before = service.watcher.deliverer.target()
    assert "telegram" in slash.handle("notify list")
    assert before is None and service.watcher.deliverer.target() is None
    assert [c for c in legacy.calls if c.get("action") == "send"] == []
    assert legacy.calls == [{"action": "list"}]


def test_slash_notify_list_shows_the_directory(service, monkeypatch):
    _install(monkeypatch, directory=lambda: DIRECTORY, legacy=Legacy(RuntimeError("must not be called")))
    out = slash.handle("notify list")
    assert "Places Hermes can send to" in out and "discord: #car" in out
    assert service.watcher.deliverer.target() is None


def test_slash_notify_list_falls_back_to_examples_when_nothing_is_available(service, monkeypatch):
    _install(monkeypatch, directory=None, legacy=Legacy(json.dumps({"error": "unknown action"})))
    out = slash.handle("notify list")
    assert "Hermes gave no list of targets" in out and "matrix:!room:server" in out
    assert service.watcher.deliverer.target() is None


def _send_modules(monkeypatch, outbound=None, legacy=None):
    """Hermes' ``send_message_tool`` at the v0.23.2+ path (``gateway.outbound.send``) and/or the old ``tools`` path."""
    for name, obj in (("gateway.outbound.send", outbound), ("tools.send_message_tool", legacy)):
        parts = name.split(".")
        if obj is None:
            monkeypatch.setitem(sys.modules, name, None)
            continue
        for i in range(1, len(parts)):
            monkeypatch.setitem(sys.modules, ".".join(parts[:i]), types.ModuleType(".".join(parts[:i])))
        mod = types.ModuleType(name)
        mod.send_message_tool = obj
        monkeypatch.setitem(sys.modules, name, mod)


# hermes_send itself is replaced by conftest's fake sender in every test, so these check the resolver it uses.

def test_hermes_v0232_send_resolves_to_gateway_outbound_not_the_old_path(monkeypatch):
    from pit_crew.watcher.delivery import _send_message_tool
    new, old = Legacy(json.dumps({"success": True})), Legacy(json.dumps({"success": True}))
    _send_modules(monkeypatch, outbound=new, legacy=old)
    assert _send_message_tool() is new


def test_older_hermes_send_falls_back_to_tools_send_message_tool(monkeypatch):
    from pit_crew.watcher.delivery import _send_message_tool
    old = Legacy(json.dumps({"success": True}))
    _send_modules(monkeypatch, outbound=None, legacy=old)
    assert _send_message_tool() is old


def test_no_send_module_at_all_raises_so_hermes_send_reports_unavailable(monkeypatch):
    from pit_crew.watcher.delivery import _send_message_tool
    _send_modules(monkeypatch, outbound=None, legacy=None)
    with pytest.raises(ImportError):
        _send_message_tool()
