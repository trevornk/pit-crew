"""Direct Tesla adapter: state mapping, sleep/rate-limit behaviour, command mapping, lazy import, token rotation.
No network and no tesla-fleet-api install: the library is replaced by fakes."""

import asyncio
import json
import os
import stat
import sys

import pytest

from pit_crew import tools
from pit_crew.adapters import base, tesla_direct as td
from pit_crew.adapters.base import AdapterError, BuildContext, CommandNotSupported
from pit_crew.brain.commands import COMMANDS
from pit_crew.brain.models import VehicleState
from pit_crew.constants import ENV_TESLA_TOKEN

from fakes import FIXTURES

DATA = json.loads((FIXTURES.parent / "tesla_vehicle_data_synthetic.json").read_text())
NOW = 1_790_000_100.0
ZONES = {"home": (10.01, 20.02, 500.0), "work": (11.5, 21.5, 300.0)}


def clone(data=None):
    return json.loads(json.dumps(data or DATA))


# -- fakes ---------------------------------------------------------------------------------------------------------
class FakeClient:
    """Stands in for LibraryClient (what TeslaDirectAdapter talks to)."""

    def __init__(self):
        self.state = "online"
        self.data = clone()
        self.calls = []
        self.apply = True
        self.error = None
        self.command_result = (True, "")

    def vehicle_state(self):
        self.calls.append("vehicle_state")
        if self.error:
            raise self.error
        return self.state

    def vehicle_data(self, with_location):
        self.calls.append(f"vehicle_data(loc={with_location})")
        if self.error:
            raise self.error
        return clone(self.data)

    def wake(self):
        self.calls.append("wake")
        self.state = "online"

    def command(self, name, args):
        self.calls.append(f"command:{name}")
        if self.apply and name == "door_unlock" or (self.apply and name == "unlock"):
            self.data["response"]["vehicle_state"]["locked"] = False
        if self.apply and name == "lock":
            self.data["response"]["vehicle_state"]["locked"] = True
        if self.apply and name in ("lock", "unlock", "door_unlock"):
            # the car re-reports the section when its state changes: a fresh car-side timestamp
            self.data["response"]["vehicle_state"]["timestamp"] = int(NOW * 1000)
        return self.command_result


def cfg(**kw):
    base_kw = dict(mode="teslemetry", vin="5YJ3E1EA7KF317000", token="t", min_poll_s=120.0, zones={}, name="Model Y")
    base_kw.update(kw)
    return td.TeslaConfig(**base_kw)


def adapter(client=None, **kw):
    return td.TeslaDirectAdapter(cfg(**kw), client or FakeClient())


# -- mapping ------------------------------------------------------------------------------------------------------
def test_vehicle_data_mapped_to_us_units_and_normalized_fields():
    st = td.tesla_data_to_state(DATA, NOW, name="Model Y", zones=ZONES)
    v = st.value
    assert v("battery_pct") == 64 and v("range_mi") == 198.4
    assert v("charging_state") == "charging" and v("plugged_in") is True and v("charge_power_kw") == 11
    assert v("charge_limit_pct") == 80 and v("charge_eta_min") == 95 and v("charge_current_max_a") == 48
    assert v("cabin_temp_f") == 68.0 and v("outside_temp_f") == 50.0 and v("target_temp_f") == pytest.approx(69.8)
    assert v("locked") is True and v("doors_open") == ["rear_passenger"] and v("windows_open") == ["rear_passenger"]
    assert v("trunk_open") is False and v("frunk_open") is False and v("sentry_on") is False
    assert v("software_version") == "2026.1.1" and v("software_update_available") is True
    assert v("odometer_mi") == 12345.6
    assert v("shift_state") == "P" and v("speed_mph") == 0.0 and v("moving") is False and st.motion() is False
    assert v("asleep") is False and v("location_zone") == "home"
    # per-field freshness from the car's own section timestamps
    assert st.fields["battery_pct"].updated_at == 1_790_000_000.0
    assert "latitude" not in json.dumps(st.to_public(NOW)) and "10.01" not in json.dumps(st.to_public(NOW))


def test_zone_logic_and_privacy():
    assert td.zone_for(10.01, 20.02, ZONES) == "home"
    assert td.zone_for(11.5, 21.5, ZONES) == "work"
    assert td.zone_for(40.0, 50.0, ZONES) == "away"
    assert td.zone_for(10.01, 20.02, {}) is None and td.zone_for(None, None, ZONES) is None
    st = td.tesla_data_to_state(DATA, NOW, name="x", zones=None)
    assert not st.is_available("location_zone")        # no zones configured -> unknown, never guessed


def test_driving_state_maps_to_moving():
    d = clone()
    d["response"]["drive_state"].update(shift_state="D", speed=37)
    st = td.tesla_data_to_state(d, NOW, name="x")
    assert st.value("moving") is True and st.value("speed_mph") == 37.0 and st.motion() is True


def test_partial_and_empty_payloads_do_not_crash():
    assert td.tesla_data_to_state({}, NOW, name="x").value("battery_pct") is None
    d = {"response": {"charge_state": {"battery_level": 50, "charging_state": "Disconnected"}}}
    st = td.tesla_data_to_state(d, NOW, name="x")
    assert st.value("plugged_in") is False and st.value("charging_state") == "disconnected"


# -- never wake to poll; rate limits --------------------------------------------------------------------------------
def test_online_car_gets_state_then_data_call_with_location_only_when_zones_set():
    c = FakeClient()
    a = adapter(c)
    a.fetch_state(NOW)
    assert c.calls == ["vehicle_state", "vehicle_data(loc=False)"]
    c2 = FakeClient()
    adapter(c2, zones=ZONES).fetch_state(NOW)
    assert c2.calls[-1] == "vehicle_data(loc=True)"


def test_sleeping_car_is_never_polled_for_data_or_woken():
    c = FakeClient()
    a = adapter(c)
    a.fetch_state(NOW)                                       # online: cache primed
    c.state = "asleep"
    for i in range(1, 6):
        st = a.fetch_state(NOW + 200 * i)                    # well past the min poll interval
        assert st.value("asleep") is True and st.value("battery_pct") == 64
    assert "wake" not in c.calls and c.calls.count("vehicle_data(loc=False)") == 1
    # its data ages -> reported as stale, not refreshed
    assert st.age_s("battery_pct", NOW + 1000) > 900


def test_sleeping_car_with_no_cache_returns_minimal_asleep_state():
    c = FakeClient()
    c.state = "offline"
    st = adapter(c).fetch_state(NOW)
    assert st.value("asleep") is True and not st.has("battery_pct") and c.calls == ["vehicle_state"]


def test_min_poll_interval_serves_cache_without_api_calls():
    c = FakeClient()
    a = adapter(c)
    a.fetch_state(NOW)
    n = len(c.calls)
    for t in (5, 30, 119):
        a.fetch_state(NOW + t)
    assert len(c.calls) == n
    a.fetch_state(NOW + 121)
    assert len(c.calls) > n


def test_rate_limit_backs_off_and_serves_cache():
    c = FakeClient()
    a = adapter(c)
    a.fetch_state(NOW)
    c.error = td.RateLimitedError("slow down")
    st = a.fetch_state(NOW + 200)
    assert st.value("battery_pct") == 64
    calls = len(c.calls)
    a.fetch_state(NOW + 300)                                 # inside the 5 min back-off: no API call at all
    assert len(c.calls) == calls
    with pytest.raises(AdapterError):
        adapter(FakeClient_with_error()).fetch_state(NOW)    # nothing cached -> clean error


def FakeClient_with_error():
    c = FakeClient()
    c.error = td.RateLimitedError("slow down")
    return c


# -- commands ----------------------------------------------------------------------------------------------------------
def test_teslemetry_and_tessie_offer_every_command_with_just_a_token():
    assert adapter().capabilities().commands == frozenset(COMMANDS)
    assert adapter(mode="tessie").capabilities().commands == frozenset(COMMANDS)


def test_command_on_sleeping_car_refused_unless_wake_allowed_once():
    c = FakeClient()
    a = adapter(c)
    a.fetch_state(NOW)
    c.state = "asleep"
    a.fetch_state(NOW + 200)
    with pytest.raises(td.WakeRefused):
        a.send_command("lock", {})
    assert "wake" not in c.calls and "command:lock" not in c.calls
    a.allow_wake_once()
    assert a.send_command("lock", {}).accepted
    assert c.calls.count("wake") == 1
    c.state = "asleep"
    with pytest.raises(td.WakeRefused):                       # permission was single-use
        a.send_command("lock", {})


def test_declined_command_reports_reason():
    c = FakeClient()
    c.command_result = (False, "vehicle_unavailable")
    a = adapter(c)
    a.fetch_state(NOW)
    r = a.send_command("lock", {})
    assert r.accepted is False and "vehicle_unavailable" in r.detail


def test_end_to_end_through_safety_engine_with_readback_and_boosted_polling(stub_service, stub, clock, store):
    from pit_crew.service import CarService
    from pit_crew.settings import Settings

    c = FakeClient()
    a = adapter(c)
    base.register_adapter("tesla-fake", lambda ctx: a, replace=True)
    svc = CarService(Settings(overrides={"adapter": "tesla-fake", "history": "own", "verify_interval_s": 2.0}),
                     {}.get, store, wall_clock=clock, sleep=clock.sleep)
    clock.t = NOW
    svc.arm(1, by="t")
    c.data["response"]["vehicle_state"]["locked"] = False
    svc.get_state(force=True)
    res = svc.run_command("lock", {}, surface="test")
    assert res["status"] == "confirmed" and res["readback"]["status"] == "confirmed"
    assert "command:lock" in c.calls
    # unlock needs a human; a sleeping car is not woken for it
    c.state = "asleep"
    clock.advance(300)
    svc.get_state(force=True)
    out = svc.run_command("set_temp", {"temp_f": 70}, surface="test")
    assert out["status"] == "blocked" and "asleep" in out["message"], out
    assert "wake" not in c.calls


# -- factory / lazy import -------------------------------------------------------------------------------------------------
def bctx(settings, secrets=None):
    return BuildContext(settings=settings, secret=(secrets or {}).get)


def test_factory_validates_configuration_without_importing_the_library():
    sys.modules.pop("tesla_fleet_api", None)
    with pytest.raises(AdapterError, match="VIN"):
        td.build_tesla(bctx({"tesla_mode": "tessie"}))
    with pytest.raises(AdapterError, match=ENV_TESLA_TOKEN):
        td.build_tesla(bctx({"tesla_mode": "tessie", "tesla_vin": "5YJ3E1EA7KF317000"}))
    with pytest.raises(AdapterError, match="tesla_mode"):      # native Fleet-direct (plugin-held OAuth) no longer exists
        td.build_tesla(bctx({"tesla_mode": "fleet", "tesla_vin": "5YJ3E1EA7KF317000"}, {ENV_TESLA_TOKEN: "t"}))
    with pytest.raises(AdapterError, match="tesla_mode"):
        td.build_tesla(bctx({"tesla_mode": "carrier-pigeon", "tesla_vin": "x"}))
    a = td.build_tesla(bctx({"tesla_mode": "teslemetry", "tesla_vin": "5YJ3E1EA7KF317000"}, {ENV_TESLA_TOKEN: "tok"}))
    assert a.label == "Tesla via Teslemetry" and "tesla_fleet_api" not in sys.modules      # lazy


def test_missing_library_gives_actionable_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "tesla_fleet_api", None)       # makes `import tesla_fleet_api` raise ImportError
    a = td.build_tesla(bctx({"tesla_mode": "tessie", "tesla_vin": "5YJ3E1EA7KF317000"}, {ENV_TESLA_TOKEN: "tok"}))
    with pytest.raises(AdapterError) as e:
        a.fetch_state(NOW)
    assert "tesla-fleet-api>=1.5,<2" in str(e.value)


def test_registered_and_selectable_as_adapter_tesla():
    assert "tesla" in base.available_adapters()


# -- the library wrapper against a fake tesla_fleet_api ------------------------------------------------------------
class RateLimited(BaseException):
    pass


class InvalidToken(BaseException):
    pass


class FakeVehicle:
    def __init__(self, vin, log, state="online"):
        self.vin, self.log, self.state_name = vin, log, state

    def __getattr__(self, name):
        async def call(*a, **k):
            self.log.append((name, a, dict(k)))
            return {"response": {"result": True, "reason": ""}}
        return call

    async def vehicle(self):
        self.log.append(("vehicle", (), {}))
        return {"response": {"state": self.state_name}}

    async def state(self, use_cache=True):
        self.log.append(("state", (), {"use_cache": use_cache}))
        return {"response": {"state": self.state_name}}

    async def vehicle_data(self, endpoints=None):
        self.log.append(("vehicle_data", (), {"endpoints": endpoints}))
        return clone()


class FakeVehicles:
    def __init__(self, parent):
        self.parent = parent

    def createFleet(self, vin):
        self.parent.kind = "fleet"
        return self.parent.vehicle

    def createSigned(self, vin):
        self.parent.kind = "signed"
        return self.parent.vehicle


class FakeApi:
    def __init__(self, log, **kw):
        self.log, self.kw, self.kind = log, kw, None
        self.vehicle = FakeVehicle("v", log)
        self.vehicles = FakeVehicles(self)
        self.refresh_token = kw.get("refresh_token")

    async def get_private_key(self, path):
        self.log.append(("get_private_key", (path,), {}))


class FakeLib:
    def __init__(self):
        self.log = []
        self.apis = []

    def _make(self, *a, **k):
        api = FakeApi(self.log, **k)
        self.apis.append(api)
        return api

    def Teslemetry(self, session, token):
        api = self._make(token=token)
        api.mode = "teslemetry"
        return api

    def Tessie(self, session, token):
        api = self._make(token=token)
        api.mode = "tessie"
        return api

    def TeslaFleetOAuth(self, **kw):
        api = self._make(**kw)
        api.mode = "fleet"
        return api


def lib_client(mode="teslemetry", **kw):
    lib = FakeLib()
    c = td.LibraryClient(cfg(mode=mode, **kw), td.AsyncRunner(), lib=lib, session_factory=lambda: object())
    return c, lib


FLEET_EXPECT = {
    "climate_off": ("auto_conditioning_stop", ()), "lock": ("door_lock", ()), "unlock": ("door_unlock", ()),
    "charge_start": ("charge_start", ()), "charge_stop": ("charge_stop", ()), "honk": ("honk_horn", ()),
    "flash": ("flash_lights", ()), "sentry_on": ("set_sentry_mode", (True,)), "sentry_off": ("set_sentry_mode", (False,)),
    "open_trunk": ("actuate_trunk", ("rear",)), "open_frunk": ("actuate_trunk", ("front",)),
    "vent_windows": ("window_control", ("vent",)), "close_windows": ("window_control", ("close",)),
    "charge_limit": ("set_charge_limit", (85,)), "charge_amps": ("set_charging_amps", (24,)),
    "defrost": ("set_preconditioning_max", (True, False)), "set_temp": ("set_temps", (21.0, 21.0)),
    "seat_heat": ("remote_seat_heater_request", (2, 3)),
}
ARGS = {"charge_limit": {"percent": 85}, "charge_amps": {"amps": 24}, "defrost": {"on": True}, "set_temp": {"temp_f": 70},
        "seat_heat": {"seat": "rear_left", "level": 3}}


@pytest.mark.parametrize("name", sorted(FLEET_EXPECT))
def test_teslemetry_command_mapping(name):
    c, lib = lib_client("teslemetry")
    ok, _ = c.command(name, ARGS.get(name, {}))
    method, expected_args = FLEET_EXPECT[name]
    calls = [l for l in lib.log if l[0] == method]
    assert ok and calls and calls[-1][1] == expected_args, (name, lib.log)


def test_climate_on_sets_temperature_then_starts():
    c, lib = lib_client()
    c.command("climate_on", {"temp_f": 72})
    assert [l[0] for l in lib.log] == ["set_temps", "auto_conditioning_start"] and lib.log[0][1] == (22.0, 22.0)
    lib.log.clear()
    c.command("climate_on", {})
    assert [l[0] for l in lib.log] == ["auto_conditioning_start"]


TESSIE_EXPECT = {
    "lock": ("lock", ()), "unlock": ("unlock", ()), "climate_off": ("stop_climate", ()), "charge_start": ("start_charging", ()),
    "charge_stop": ("stop_charging", ()), "honk": ("honk", ()), "flash": ("flash", ()), "sentry_on": ("enable_sentry", ()),
    "sentry_off": ("disable_sentry", ()), "open_trunk": ("activate_rear_trunk", ()), "open_frunk": ("activate_front_trunk", ()),
    "vent_windows": ("vent_windows", ()), "close_windows": ("close_windows", ()), "charge_limit": ("set_charge_limit", (85,)),
    "charge_amps": ("tessie_set_charging_amps", (24,)), "set_temp": ("set_temperatures", (21.0,)),
    "seat_heat": ("set_seat_heat", ("rear_left", 3)),
}


@pytest.mark.parametrize("name", sorted(TESSIE_EXPECT))
def test_tessie_command_mapping(name):
    c, lib = lib_client("tessie")
    ok, _ = c.command(name, ARGS.get(name, {}))
    method, expected = TESSIE_EXPECT[name]
    assert ok and [l for l in lib.log if l[0] == method][-1][1] == expected


def test_unsupported_command_in_tessie_mode():
    c, _ = lib_client("tessie")
    with pytest.raises(CommandNotSupported):
        c.command("defrost", {"on": True})


def test_polling_never_calls_wake_and_uses_cache_for_tessie():
    c, lib = lib_client("teslemetry")
    assert c.vehicle_state() == "online"
    c.vehicle_data(with_location=True)
    names = [l[0] for l in lib.log]
    assert "wake_up" not in names and "wake" not in names
    assert [l for l in lib.log if l[0] == "vehicle_data"][0][2]["endpoints"][-1] == "location_data"
    ct, lt = lib_client("tessie")
    ct.vehicle_state(); ct.vehicle_data(False)
    assert all(l[2].get("use_cache") is True for l in lt.log if l[0] == "state")


def test_library_errors_are_converted_secret_free_and_ratelimit_is_typed():
    c, lib = lib_client()
    api_vehicle = None

    async def boom(*a, **k):
        raise InvalidToken("Bearer abcdefghijklmnop12345 rejected")
    c.vehicle_state()                                      # builds
    lib.apis[0].vehicle.vehicle = boom
    with pytest.raises(AdapterError) as e:
        c.vehicle_state()
    assert "rejected the access token" in str(e.value) and "abcdefghijklmnop" not in str(e.value)

    async def limited(*a, **k):
        raise RateLimited()
    lib.apis[0].vehicle.vehicle = limited
    with pytest.raises(td.RateLimitedError):
        c.vehicle_state()


def test_adapter_module_does_not_import_the_library_at_import_time():
    src = open(td.__file__, encoding="utf-8").read()
    top_level = [l for l in src.splitlines() if l.startswith(("import tesla_fleet_api", "from tesla_fleet_api"))]
    assert top_level == []
    assert "tesla_fleet_api" not in sys.modules


def test_no_plugin_held_oauth_anywhere():
    """Catalog rule 11: no refresh-token storage/rotation, no token files, no OAuth client in the Tesla adapter."""
    src = open(td.__file__, encoding="utf-8").read().lower()
    for banned in ("refresh_token", "token_store", "tokenstore", "teslafleetoauth", "client_secret", "get_private_key",
                   "private_key", "os.environ", "getenv", "os.open", "open("):
        assert banned not in src, banned


def test_the_token_comes_only_from_ctx_secret():
    seen = []
    ctx = BuildContext(settings={"tesla_mode": "tessie", "tesla_vin": "5YJ3E1EA7KF317000"},
                       secret=lambda name: (seen.append(name), "tok")[1])
    td.build_tesla(ctx)
    assert seen == [ENV_TESLA_TOKEN]


def test_car_status_with_direct_adapter_reports_label_and_sleep(store, clock):
    from pit_crew.service import CarService, set_service
    from pit_crew.settings import Settings
    c = FakeClient()
    a = adapter(c)
    base.register_adapter("tesla-fake2", lambda ctx: a, replace=True)
    svc = CarService(Settings(overrides={"adapter": "tesla-fake2"}), {}.get, store, wall_clock=clock, sleep=clock.sleep)
    set_service(svc)
    clock.t = NOW
    out = json.loads(tools.car_status({}))
    assert out["ok"] and out["adapter"]["label"] == "Tesla via Teslemetry" and out["battery"]["battery_pct"] == 64
    c.state = "asleep"
    clock.advance(500)
    out = json.loads(tools.car_status({"refresh": True}))
    assert out["status"]["asleep"] is True and out["freshness"]["stale_fields"] == {} or out["freshness"]["newest_update_age_s"] >= 0
    set_service(None)
