"""G8 audit regressions: adapters (non-finite readings, malformed bodies, backoff, factories, resource release)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import pit_crew.adapters.tesla_direct as td
from fakes import FakeHA, load_fixture
from pit_crew.adapters.base import AdapterError, AdapterUnavailable, BuildContext
from pit_crew.adapters.discovery import discover
from pit_crew.adapters.homeassistant import HomeAssistantAdapter, parse_ha_time
from pit_crew.adapters.http import json_request
from pit_crew.adapters.profile import load_profile
from pit_crew.adapters.teslamate import normalize_drive, build_teslamate_live

NOW = parse_ha_time("2025-10-05T00:17:00+00:00")


class RawServer:
    """Serves one fixed raw body/status for any request (so malformed JSON can be sent)."""

    def __init__(self, body: bytes, status: int = 200):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self):
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

            do_GET = do_POST = _go

        self.body, self.status = body, status
        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def ha_adapter(states):
    ha = FakeHA(states)
    return ha, HomeAssistantAdapter(ha.url, FakeHA.TOKEN, load_profile("tesla_fleet"), "demo_car")


# -- non-finite readings ------------------------------------------------------------------------------------------
def test_nan_speed_is_not_a_fresh_parked_proof_and_nan_readings_are_unavailable():
    states = load_fixture("ha_states_tesla_fleet.json") + [
        {"entity_id": "sensor.demo_car_speed", "state": "nan", "attributes": {"unit_of_measurement": "mph"},
         "last_updated": "2025-10-05T00:16:00+00:00"},
        {"entity_id": "sensor.demo_car_odometer", "state": "inf", "attributes": {"unit_of_measurement": "km"},
         "last_updated": "2025-10-05T00:16:00+00:00"}]
    for s in states:
        if s["entity_id"] == "sensor.demo_car_battery_level":
            s["state"] = "NaN"
        if s["entity_id"] == "sensor.demo_car_battery_range":
            s["state"] = "-inf"
    states = [s for s in states if "teslamate" not in s["entity_id"]]      # no other motion source than the speed sensor
    ha, a = ha_adapter(states)
    try:
        st = a.fetch_state(NOW)
    finally:
        ha.stop()
    for f in ("speed_mph", "odometer_mi", "battery_pct", "range_mi"):
        assert not st.is_available(f), f
    # the NaN speed must not become a fresh "parked" vote (nan > 1 is False): there is no motion reading at all
    assert not st.has("moving")


@pytest.mark.parametrize("body", [b'{"a": NaN}', b"[Infinity]", b'{"a": -Infinity}', b"[" * 200000, b"not json"])
def test_json_responses_with_nan_or_pathological_nesting_are_adapter_errors(body):
    srv = RawServer(body)
    try:
        with pytest.raises(AdapterError):
            json_request("GET", srv.url + "/x", what="Test")
    finally:
        srv.stop()


def test_gateway_errors_are_unavailable_so_commands_are_read_back_not_called_failed():
    for code, cls in ((502, AdapterUnavailable), (503, AdapterUnavailable), (504, AdapterUnavailable)):
        srv = RawServer(b"{}", code)
        try:
            with pytest.raises(cls):
                json_request("POST", srv.url + "/x", body={}, what="Test")
        finally:
            srv.stop()
    srv = RawServer(b"{}", 500)
    try:
        with pytest.raises(AdapterError) as e:
            json_request("POST", srv.url + "/x", body={}, what="Test")
        assert not isinstance(e.value, AdapterUnavailable)
    finally:
        srv.stop()


# -- malformed Home Assistant entries --------------------------------------------------------------------------------
def test_malformed_entities_do_not_crash_a_poll():
    states = load_fixture("ha_states_tesla_fleet.json")
    for s in states:
        if s["entity_id"] == "sensor.demo_car_battery_level":
            s["attributes"] = [1, 2]
        if s["entity_id"] == "sensor.demo_car_inside_temperature":
            s["last_updated"] = 12345
            s["last_reported"] = {"x": 1}
        if s["entity_id"] == "lock.demo_car_lock":
            s["attributes"] = None
    ha, a = ha_adapter(states)
    ha.states["junk1"] = {"entity_id": ["not", "a", "string"], "state": "on"}       # served verbatim by /api/states
    ha.states["junk2"] = "garbage"
    try:
        st = a.fetch_state(NOW)
    finally:
        ha.stop()
    assert st.value("range_mi") is not None and st.value("battery_pct") == 80   # attributes: [..] is treated as none


def test_whitespace_only_slug_is_rejected():
    with pytest.raises(AdapterError, match="which Home Assistant car"):
        HomeAssistantAdapter("http://127.0.0.1:1", "t", load_profile("tesla_fleet"), "   ")


def test_discover_accepts_a_generator():
    states = load_fixture("ha_states_tesla_fleet.json")
    assert discover(iter(states)) == discover(states)


# -- Tesla / TeslaMate ---------------------------------------------------------------------------------------------------
def test_nan_and_inf_in_vehicle_data_are_unavailable_not_values():
    data = {"charge_state": {"battery_level": float("nan"), "battery_range": float("inf"), "timestamp": 1_790_000_000_000},
            "vehicle_state": {"odometer": float("nan"), "tpms_pressure_fl": float("inf"), "timestamp": 1_790_000_000_000},
            "drive_state": {"speed": float("nan"), "shift_state": None, "timestamp": 1_790_000_000_000}}
    st = td.tesla_data_to_state(data, 1_790_000_100.0, name="t")
    assert not st.has("battery_pct") and not st.has("range_mi") and not st.has("odometer_mi") and not st.has("tire_psi_fl")
    assert st.value("speed_mph") == 0.0          # NaN speed -> treated as no speed (0.0 by the existing rule), never NaN


def test_teslamate_consumption_net_fallback_is_wh_per_mile():
    raw = {"start_date": "2026-01-01T10:00:00Z", "end_date": "2026-01-01T10:05:00Z", "duration_min": 5,
           "odometer_details": {"odometer_distance": 0.0}, "speed_avg": 0, "consumption_net": 0.16,
           "_units": {"unit_of_length": "km", "unit_of_temperature": "C"}}
    d = normalize_drive(raw)
    assert d.wh_per_mi == pytest.approx(0.16 * 1000 / 0.621371, abs=0.2)          # 0.16 kWh/km = ~257 Wh/mi


def test_numeric_settings_garbage_is_an_adapter_error_not_a_valueerror():
    def ctx(**kw):
        return BuildContext(settings={"tesla_mode": "teslemetry", "tesla_vin": "5YJ3E1EA7KF317000", **kw}, secret={"PIT_CREW_TESLA_TOKEN": "t"}.get)

    for bad in ({"tesla_home_latitude": "abc"}, {"tesla_min_poll_s": "soon"}, {"tesla_home_longitude": "nan"},
                {"tesla_home_latitude": 10, "tesla_home_longitude": 20, "tesla_home_radius_m": "wide"}):
        with pytest.raises(AdapterError):
            td.build_tesla(ctx(**bad))
    td.build_tesla(ctx(tesla_zones=["not", "a", "mapping"]))                 # ignored with a warning, not an AttributeError
    with pytest.raises(AdapterError, match="Car id"):
        build_teslamate_live(BuildContext(settings={"teslamate_car_id": "one", "teslamate_url": "http://127.0.0.1:1"}, secret=lambda k: None))


def test_failed_data_calls_back_off_instead_of_hammering_the_api():
    class Client:
        def __init__(self):
            self.calls = 0

        def vehicle_state(self):
            self.calls += 1
            return "online"

        def vehicle_data(self, with_location):
            self.calls += 1
            raise AdapterError("upstream broke")

    c = Client()
    a = td.TeslaDirectAdapter(td.TeslaConfig(mode="teslemetry", vin="5YJ3E1EA7KF317000", token="t", min_poll_s=120.0,
                                              zones={}, name="x"), c)
    with pytest.raises(AdapterError, match="upstream broke"):
        a.fetch_state(1000.0)
    assert c.calls == 2
    with pytest.raises(AdapterUnavailable, match="returned an error"):
        a.fetch_state(1010.0)
    assert c.calls == 2                                  # no new billed calls inside the backoff window
    with pytest.raises(AdapterError):
        a.fetch_state(1000.0 + td.ERROR_BACKOFF_S + 1)   # then it tries again
    assert c.calls == 4


def test_async_runner_close_releases_the_event_loop():
    r = td.AsyncRunner()
    loop = r._ensure()
    thread = r._thread
    assert r.run(_noop()) == 7
    r.close()
    assert loop.is_closed() and not thread.is_alive()
    r.close()                                             # idempotent


async def _noop():
    return 7


# -- simulator -----------------------------------------------------------------------------------------------------------------
def test_simulator_rejects_non_finite_numbers_and_stays_usable(service):
    sim = service.adapter
    for action, kw in (("advance", {"minutes": float("nan")}), ("drive", {"miles": float("inf")}),
                       ("set_soc", {"percent": float("nan")}), ("cold", {"temp_f": float("nan")})):
        with pytest.raises(AdapterError):
            sim.control(action, **kw)
    assert service.get_state(force=True).value("battery_pct") is not None
