"""Run inside the Hermes venv: drive the plugin's REAL delivery code against Hermes' REAL send_message_tool (target
parsing, platform config, home channel, relay guard, secret scope, session context). Only the network sender
(``tools.send_message_tool._send_to_platform``) is replaced. Prints one JSON line. Invoked by test_delivery_hermes.py."""
import asyncio
import contextvars
import importlib.util
import json
import os
import sys
import threading

home, repo = sys.argv[1], sys.argv[2]
os.environ["HERMES_HOME"] = home
os.makedirs(home, exist_ok=True)
for k in list(os.environ):
    if k.endswith(("_BOT_TOKEN", "_HOME_CHANNEL", "_TOKEN", "_ACCESS_TOKEN")) or k.startswith(("MATRIX_", "SIGNAL_", "DISCORD_", "TELEGRAM_", "SLACK_")):
        del os.environ[k]
with open(os.path.join(home, "config.yaml"), "w") as fh:
    fh.write("{}\n")
with open(os.path.join(home, ".env"), "w") as fh:
    fh.write("")

spec = importlib.util.spec_from_file_location("pit_crew", os.path.join(repo, "__init__.py"), submodule_search_locations=[repo])
mod = importlib.util.module_from_spec(spec)
sys.modules["pit_crew"] = mod
spec.loader.exec_module(mod)
from pit_crew.watcher import delivery  # noqa: E402

import tools.send_message_tool as smt  # noqa: E402

calls = []
behaviour = {"result": None}


async def fake_send(platform, pconfig, chat_id, message, thread_id=None, media_files=None, **kw):
    calls.append({"platform": getattr(platform, "value", str(platform)), "chat_id": chat_id, "thread_id": thread_id,
                  "message": message, "token": getattr(pconfig, "token", None)})
    if behaviour["result"] is not None:
        return behaviour["result"]
    return {"success": True, "platform": getattr(platform, "value", str(platform)), "chat_id": chat_id, "message_id": "m1"}


smt._send_to_platform = fake_send
out = {}


def attempt(label, target, **env):
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    n = len(calls)
    try:
        ok, reason, kind = delivery.hermes_send(target, "hello from pit crew")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    out[label] = {"ok": ok, "reason": reason, "kind": kind, "calls": calls[n:]}


TOK = {"TELEGRAM_BOT_TOKEN": "123456:ABCDEF", "DISCORD_BOT_TOKEN": "discord-token", "SLACK_BOT_TOKEN": "xoxb-1",
       "MATRIX_ACCESS_TOKEN": "syt_x", "MATRIX_HOMESERVER": "https://matrix.example.org"}
attempt("telegram_chat", "telegram:-1001234567890", **TOK)
attempt("telegram_thread", "telegram:-1001234567890:17585", **TOK)
attempt("telegram_home", "telegram", TELEGRAM_HOME_CHANNEL="-1009999", **TOK)
attempt("telegram_no_home", "telegram", **TOK)
attempt("matrix_room", "matrix:!abcDEF:example.org", **TOK)
attempt("discord_channel_by_name", "discord:#car", **TOK)
attempt("discord_id", "discord:38472910562837", **TOK)
attempt("signal_number", "signal:+15551234567", SIGNAL_HTTP_URL="http://127.0.0.1:8080", SIGNAL_ACCOUNT="+15550000000")
attempt("unknown_platform", "nosuchplatform:abc", **TOK)
attempt("telegram_unconfigured", "telegram:-100123")
behaviour["result"] = {"error": "Telegram API error: Forbidden: bot was blocked by the user"}
attempt("platform_error", "telegram:-100123", **TOK)
behaviour["result"] = None

# secret scope: platform token only in the profile secret scope, call made on a worker thread carrying a copy of the context
from agent.secret_scope import reset_secret_scope, set_secret_scope  # noqa: E402

tok = set_secret_scope({"TELEGRAM_BOT_TOKEN": "scoped-token-9"}, profile_home=home)
res = {}


def worker():
    n = len(calls)
    res["r"] = delivery.hermes_send("telegram:-100777", "scoped")
    res["calls"] = calls[n:]


ctx = contextvars.copy_context()
t = threading.Thread(target=ctx.run, args=(worker,))
t.start(); t.join(20)
out["scope_thread_with_copied_context"] = {"ok": res["r"][0], "reason": res["r"][1], "tokens": [c["token"] for c in res["calls"]]}
res2 = {}


def bare_worker():
    n = len(calls)
    res2["r"] = delivery.hermes_send("telegram:-100777", "bare thread")
    res2["calls"] = calls[n:]


t = threading.Thread(target=bare_worker)             # a thread that did NOT copy the context: no scope there
t.start(); t.join(20)
out["scope_thread_without_context"] = {"ok": res2["r"][0], "reason": res2["r"][1], "kind": res2["r"][2], "calls": len(res2["calls"])}

async def via_to_thread():            # how Watcher.run polls and delivers: asyncio.to_thread copies the running context
    n = len(calls)
    r = await asyncio.to_thread(delivery.hermes_send, "telegram:-100888", "via to_thread")
    return r, calls[n:]


r3, c3 = asyncio.run(via_to_thread())
out["scope_via_asyncio_to_thread"] = {"ok": r3[0], "tokens": [c["token"] for c in c3]}
reset_secret_scope(tok)

# session context -> delivery target, read by Hermes' own reader
from gateway.session_context import clear_session_vars, set_session_vars  # noqa: E402

targets = {}
for plat, chat, thread in [("telegram", "-1001234567890", "17585"), ("matrix", "!abcDEF:example.org", ""),
                           ("discord", "38472910562837", ""), ("signal", "+15551234567", ""), ("slack", "C0123ABCD", "")]:
    tokens = set_session_vars(platform=plat, chat_id=chat, thread_id=thread, session_key=f"agent:main:{plat}:x")
    try:
        targets[plat] = delivery.target_from_session(plat)
    finally:
        clear_session_vars(tokens)
tokens = set_session_vars(platform="tui", source="tui")
targets["tui"] = delivery.target_from_session("tui")
clear_session_vars(tokens)
out["session_targets"] = targets
out["registered"] = {"send_message_in_registry": __import__("tools.registry", fromlist=["registry"]).registry.get_entry("send_message") is not None}
out["list"] = delivery.list_targets()
print("PROBE_JSON:" + json.dumps(out, default=str))
