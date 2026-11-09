import asyncio
import threading
import json

import pytest

from conftest import CronCtx
from pit_crew import slash
from pit_crew.adapters.base import AdapterUnavailable
from pit_crew.watcher.watcher import Watcher, start_watcher, reset_started_for_tests


@pytest.fixture
def svc(stub_service, stub):
    return stub_service(armed=False)


def trigger_alert(stub, clock, svc):
    stub.values.update(locked=False, location_zone="away")
    svc.watcher.poll_once(); clock.advance(11 * 60)
    return svc.watcher.poll_once()


def test_poll_interval_backs_off_when_asleep_and_on_errors(svc, stub):
    w = svc.watcher
    assert w.next_interval({}) == 120
    assert w.next_interval({"asleep": True}) == 900
    w._errors = 3
    assert w.next_interval({}) == 960 or w.next_interval({}) == 900  # exponential, capped at 15 min
    w._errors = 0


def test_error_polls_back_off_and_dont_crash(svc, stub):
    def boom(now):
        raise AdapterUnavailable("cannot reach Home Assistant")
    stub.fetch_state = boom
    res = svc.watcher.poll_once()
    assert res["polled"] is False and "cannot reach" in res["error"]
    assert svc.watcher.next_interval(res) > 120


def test_polling_only_reads_never_commands(svc, stub, clock):
    for _ in range(3):
        svc.watcher.poll_once(); clock.advance(300)
    assert stub.sent == []


def test_disabled_watcher_does_nothing(stub_service, stub):
    s = stub_service(watcher_enabled=False)
    assert s.watcher.poll_once()["skipped"] == "watcher disabled"
    assert s.watcher.poll_once(force=True)["polled"] is True      # but check_now still works


def test_lease_ensures_single_active_watcher(svc, stub, clock):
    other = Watcher(svc)
    assert svc.watcher.poll_once()["polled"] is True
    res = other.poll_once()
    assert res["polled"] is False and "lease" in res["skipped"]
    clock.advance(10 * 60)                         # lease expires -> takeover
    assert other.poll_once()["polled"] is True
    other.release()


def test_push_goes_through_hermes_send_to_the_target_as_a_plain_message(svc, stub, clock, sender):
    d = svc.watcher.deliverer
    d.bind("telegram:-100123")
    res = trigger_alert(stub, clock, svc)
    assert res["pushed"] is True
    text, target = sender.sent[-1]
    assert target == "telegram:-100123" and text.startswith("Pit Crew car alert")
    assert "unlocked and away" in text and "nothing is sent to the car until you say yes" in text
    assert "not typed by the user" not in text and "Tell the user" not in text      # a message for a person, not a prompt for an agent
    assert svc.alerts(("new",)) == []              # surfaced, so the next-turn hook won't repeat it
    assert svc.alerts(("surfaced",))
    last = svc.store.kv_get("notify_last")
    assert last["ok"] is True and last["delivered"] is True and last["target"] == "telegram:-100123"


def test_push_refused_by_the_platform_is_reported_and_alert_stays_new(svc, stub, clock, sender):
    sender.fail("Telegram API error: chat not found", "refused")
    svc.watcher.deliverer.bind("telegram:-100123")
    res = trigger_alert(stub, clock, svc)
    assert res["pushed"] is False
    assert svc.alerts(("new",))
    st = svc.watcher.deliverer.status()
    assert st["last_push"]["ok"] is False and "chat not found" in st["last_push"]["hint"]
    assert "/car notify list" in st["last_push"]["hint"]


def test_no_target_means_no_push_but_next_turn_context(svc, stub, clock, sender):
    res = trigger_alert(stub, clock, svc)
    assert res["pushed"] is False and sender.sent == []
    text = svc.watcher.deliverer.context_for_next_turn()
    assert "unlocked and away" in text and "don't run it without a yes" in text
    assert svc.watcher.deliverer.context_for_next_turn() is None      # only once


def test_info_alerts_are_not_pushed(svc, stub, clock, sender):
    svc.watcher.deliverer.bind("telegram")
    stub.values["software_update_available"] = True
    res = svc.watcher.poll_once()
    assert res["raised"] and res["pushed"] is False and sender.sent == []


def test_push_disabled_by_setting(stub_service, stub, clock, sender):
    s = stub_service(alert_push=False)
    s.watcher.deliverer.bind("telegram")
    assert trigger_alert(stub, clock, s)["pushed"] is False and sender.sent == []


def test_the_alert_target_setting_wins_over_the_command_binding(stub_service, stub, clock, sender):
    s = stub_service(alert_target="discord:#car")
    s.watcher.deliverer.bind("telegram")
    assert s.watcher.deliverer.target() == {"target": "discord:#car", "source": "setting"}
    assert trigger_alert(stub, clock, s)["pushed"] is True
    assert sender.sent[-1][1] == "discord:#car"
    assert "alert_target setting" in slash.handle("notify status")
    assert "wins over this" in slash.handle("notify matrix:!room:example.org")


def test_setting_alone_is_enough_and_off_does_not_clear_it(stub_service, stub, clock, sender):
    s = stub_service(alert_target="signal:+15551234567")
    assert trigger_alert(stub, clock, s)["pushed"] is True
    assert "still set" in slash.handle("notify off")
    assert s.watcher.deliverer.target()["target"] == "signal:+15551234567"


def test_bad_targets_are_refused(svc):
    assert svc.watcher.deliverer.bind("") is None and svc.watcher.deliverer.bind("not a target") is None
    assert svc.watcher.deliverer.bind("Telegram") is None      # the command lower-cases first; bind itself is strict
    out = slash.handle("notify not a target")
    assert "doesn't look like a delivery target" in out and svc.watcher.deliverer.target() is None


def test_an_old_session_key_binding_is_not_reused_and_status_says_how_to_fix_it(svc, stub, clock, sender):
    svc.store.kv_set("notify_target", {"session_key": "agent:main:matrix:group:!r:s", "platform": "matrix", "bound_at": 1.0})
    assert svc.watcher.deliverer.target() is None and svc.watcher.deliverer.legacy_binding()
    assert trigger_alert(stub, clock, svc)["pushed"] is False and sender.sent == []
    assert "run `/car notify here` again" in slash.handle("notify status")


def test_push_text_is_scrubbed(svc, stub, clock, sender):
    svc.watcher.deliverer.bind("telegram")
    stub.values.update(locked=False, location_zone="5YJ3E1EA7KF317000")  # absurd zone name that looks like a VIN
    svc.watcher.poll_once(); clock.advance(11 * 60); svc.watcher.poll_once()
    assert "5YJ3E1EA7KF317000" not in sender.sent[-1][0]


def test_the_send_runs_in_the_secret_scope_the_poll_ran_in(svc, stub, clock, sender, monkeypatch):
    """Watcher.run polls AND delivers in one asyncio.to_thread call, which copies the running context: whatever secret scope the
    host gave the watcher task (HA token and platform token alike) is what send_message sees."""
    import contextvars
    scope = contextvars.ContextVar("fake_secret_scope", default=None)
    seen = []

    def spy(target, text):
        seen.append(scope.get())
        return sender(target, text)
    monkeypatch.setattr("pit_crew.watcher.delivery.hermes_send", spy)
    svc.watcher.deliverer.bind("telegram")
    stub.values.update(locked=False, location_zone="away")
    svc.watcher.poll_once(); clock.advance(11 * 60)

    async def go():
        scope.set("owner-profile")
        return await asyncio.to_thread(svc.watcher.poll_once)
    res = asyncio.run(go())
    assert res["pushed"] is True and seen == ["owner-profile"]


def test_async_run_loop_polls_and_stops(svc, stub, clock):
    async def go():
        stop = asyncio.Event()
        w = svc.watcher
        task = asyncio.create_task(w.run(stop, initial_delay_s=0.0))
        await asyncio.sleep(0.3)
        assert w.last_poll is not None
        stop.set()
        await asyncio.wait_for(task, 5)
    asyncio.run(go())


class SpawnCtx:
    def __init__(self, behaviour="ok"):
        self.behaviour, self.spawned, self.unloads = behaviour, [], []

    def spawn_task(self, coro, name=None):
        if self.behaviour == "no_loop":
            raise RuntimeError("no running event loop")
        if self.behaviour == "boom":
            raise TypeError("odd")
        self.spawned.append((coro, name))

    def on_unload(self, cb):
        self.unloads.append(cb)


def test_spawn_task_is_used_even_without_a_running_loop(svc):
    reset_started_for_tests()
    ctx = SpawnCtx("ok")                                   # new Hermes: works with no loop at register() time
    assert start_watcher(ctx, lambda: svc, initial_delay_s=0.0) == "task"
    coro, name = ctx.spawned[0]
    assert name == "pit-crew:watcher" and len(ctx.unloads) == 1
    coro.close()
    assert start_watcher(SpawnCtx(), lambda: svc) == "already"       # once-per-process guard
    reset_started_for_tests()


def test_spawn_task_stop_event_ends_the_watcher_on_whatever_loop_runs_it(svc):
    reset_started_for_tests()
    ctx = SpawnCtx("ok")
    start_watcher(ctx, lambda: svc, initial_delay_s=30.0)
    coro, _ = ctx.spawned[0]
    result = {}

    def host_loop():                                       # a Hermes-owned loop on a different thread
        asyncio.run(asyncio.wait_for(coro, 10))
        result["done"] = True

    t = threading.Thread(target=host_loop)
    t.start()
    ctx.unloads[0]()                                       # on_unload from yet another thread
    t.join(8)
    assert result.get("done") is True
    reset_started_for_tests()


@pytest.mark.parametrize("behaviour", ["no_loop", "boom"])
def test_runtimeerror_or_failure_falls_back_to_own_thread(svc, behaviour):
    reset_started_for_tests()
    ctx = SpawnCtx(behaviour)
    assert start_watcher(ctx, lambda: svc, initial_delay_s=30.0) == "thread"
    assert ctx.spawned == [] and len(ctx.unloads) == 1
    ctx.unloads[0]()
    reset_started_for_tests()


def test_missing_spawn_task_falls_back_to_own_thread(svc):
    reset_started_for_tests()
    unloads = []

    class Old:
        def on_unload(self, cb):
            unloads.append(cb)

    assert start_watcher(Old(), lambda: svc, initial_delay_s=30.0) == "thread"
    unloads[0]()
    reset_started_for_tests()


# -- /car slash command ----------------------------------------------------------------------------
def test_slash_status_card(service):
    out = slash.handle("")
    assert "Battery" in out and "Practice" in out and "Alerts: none" in out


def test_slash_arm_asks_choice_then_arms_and_disarm(service):
    asked = []
    service.ctx = type("C", (), {"ask_choice": lambda self, q, ch: (asked.append(q), "yes")[1],
                                 "current_cron_execution": lambda self: None})()
    out = slash.handle("arm 2")
    assert "Live for 2 h" in out and asked and "SIMULATED" in asked[0]
    assert service.armed_info()["armed"]
    assert "Live until" in slash.handle("")
    assert "Back to practice mode" in slash.handle("practice") and not service.armed_info()["armed"]


def test_slash_arm_cancel_and_no_prompt_fallback(service):
    service.ctx = type("C", (), {"ask_choice": lambda self, q, ch: "no", "current_cron_execution": lambda self: None})()
    assert "Cancelled" in slash.handle("arm") and not service.armed_info()["armed"]
    service.ctx = type("C", (), {"ask_choice": lambda self, q, ch: None, "current_cron_execution": lambda self: None})()
    out = slash.handle("arm 3")
    assert "/car live 3 confirm" in out and not service.armed_info()["armed"]
    assert "Live for 3 h" in slash.handle("live 3 confirm") and service.armed_info()["armed"]


def test_slash_log_alerts_rules_help_and_errors(service):
    service.arm(1, by="t")
    service.run_command("set_temp", {"temp_f": 70}, surface="tool")
    log = slash.handle("log 5")
    assert "set_temp(temp_f=70.0)" in log and "car confirmed" in log and "went live" in log
    assert slash.handle("alerts") == "No open car alerts."
    assert "Left unlocked away from home" in slash.handle("rules")
    assert "/car live" in slash.handle("help") and "Unknown subcommand" in slash.handle("bogus")


def test_slash_sim_controls_only_for_simulator(service, stub_service, stub):
    assert "Simulator: fast-forwarded 90 minutes" in slash.handle("sim advance 90")
    assert "Simulator: drove 20 mi to away" in slash.handle("sim drive 20 away")
    assert "Bad arguments" in slash.handle("sim advance abc")
    s = stub_service()
    assert "only works with the simulated car" in slash.handle("sim advance 5")


def _session_vars(monkeypatch, platform, chat_id, thread_id=""):
    """What Hermes binds while a plugin command runs (HERMES_SESSION_* context variables). Real Hermes' reader is covered in
    test_delivery_hermes.py."""
    env = {"HERMES_SESSION_PLATFORM": platform, "HERMES_SESSION_CHAT_ID": chat_id, "HERMES_SESSION_THREAD_ID": thread_id}
    monkeypatch.setattr("pit_crew.watcher.delivery.session_env", lambda name: env.get(name, ""))


@pytest.mark.parametrize("platform,chat,thread,expected", [
    ("telegram", "-1001234567890", "", "telegram:-1001234567890"),
    ("telegram", "-1001234567890", "17585", "telegram:-1001234567890:17585"),
    ("discord", "38472910562837", "", "discord:38472910562837"),
    ("matrix", "!abcDEF:example.org", "", "matrix:!abcDEF:example.org"),
    ("matrix", "!abcDEF:example.org", "$ev1", "matrix:!abcDEF:example.org"),     # Matrix room ids contain ':'; no thread suffix
    ("signal", "+15551234567", "", "signal:+15551234567"),
    ("slack", "C0123ABCD", "1700.1", "slack:C0123ABCD:1700.1"),
])
def test_notify_here_derives_a_delivery_target_from_each_platforms_session_context(service, monkeypatch, platform, chat, thread, expected):
    _session_vars(monkeypatch, platform, chat, thread)
    out = slash.handle("notify here", session_key="agent:main:x", platform=platform)
    assert f"`{expected}`" in out and "no agent turn" in out
    assert service.watcher.deliverer.target()["target"] == expected
    assert platform in slash.handle("notify status")


def test_notify_here_from_a_desktop_or_cli_session_binds_nothing_and_says_so(service, monkeypatch):
    monkeypatch.setattr("pit_crew.watcher.delivery.session_env", lambda name: "")
    for plat in ("cli", "tui", "", "desktop"):
        out = slash.handle("notify here", session_key="s", platform=plat)
        assert "Desktop/CLI session" in out and "/car notify telegram" in out
    assert service.watcher.deliverer.target() is None


def test_notify_here_in_a_messaging_chat_without_a_chat_id_asks_for_an_explicit_target(service, monkeypatch):
    _session_vars(monkeypatch, "telegram", "")
    out = slash.handle("notify here", session_key="k", platform="telegram")
    assert "did not tell me which telegram chat" in out and service.watcher.deliverer.target() is None


@pytest.mark.parametrize("target", ["telegram", "discord:#car", "matrix:!room:example.org", "signal:+15551234567", "slack:#ops"])
def test_notify_explicit_target_for_each_platform(service, target):
    out = slash.handle(f"notify {target}")
    assert f"`{target}`" in out and service.watcher.deliverer.target()["target"] == target
    assert target in slash.handle("notify status")
    assert "no longer be pushed" in slash.handle("notify off")
    assert "No place is set" in slash.handle("notify")


def test_notify_list_shows_hermes_targets_or_examples(service, monkeypatch):
    monkeypatch.setattr("pit_crew.slash.list_targets", lambda: "telegram:\n  home\ndiscord:\n  #car")
    assert "#car" in slash.handle("notify list")
    monkeypatch.setattr("pit_crew.slash.list_targets", lambda: None)
    assert "matrix:!room:server" in slash.handle("notify list")


def test_slash_never_raises(service, monkeypatch):
    monkeypatch.setattr("pit_crew.slash._status_card", lambda svc: 1 / 0)
    assert "internal error" in slash.handle("")


def test_next_turn_context_hook_skips_cron_runs_so_alerts_are_not_swallowed(svc, stub, clock):
    import pit_crew
    trigger_alert(stub, clock, svc)
    svc.ctx.cron = True
    assert pit_crew._alert_context_hook(platform="cron") is None
    assert pit_crew._alert_context_hook(platform="telegram") is None       # unattended via ctx too
    assert svc.alerts(("new",))                                              # still waiting for a human turn
    svc.ctx.cron = False
    out = pit_crew._alert_context_hook(platform="telegram")
    assert out and "unlocked and away" in out["context"]
    assert pit_crew._alert_context_hook(platform="telegram") is None       # once


def test_next_turn_context_hook_skips_subagent_turns_so_the_user_still_sees_the_alert(svc, stub, clock):
    import pit_crew
    trigger_alert(stub, clock, svc)
    assert pit_crew._alert_context_hook(platform="telegram", parent_session_id="parent-1", session_id="child") is None
    assert svc.alerts(("new",))                                              # not consumed by the child turn
    out = pit_crew._alert_context_hook(platform="telegram", parent_session_id=None, session_id="main")
    assert out and "unlocked and away" in out["context"]


def test_simulated_car_alerts_are_labelled(service, clock):
    service.adapter.control("event", kind="software_update")
    service.adapter.control("leave_unlocked")
    service.adapter.control("drive", miles=3, to="away")
    service.watcher.poll_once(force=True)
    service.adapter.control("advance", minutes=12)
    service.watcher.poll_once(force=True)
    titles = [a["title"] for a in service.alerts()]
    assert titles and all(t.startswith("[Simulated] ") for t in titles)


def test_notify_test_sends_a_labelled_message_without_creating_alerts(svc, stub, clock, sender):
    d = svc.watcher.deliverer
    d.bind("telegram:-100123")
    before_last = svc.store.kv_get("notify_last")
    out = slash.handle("notify test")
    assert out.startswith("Hermes delivered the test message to `telegram:-100123`") and "platform accepted it" in out
    text, target = sender.sent[-1]
    assert target == "telegram:-100123" and "Pit Crew test alert: delivery works" in text
    assert svc.alerts(("new", "surfaced", "acked", "resolved")) == []
    assert svc.store.kv_get("notify_last") == before_last          # status/dedupe state untouched


def test_notify_test_reports_the_platforms_answer_on_failure(svc, sender):
    sender.fail("Telegram API error: Bad Request: chat not found", "refused")
    svc.watcher.deliverer.bind("telegram:-1")
    out = slash.handle("notify test")
    assert out.startswith("Test alert FAILED") and "chat not found" in out
    assert svc.alerts(("new",)) == []


def test_notify_test_without_a_target_or_when_send_is_unavailable(svc, sender):
    assert "No place is set" in slash.handle("notify test") and sender.sent == []
    svc.watcher.deliverer.bind("telegram")
    sender.fail("send_message is not available (ImportError)", "unavailable")
    out = slash.handle("notify test")
    assert out.startswith("Test alert FAILED") and "cross-platform send" in out


def test_notify_test_does_not_touch_rules_or_dedupe(svc, stub, clock):
    svc.watcher.deliverer.bind("telegram")
    slash.handle("notify test")
    res = trigger_alert(stub, clock, svc)           # a real alert afterwards is still raised and pushed
    assert res["pushed"] is True and len(svc.alerts(("surfaced",))) == 1
