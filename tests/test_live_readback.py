"""2026-11-09 real-car test: read-back must be honest about what HA has (and has not) re-reported."""

from datetime import datetime, timezone

import pytest

from fakes import FakeHA
from pit_crew.adapters.homeassistant import parse_ha_time
from pit_crew.settings import DEFAULTS

NOW = parse_ha_time("2025-10-05T00:17:00+00:00")
CHARGING = "sensor.demo_car_charging"
POWER = "sensor.demo_car_charger_power"
CURRENT = "sensor.demo_car_charger_current"
SWITCH = "switch.demo_car_charge"


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def stamp(ha, eid, ts):
    ha.states[eid]["last_reported"] = ha.states[eid]["last_updated"] = ha.states[eid]["last_changed"] = iso(ts)


@pytest.fixture
def ha():
    fake = FakeHA()
    for eid in fake.states:
        stamp(fake, eid, NOW - 5)                            # the integration just polled everything
    fake.set_state("binary_sensor.demo_car_charge_cable", "on")
    fake.set_state("binary_sensor.demo_car_status", "on")
    fake.now_fn = lambda: NOW
    yield fake
    fake.stop()


def svc_for(make_service, ha, clock):
    clock.t = NOW
    ha.now_fn = lambda: clock.t
    svc = make_service({"adapter": "homeassistant", "history": "none", "ha_url": ha.url, "ha_slug": "demo_car"},
                       secrets={"PIT_CREW_HA_TOKEN": FakeHA.TOKEN})
    svc.arm(1, by="t")
    return svc


# -- charging: HA's switch is optimistic, the sensor is the car's word --------------------------------------------------
def test_charge_stop_with_a_stale_charging_sensor_is_never_confirmed_from_the_optimistic_switch(make_service, ha, clock):
    ha.set_state(CHARGING, "charging"); stamp(ha, CHARGING, NOW - 2160)      # the car did stop, but HA has not re-read these
    ha.set_state(POWER, "1"); stamp(ha, POWER, NOW - 2160)
    ha.set_state(SWITCH, "on")
    svc = svc_for(make_service, ha, clock)

    def only_the_optimistic_switch(service, data):            # what HA does on the command: the switch flips, nothing else
        ha.set_state(SWITCH, "off"); stamp(ha, SWITCH, clock.t)
    ha._effect = only_the_optimistic_switch
    res = svc.run_command("charge_stop", {}, surface="test")
    assert res["status"] == "sent_unconfirmed" and res["readback"]["status"] == "not_confirmed"
    assert clock.t - NOW >= 90                                # it looked for the Fleet API lag, not just 20 s
    assert "NOT confirmed" in res["message"]


def test_charge_stop_is_confirmed_when_the_car_reports_it_within_the_lag_window(make_service, ha, clock):
    ha.apply_effects = False
    ha.set_state(CHARGING, "charging"); stamp(ha, CHARGING, NOW - 5)
    svc = svc_for(make_service, ha, clock)
    real_sleep = svc._sleep

    def sleep(sec):
        real_sleep(sec)
        if clock.t - NOW >= 40 and ha.states[CHARGING]["state"] == "charging":     # Fleet API catches up ~40 s later
            ha.set_state(CHARGING, "stopped"); stamp(ha, CHARGING, clock.t)
    svc._sleep = sleep
    res = svc.run_command("charge_stop", {}, surface="test")
    assert res["status"] == "confirmed" and res["readback"]["status"] == "confirmed"
    assert 40 <= clock.t - NOW < 90


def test_charge_start_does_not_trust_a_stale_charging_reading(make_service, ha, clock):
    """HA still says 'charging' from 36 minutes ago (the real finding): that is neither 'already charging' nor a confirmation."""
    ha.apply_effects = False
    ha.set_state(CHARGING, "charging"); stamp(ha, CHARGING, NOW - 2160)
    svc = svc_for(make_service, ha, clock)
    res = svc.run_command("charge_start", {}, surface="test")
    assert res["status"] == "sent_unconfirmed"
    assert "already" not in res["message"]
    assert "before the command" in res["readback"]["detail"]


def test_charge_start_when_already_charging_with_fresh_data_says_so(make_service, ha, clock):
    ha.apply_effects = False
    ha.set_state(CHARGING, "charging"); stamp(ha, CHARGING, NOW - 20)
    svc = svc_for(make_service, ha, clock)
    res = svc.run_command("charge_start", {}, surface="test")
    assert res["status"] == "confirmed" and res["readback"].get("already") == "charging"
    assert "already showed this" in res["message"]


# -- charge_amps: the number entity is HA's own echo; the car's actual current is the evidence -----------------------------
def test_charge_amps_is_unverifiable_while_not_charging(make_service, ha, clock):
    ha.set_state(CHARGING, "stopped")
    svc = svc_for(make_service, ha, clock)
    res = svc.run_command("charge_amps", {"amps": 8}, surface="test")
    assert res["status"] == "sent_unconfirmed" and res["readback"]["status"] == "unverifiable"
    assert ha.states["number.demo_car_charge_current"]["state"] == "8"      # HA's echo exists but proves nothing


def test_charge_amps_is_confirmed_by_the_cars_actual_current_while_charging(make_service, ha, clock):
    ha.set_state(CHARGING, "charging"); stamp(ha, CHARGING, NOW - 5)
    ha.set_state(CURRENT, "12"); stamp(ha, CURRENT, NOW - 5)
    ha.apply_effects = False
    svc = svc_for(make_service, ha, clock)
    real_sleep = svc._sleep

    def sleep(sec):
        real_sleep(sec)
        ha.set_state(CURRENT, "8"); stamp(ha, CURRENT, clock.t)             # the charger actually drops to 8 A
    svc._sleep = sleep
    res = svc.run_command("charge_amps", {"amps": 8}, surface="test")
    assert res["status"] == "confirmed" and res["readback"]["status"] == "confirmed"


# -- already in the target state ---------------------------------------------------------------------------------------------
def test_close_windows_when_already_closed_says_already_closed(make_service, ha, clock):
    for w in ("front_driver", "front_passenger", "rear_driver", "rear_passenger"):
        ha.set_state(f"binary_sensor.demo_car_{w}_window", "off")
    ha.set_state("cover.demo_car_windows", "closed")
    svc = svc_for(make_service, ha, clock)
    res = svc.run_command("close_windows", {}, surface="test")
    assert res["status"] == "confirmed" and res["readback"]["already"] == "closed"
    assert "already closed" in res["readback"]["detail"] and "already showed this (closed)" in res["message"]


def test_lock_when_already_locked_says_so(make_service, ha, clock):
    ha.set_state("lock.demo_car_lock", "locked")
    svc = svc_for(make_service, ha, clock)
    res = svc.run_command("lock", {}, surface="test")
    assert res["status"] == "confirmed" and res["readback"]["already"] == "locked"


def test_lock_that_really_changes_is_a_normal_confirmation(make_service, ha, clock):
    ha.set_state("lock.demo_car_lock", "unlocked")
    svc = svc_for(make_service, ha, clock)
    res = svc.run_command("lock", {}, surface="test")
    assert res["status"] == "confirmed" and "already" not in res["readback"]
    assert "the car confirmed it" in res["message"]


# -- B4 -------------------------------------------------------------------------------------------------------------------
def test_defrost_is_described_as_maximum_heat_and_fan(make_service, ha, clock):
    from pit_crew import schemas
    from pit_crew.brain.commands import COMMANDS
    assert "MAXIMUM" in schemas.CAR_COMMAND["description"] and "heat" in schemas.CAR_COMMAND["description"]
    assert "MAXIMUM" in COMMANDS["defrost"].summary
    svc = svc_for(make_service, ha, clock)
    live = svc.run_command("defrost", {"on": True}, surface="test")
    assert "MAXIMUM" in live["message"] and "Heads-up" in live["message"]
    off = svc.run_command("defrost", {"on": False}, surface="test")
    assert "Heads-up" not in off["message"]
