from datetime import datetime, timedelta, timezone

import pytest

from pit_crew.adapters.base import DriveRecord
from pit_crew.brain import readiness as rd
from pit_crew.brain.models import VehicleState
from pit_crew.brain.timeparse import WhenError, parse_when

TZ = timezone(timedelta(hours=-6))
NOW = datetime(2026, 11, 8, 21, 0, tzinfo=TZ)  # Sunday 9 PM
DEP = datetime(2026, 11, 9, 7, 30, tzinfo=TZ)  # Monday 7:30 AM (10.5 h away)


def state(**vals):
    st = VehicleState(name="T", source="t", captured_at=NOW.timestamp())
    base = dict(battery_pct=50, charging_state="charging", plugged_in=True, charge_power_kw=7.0, charge_limit_pct=80,
                charge_current_max_a=32, charger_voltage_v=240, location_zone="home", outside_temp_f=60)
    base.update(vals)
    for k, v in base.items():
        st.set(k, v)
    return st


def drives(n=10, wh=250.0, temp=60.0):
    return [DriveRecord(start=1e9 + i, end=1e9 + i + 600, miles=10.0, duration_min=20, kwh=wh * 10 / 1000,
                        wh_per_mi=wh, outside_temp_f=temp) for i in range(n)]


def test_weather_factor_monotonic_extremes():
    assert rd.weather_factor(10) > rd.weather_factor(35) > rd.weather_factor(70) == 1.0
    assert rd.weather_factor(95) > 1.0 and rd.weather_factor(None) == 1.0


def test_ready_when_charging_will_finish(monkeypatch):
    res = rd.evaluate(state(), DEP, NOW, trip_mi=40, drives=drives(), battery_kwh=75)
    # 40 mi * 250 Wh = 10 kWh = 13.3% * 1.15 + 10 = ~25.3 % needed; charging to 80% easily
    assert res["ok"] and res["verdict"] == "ready" and res["ready"]
    assert res["needed_pct"] == pytest.approx(25, abs=1.5)
    assert res["projected_at_departure_pct"] == 80
    assert "history" in res["assumptions"]["efficiency"] or "trips" in res["assumptions"]["efficiency"]
    assert res["summary"].startswith("Ready for Mon 7:30 AM")


def test_limit_too_low_recommends_raising_limit():
    st = state(battery_pct=40, charge_limit_pct=50, charging_state="complete", charge_power_kw=0)
    res = rd.evaluate(st, DEP, NOW, trip_mi=150, drives=drives(), battery_kwh=75)
    assert res["verdict"] == "not_ready"
    rec = next(r for r in res["recommendations"] if r["action"] == "raise_charge_limit")
    assert rec["command"] == "charge_limit" and rec["args"]["percent"] >= 65 and rec["args"]["percent"] <= 100
    assert any("charge limit" in i for i in res["issues"])


def test_not_plugged_in_at_home_says_plug_in():
    st = state(battery_pct=30, plugged_in=False, charging_state="disconnected", charge_power_kw=0)
    res = rd.evaluate(st, DEP, NOW, trip_mi=60, drives=drives(), battery_kwh=75)
    assert res["verdict"] == "not_ready"
    assert any(r["action"] == "plug_in" for r in res["recommendations"])


def test_plugged_but_not_charging_recommends_start():
    st = state(battery_pct=30, charging_state="stopped", charge_power_kw=0)
    res = rd.evaluate(st, DEP, NOW, trip_mi=60, drives=drives(), battery_kwh=75)
    assert any(r["command"] == "charge_start" for r in res["recommendations"])


def test_slow_120v_charging_flags_speed_and_gives_ready_by():
    st = state(battery_pct=20, charge_power_kw=1.4, charger_voltage_v=117, charge_current_max_a=12, charge_limit_pct=90)
    res = rd.evaluate(st, DEP, NOW, trip_mi=120, drives=drives(), battery_kwh=75)
    assert res["verdict"] == "not_ready"
    assert any("too slow" in i for i in res["issues"])
    assert res["charging"]["ready_by"] is not None
    assert any(r["action"] == "charge_elsewhere_or_leave_later" for r in res["recommendations"])


def test_tight_verdict():
    # required ~25.3%; projected stays at battery_pct (not charging) so choose soc just above
    st = state(battery_pct=27, charging_state="complete", charge_power_kw=0, charge_limit_pct=27)
    res = rd.evaluate(st, DEP, NOW, trip_mi=40, drives=drives(), battery_kwh=75)
    assert res["verdict"] == "tight"


def test_cold_uses_more_energy_with_default_efficiency():
    warm = rd.evaluate(state(), DEP, NOW, trip_mi=50, expected_temp_f=70, battery_kwh=75)
    cold = rd.evaluate(state(), DEP, NOW, trip_mi=50, expected_temp_f=10, battery_kwh=75)
    assert cold["trip"]["wh_per_mi"] > warm["trip"]["wh_per_mi"]
    assert cold["needed_pct"] > warm["needed_pct"]
    assert "default estimate" in warm["assumptions"]["efficiency"]


def test_history_near_temperature_beats_overall():
    mixed = drives(6, 230.0, 70.0) + drives(6, 330.0, 25.0)
    res = rd.evaluate(state(), DEP, NOW, trip_mi=30, expected_temp_f=25, drives=mixed, battery_kwh=75)
    assert res["trip"]["wh_per_mi"] == pytest.approx(330, abs=1)
    assert "similar temperatures" in res["assumptions"]["efficiency"]


def test_precondition_recommended_in_cold_when_plugged_in_and_respects_capabilities():
    res = rd.evaluate(state(), DEP, NOW, trip_mi=20, expected_temp_f=15, battery_kwh=75)
    pre = next(r for r in res["recommendations"] if r["action"] == "precondition")
    assert pre["command"] == "climate_on" and pre["at"].startswith("2026-11-09T07:0")  # 30 min before 07:30
    res2 = rd.evaluate(state(), DEP, NOW, trip_mi=20, expected_temp_f=15, battery_kwh=75, can_do={"lock"})
    assert not any(r["action"] == "precondition" for r in res2["recommendations"])


def test_errors():
    st = VehicleState(name="x", source="t", captured_at=0)
    assert rd.evaluate(st, DEP, NOW, trip_mi=10)["ok"] is False
    assert rd.evaluate(state(), NOW - timedelta(hours=1), NOW, trip_mi=10)["ok"] is False


@pytest.mark.parametrize("text,expected", [
    ("tomorrow 7:30 AM", datetime(2026, 11, 9, 7, 30, tzinfo=TZ)),
    ("tomorrow 7:30am", datetime(2026, 11, 9, 7, 30, tzinfo=TZ)),
    ("7am", datetime(2026, 11, 9, 7, 0, tzinfo=TZ)),  # next occurrence (already past 7 today -> tomorrow)
    ("22:30", datetime(2026, 11, 8, 22, 30, tzinfo=TZ)),
    ("tonight 11pm", datetime(2026, 11, 8, 23, 0, tzinfo=TZ)),
    ("Friday 8:15 PM", datetime(2026, 11, 13, 20, 15, tzinfo=TZ)),
    ("mon 6am", datetime(2026, 11, 9, 6, 0, tzinfo=TZ)),
    ("2026-11-10T09:00:00-06:00", datetime(2026, 11, 10, 9, 0, tzinfo=TZ)),
])
def test_parse_when(text, expected):
    assert parse_when(text, NOW) == expected


def test_parse_when_errors():
    for bad in ("", "sometime", "today 7am"):
        with pytest.raises(WhenError):
            parse_when(bad, NOW)


# -- Pass 8: R10 (voltage) and R11 (scheduled charging) -------------------------------------------------------
def test_r10_idle_charger_voltage_is_not_treated_as_240v():
    st = state(charging_state="stopped", charger_voltage_v=2, charge_power_kw=0)
    unknown = rd.charge_rate_kw(st)
    assert unknown["kw"] == 0.0 and "unknown" in unknown["basis"]
    remembered = rd.charge_rate_kw(st, last_charge_volts=117)
    assert remembered["kw"] == pytest.approx(117 * 32 / 1000 * rd.CHARGE_EFFICIENCY) and "last seen" in remembered["basis"]
    silent = rd.charge_rate_kw(state(charging_state="stopped", charger_voltage_v=None, charge_power_kw=0))
    assert silent["kw"] > 0 and "assuming" in silent["basis"]


def test_r11_scheduled_charging_is_respected():
    st = state(battery_pct=40, charging_state="stopped", charge_power_kw=0)
    sched = datetime(2026, 11, 9, 0, 0, tzinfo=TZ)           # plugged in at 9 pm, schedule starts at midnight
    res = rd.evaluate(st, DEP, NOW, trip_mi=100, drives=drives(), battery_kwh=75, scheduled_start=sched,
                      can_do={"charge_start", "charge_limit"})
    assert res["verdict"] == "ready" and res["charging"]["scheduled_start"].startswith("2026-11-09T00:00")
    assert not any(r["action"] == "start_charging" for r in res["recommendations"])
    # without a schedule the same car is not charging and is told to start
    res = rd.evaluate(st, DEP, NOW, trip_mi=100, drives=drives(), battery_kwh=75, can_do={"charge_start", "charge_limit"})
    assert res["verdict"] == "not_ready"


def test_r9_range_sourced_drives_do_not_feed_efficiency():
    circular = [DriveRecord(start=1e9 + i, end=1e9 + i + 600, miles=10.0, duration_min=20, kwh=2.4, wh_per_mi=None,
                            outside_temp_f=15.0) for i in range(10)]
    eff = rd.estimate_efficiency(circular, 15.0)
    assert "default" in eff["source"] and eff["wh_per_mi"] == round(rd.DEFAULT_WH_PER_MI * rd.weather_factor(15.0))
