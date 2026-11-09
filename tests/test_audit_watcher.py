"""G8 audit regressions: readiness, watcher rules, alert delivery, watcher lifecycle, history recorder, time parsing."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from pit_crew.brain.history import summarize_charges
from pit_crew.brain.timeparse import WhenError, parse_when
from pit_crew.adapters.base import ChargeRecord
from pit_crew.watcher import watcher as watcher_mod


def poll(svc):
    return svc.watcher.poll_once()


@pytest.fixture
def svc(stub_service, stub):
    return stub_service(armed=False, watcher_enabled=True, history="own", battery_kwh=75.0)


def tick(svc, clock, seconds=120, **vals):
    clock.advance(seconds)
    svc.adapter.values.update(vals)
    return svc.get_state(force=True)


# -- readiness ---------------------------------------------------------------------------------------------------------
def test_a_trip_longer_than_a_full_battery_is_not_ready_even_at_100_percent(svc, stub):
    stub.values.update(battery_pct=100.0, charge_limit_pct=100, plugged_in=True, charging_state="complete")
    res = svc.readiness("tomorrow 7:00 AM", 600.0)
    assert res["ok"] and res["verdict"] == "not_ready"             # used to be 'tight': 100% >= min(100, 216%)
    assert res["needed_pct"] > 100 and res["margin_pct"] < 0
    assert "charging stop" in " ".join(res["issues"]) and res["recommendations"][0]["action"] == "plan_charging_stop"
    assert res["summary"].startswith("NOT ready") and "216" in res["summary"] or "%" in res["summary"]


def test_a_normal_trip_is_unchanged(svc, stub):
    stub.values.update(battery_pct=90.0, charge_limit_pct=90, plugged_in=True, charging_state="complete")
    assert svc.readiness("tomorrow 7:00 AM", 30.0)["verdict"] == "ready"


def test_the_not_enough_charge_alert_fires_for_the_impossible_trip(svc, stub, clock):
    stub.values.update(battery_pct=100.0, charge_limit_pct=100, plugged_in=True, charging_state="complete")
    svc.watcher.engine.set_rule("soc_below_tomorrow", trip_mi=600, within_hours=48)
    raised = poll(svc)["raised"]
    assert [a["rule"] for a in raised] == ["soc_below_tomorrow"] and "Plan a charging stop" in raised[0]["suggestion"]


# -- rules while driving -----------------------------------------------------------------------------------------------
def test_unlocked_and_sentry_alerts_are_for_a_parked_car_not_a_moving_one(svc, stub, clock):
    stub.values.update(locked=False, sentry_on=False, location_zone="away", moving=True, speed_mph=60.0, shift_state="D")
    for _ in range(4):
        assert poll(svc)["raised"] == []
        clock.advance(6 * 60)
    stub.values.update(moving=False, speed_mph=0.0, shift_state="P")           # now parked and still unlocked / Sentry off
    poll(svc); clock.advance(11 * 60)
    assert {a["rule"] for a in poll(svc)["raised"]} == {"left_unlocked_away", "sentry_off_away"}


# -- alert delivery ------------------------------------------------------------------------------------------------------
def _alert(svc, state="new"):
    aid = svc.store.alert_add(ts=1.0, rule="r", dedupe_key="k", severity="low", title="t", body="b", suggestion="s")
    if state != "new":
        svc.store.alert_set_state(aid, state)
    return aid


def test_a_push_cannot_resurrect_an_alert_that_was_resolved_or_acked_while_it_was_in_flight(svc):
    a = _alert(svc)
    assert svc.store.alert_claim_push(a, 1000.0)                       # new -> pushing
    svc.store.alerts_resolve_key("r", "k")                             # the condition cleared mid-push
    assert svc.store.alert_transition(a, "surfaced", ("pushing",)) is False
    assert svc.store.alerts_list(("resolved",))[0]["id"] == a
    b = _alert(svc)
    assert svc.store.alert_claim_push(b, 1000.0)
    svc.store.alert_set_state(b, "acked")                              # the human acknowledged it mid-push
    assert svc.store.alert_transition(b, "surfaced", ("pushing",)) is False
    assert [r["id"] for r in svc.store.alerts_list(("acked",))] == [b]
    c = _alert(svc)
    assert svc.store.alert_claim_push(c, 1000.0)
    assert svc.store.alert_transition(c, "surfaced", ("pushing",)) is True


def test_next_turn_context_does_not_overwrite_a_changed_alert(svc):
    a = _alert(svc)
    svc.store.alert_set_state(a, "acked")
    assert svc.watcher.deliverer.context_for_next_turn() is None
    assert svc.store.alerts_list(("acked",))[0]["id"] == a


# -- watcher lifecycle ----------------------------------------------------------------------------------------------------
def test_watcher_can_be_started_again_after_unload():
    class Ctx:
        def __init__(self):
            self.unloads = []

        def spawn_task(self, coro, name=""):
            coro.close()

        def on_unload(self, fn):
            self.unloads.append(fn)

    watcher_mod.reset_started_for_tests()
    ctx = Ctx()
    assert watcher_mod.start_watcher(ctx, lambda: None) == "task"
    assert watcher_mod.start_watcher(ctx, lambda: None) == "already"
    for fn in ctx.unloads:                                             # plugin unload (reload / update in one process)
        fn()
    assert watcher_mod.start_watcher(ctx, lambda: None) == "task"      # used to stay 'already' with nothing running
    watcher_mod.reset_started_for_tests()


# -- history recorder ------------------------------------------------------------------------------------------------------
def test_sparse_polling_cannot_inflate_charge_energy(svc, stub, clock):
    stub.values.update(battery_pct=40.0, plugged_in=True, charging_state="charging", charge_power_kw=7.0, location_zone="home")
    svc.get_state(force=True)
    for i in range(3):
        # charging stops early inside a 5-hour gap: the battery went 40% -> 42% (1.5 kWh) but 3.5 kW avg x 5 h says 17.5 kWh
        tick(svc, clock, 5 * 3600, battery_pct=42.0, charging_state="complete", charge_power_kw=0.0)
        tick(svc, clock, 3600)
        tick(svc, clock, 3600, plugged_in=False, charging_state="disconnected", battery_pct=40.0)      # unplugged, drove
        tick(svc, clock, 600, plugged_in=True, charging_state="charging", charge_power_kw=7.0)
    tick(svc, clock, 5 * 3600, battery_pct=42.0, charging_state="complete", charge_power_kw=0.0)
    tick(svc, clock, 3600)
    tick(svc, clock, 3600)
    charges = [c for c in svc.history_provider.charges(0, 1e12) if c.kwh_used]
    assert len(charges) >= 3
    for c in charges:
        assert c.kwh_used <= 5.0, c                       # was 17.5+ kWh "used" for 2 points of battery (~1.5 kWh)


def test_implausible_pack_size_estimates_are_ignored(svc):
    # three finished sessions that imply a 150+ kWh pack (bad energy integration) must not become the readiness pack size
    for i in range(3):
        svc.store.execute(
            "INSERT INTO charges(source, start_ts, end_ts, start_pct, end_pct, kwh_used, temp_sum, temp_n, open) "
            "VALUES('recorded',?,?,?,?,?,0,0,0)", (1000.0 + i * 1e5, 2000.0 + i * 1e5, 20.0, 50.0, 60.0))
    assert svc.history_provider.battery_health() is None


def test_an_odometer_glitch_is_not_a_600_mile_trip(svc, stub, clock):
    stub.values.update(odometer_mi=1000.0, battery_pct=80.0, range_mi=250.0, location_zone="home", moving=False)
    svc.get_state(force=True)
    tick(svc, clock, 3600, odometer_mi=1600.0, moving=True, speed_mph=30.0, location_zone="away")
    tick(svc, clock, 3600, moving=False, speed_mph=0.0)
    tick(svc, clock, 3600)
    for d in svc.history_provider.drives(0, 1e12):
        assert d.miles < 50, d


def test_a_spot_speed_does_not_become_a_150_mile_drive_over_a_five_hour_gap(svc, stub, clock):
    for k in ("odometer_mi",):
        stub.values.pop(k, None)
    stub.values.update(battery_pct=80.0, range_mi=250.0, moving=True, speed_mph=60.0, location_zone="home")
    svc.get_state(force=True)
    tick(svc, clock, 5 * 3600, moving=False, speed_mph=0.0, location_zone="away")
    tick(svc, clock, 3600)
    for d in svc.history_provider.drives(0, 1e12):
        assert d.miles <= 10.5, d


def test_a_short_stop_does_not_split_a_drive_in_two(svc, stub, clock):
    stub.values.update(odometer_mi=2000.0, battery_pct=80.0, range_mi=250.0, moving=True, speed_mph=40.0, location_zone="home")
    svc.get_state(force=True)
    tick(svc, clock, 120, odometer_mi=2001.5, location_zone="away")
    tick(svc, clock, 120, odometer_mi=2003.0)
    tick(svc, clock, 300, moving=False, speed_mph=0.0)                  # stopped (the reading still follows movement)
    tick(svc, clock, 300)                                               # ...and 5 more minutes parked: an idle interval
    tick(svc, clock, 120, odometer_mi=2004.5, moving=True, speed_mph=40.0)
    tick(svc, clock, 120, odometer_mi=2006.0)
    tick(svc, clock, 300, moving=False, speed_mph=0.0)
    tick(svc, clock, 3600)
    tick(svc, clock, 3600)
    drives = svc.history_provider.drives(0, 1e12)
    assert len(drives) == 1 and drives[0].miles == pytest.approx(6.0, abs=0.1)


def test_charging_efficiency_compares_like_with_like():
    mk = lambda added, used: ChargeRecord(start=0, end=1, kwh_added=added, kwh_used=used, cost=None, start_pct=10, end_pct=50,
                                          duration_min=60, outside_temp_f=60)
    out = summarize_charges([mk(10.0, 11.0), mk(30.0, None)])         # the second has no wall-energy figure
    assert out["charging_efficiency_pct"] == pytest.approx(91, abs=1)  # was (10+30)/11 = 364%


# -- time parsing ------------------------------------------------------------------------------------------------------------
def test_relative_times_are_relative_not_a_clock_time_tomorrow():
    now = datetime(2026, 11, 9, 15, 20, 30, tzinfo=ZoneInfo("America/Chicago"))
    assert parse_when("in 2 hours", now) == datetime(2026, 11, 9, 17, 21, tzinfo=ZoneInfo("America/Chicago"))
    assert parse_when("in 90 min", now).strftime("%H:%M") == "16:51"
    assert parse_when("in 3 days", now).day == 12
    for bad in ("in an hour", "2 hours from now", "in 0 hours", "in 400 days"):
        with pytest.raises(WhenError):
            parse_when(bad, now)
    assert parse_when("tomorrow 7:30 AM", now).strftime("%d %H:%M") == "10 07:30"        # unchanged


def test_history_dates_use_the_configured_time_zone(make_service, clock):
    clock.t = 1_790_000_000.0                                   # 2026-09-21 13:33 UTC = 2026-09-22 01:33 in Auckland
    auckland = make_service({"timezone": "Pacific/Auckland"})
    chicago = make_service({"timezone": "America/Chicago"})
    a = auckland.history("summary", days=7)
    c = chicago.history("summary", days=7)
    assert a["window"]["to"] == "2026-09-22" and c["window"]["to"] == "2026-09-21"
