import pytest

from fakes import FakeTeslaMate, load_fixture
from pit_crew.adapters.base import AdapterError
from pit_crew.adapters.teslamate import (TeslaMateClient, TeslaMateHistory, TeslaMateLive, normalize_charge,
                                           normalize_drive, status_to_state)


@pytest.fixture
def tm():
    fake = FakeTeslaMate()
    yield fake
    fake.stop()


NOW = 1_794_300_000.0


def test_status_mapping_from_live_fixture():
    data = load_fixture("teslamate_cars_1_status.json")["data"]
    st = status_to_state(data, "My Car", NOW)
    v = st.value
    assert v("battery_pct") == 80 and v("range_mi") == pytest.approx(222.9, abs=0.1)
    assert v("plugged_in") is True and v("charging_state") == "complete"
    assert v("charge_limit_pct") == 80 and v("charge_eta_min") == 0
    assert v("locked") is True and v("sentry_on") is False
    assert v("doors_open") == [] and v("windows_open") == [] and v("trunk_open") is False
    assert v("cabin_temp_f") == pytest.approx(73.22) and v("outside_temp_f") == pytest.approx(68.9)
    assert v("climate_on") is False and v("software_version") == "2026.1.1"
    assert v("software_update_available") is False
    # 'suspended' -> data is aged from state_since, not from now
    assert st.age_s("battery_pct", NOW) > 60
    assert v("asleep") is False  # suspended is "trying to sleep", not asleep
    assert v("location_zone") is None  # redacted geofence -> not guessed
    assert "latitude" not in str(st.to_public(NOW))


def test_drive_normalization_uses_speed_times_duration_when_odometer_zeroed():
    d = normalize_drive(load_fixture("teslamate_cars_1_drives_show_5.json")["data"]["drives"][0])
    assert d.miles == pytest.approx(23.41 * 11 / 60, abs=0.05)
    assert d.kwh == pytest.approx(1.1386, abs=0.001)
    assert 240 < d.wh_per_mi < 290  # kWh / miles, not the (sanitized) consumption_net column
    assert d.start_pct == 72 and d.end_pct == 70 and d.outside_temp_f == pytest.approx(51.6, abs=0.1)


def test_charge_normalization():
    c = normalize_charge(load_fixture("teslamate_cars_1_charges_show_5.json")["data"]["charges"][1])
    assert c.kwh_added == 7.14 and c.kwh_used == 9.27 and c.cost == 0.93
    assert c.start_pct == 70 and c.end_pct == 80 and c.duration_min == 410


def test_live_adapter_over_http_and_read_only_capabilities(tm):
    a = TeslaMateLive(TeslaMateClient(tm.url, 1), name="Demo")
    st = a.fetch_state(NOW)
    assert st.name == "Demo" and st.value("battery_pct") == 80
    assert a.capabilities().commands == frozenset()


def test_history_over_http_and_only_get(tm):
    h = TeslaMateHistory(TeslaMateClient(tm.url, 1))
    drives = h.drives(0, 2e10)
    charges = h.charges(0, 2e10)
    assert len(drives) == 5 and len(charges) == 5
    assert drives == sorted(drives, key=lambda d: d.start)
    health = h.battery_health()
    assert health.health_pct == pytest.approx(90.1, abs=0.1) and health.current_capacity_kwh == pytest.approx(72.12, abs=0.01)
    assert {m for m, *_ in tm.requests} == {"GET"}
    assert all(path.startswith("/api/v1/cars") for _, path, *_ in tm.requests)
    assert not any("command" in path or "wake" in path for _, path, *_ in tm.requests)


def test_token_sent_as_bearer_and_bad_token_clean_error():
    fake = FakeTeslaMate(require_token=True)
    try:
        ok = TeslaMateClient(fake.url, 1, FakeTeslaMate.TOKEN).status()
        assert ok["status"]["state"] == "suspended"
        with pytest.raises(AdapterError, match="rejected the credentials"):
            TeslaMateClient(fake.url, 1, "wrong").status()
    finally:
        fake.stop()


def test_wrong_car_id_is_clean_error(tm):
    with pytest.raises(AdapterError):
        TeslaMateClient(tm.url, 7).status()


def test_client_has_no_write_methods():
    public = [n for n in dir(TeslaMateClient) if not n.startswith("_")]
    assert sorted(public) == ["battery_health", "cars", "charges", "drives", "status"]


# -- Pass 8: R12 units on history import, R15 gear allowlist -------------------------------------------------------
def test_r12_metric_history_is_converted_to_miles_and_fahrenheit():
    raw = dict(load_fixture("teslamate_cars_1_drives_show_5.json")["data"]["drives"][0])
    raw.update(_units={"unit_of_length": "km", "unit_of_temperature": "C"}, speed_max=100, speed_avg=50,
               outside_temp_avg=10.0, odometer_details={"odometer_distance": 16.0934})
    d = normalize_drive(raw)
    assert d.miles == pytest.approx(10.0, abs=0.01) and d.outside_temp_f == pytest.approx(50.0)
    assert d.speed_max_mph == pytest.approx(62.1, abs=0.1) and d.speed_avg_mph == pytest.approx(31.07, abs=0.05)
    charge = dict(load_fixture("teslamate_cars_1_charges_show_5.json")["data"]["charges"][0],
                  _units={"unit_of_length": "km", "unit_of_temperature": "C"}, outside_temp_avg=0.0)
    assert normalize_charge(charge).outside_temp_f == pytest.approx(32.0)


def test_r12_client_tags_rows_with_response_units(tm):
    client = TeslaMateClient(tm.url, 1)
    rows = client.drives(0, 2e9)
    assert rows and all(r["_units"] == {"unit_of_length": "mi", "unit_of_temperature": "F"} for r in rows)


@pytest.mark.parametrize("state,gear", [("online", "P"), ("asleep", "P"), ("offline", None), ("start", None), ("weird", None)])
def test_r15_empty_gear_is_parked_only_for_allowlisted_states(state, gear):
    data = load_fixture("teslamate_cars_1_status.json")["data"]
    data["status"]["state"] = state
    data["status"]["driving_details"]["shift_state"] = None
    assert status_to_state(data, "C", NOW).value("shift_state") == gear
