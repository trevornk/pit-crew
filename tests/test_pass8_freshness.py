"""Pass 8: R1 (HA stamps = last *changed*), R2 (zone/lock freshness), R16 (watcher freshness) from the REAL live fixture."""

import pytest

from fakes import FakeHA
from pit_crew.adapters.homeassistant import HomeAssistantAdapter, parse_ha_time
from pit_crew.adapters.profile import load_profile
from pit_crew.brain.commands import COMMANDS
from pit_crew.safety import policy
from pit_crew.settings import DEFAULTS

# the real fixture's newest stamp is 20:40:25Z; the car is online (status entity 'on')
NOW = parse_ha_time("2025-10-05T00:02:00+00:00")


@pytest.fixture
def ha():
    fake = FakeHA()
    yield fake
    fake.stop()


def state(ha, now=NOW):
    return HomeAssistantAdapter(ha.url, FakeHA.TOKEN, load_profile("tesla_fleet"), "demo_car").fetch_state(now)


def test_r1_unchanged_values_are_confirmed_fresh_while_online(ha):
    st = state(ha)
    # raw HA stamps: charge cable 02:31, frunk Nov 5, lock 19:52 — all "stale" by last-changed. None may be now.
    for f in ("plugged_in", "frunk_open", "locked", "range_mi", "doors_open", "windows_open"):
        assert st.age_s(f, NOW) <= 600, f
    assert st.fields["frunk_open"].changed_at is not None and st.fields["frunk_open"].changed_at < NOW - 86400


def test_r1_unlock_is_not_refused_as_unconfirmable_on_the_real_fixture(ha):
    st = state(ha)
    st.set("location_zone", "home", st.fields["location_zone"].updated_at)
    res = policy.evaluate(COMMANDS["unlock"], {}, st, dict(DEFAULTS), NOW)
    by = {c.name: c for c in res.checks}
    assert by["state_fresh"].ok and by["parked"].ok, res.denials


def test_r1_offline_car_keeps_per_entity_stamps(ha):
    ha.set_state("binary_sensor.demo_car_status", "off")
    st = state(ha)
    assert st.value("asleep") is True
    assert st.age_s("frunk_open", NOW) > 86400


def test_r2_stale_zone_cannot_satisfy_trusted_zone(ha):
    st = state(ha)
    st.set("location_zone", "home", NOW - 4000)           # location reading is old although other fields are fresh
    res = policy.evaluate(COMMANDS["unlock"], {}, st, dict(DEFAULTS), NOW)
    zone = [c for c in res.checks if c.name == "trusted_zone"][0]
    assert not zone.ok and "location reading" in zone.detail
    # an explicit override still works (it is confirmed by the human)
    res = policy.evaluate(COMMANDS["unlock"], {"override_zone": True}, st, dict(DEFAULTS), NOW)
    assert [c for c in res.checks if c.name == "trusted_zone"][0].ok


# -- R7: time parser ------------------------------------------------------------------------------------------
from datetime import datetime, timezone

from pit_crew.brain.timeparse import parse_when


@pytest.mark.parametrize("text,expect", [
    ("Nov 10 7am", (11, 10, 7, 0)),
    ("November 10th at 7:30 pm", (11, 10, 19, 30)),
    ("10 Nov 6am", (11, 10, 6, 0)),
    ("Friday the 14th at 8am", (11, 14, 8, 0)),          # 2026-11-14 is a Saturday; weekday wins -> Fri Nov 13
    ("7.30am", (11, 9, 7, 30)),
    ("tonight 8", (11, 8, 20, 0)),
    ("tomorrow 7:30", (11, 9, 7, 30)),
    ("8:30 dentist, bring 2 kids", (11, 9, 8, 30)),
    ("Jan 2 9am", (1, 2, 9, 0)),
])
def test_r7_time_parser(text, expect):
    now = datetime(2026, 11, 8, 18, 0, tzinfo=timezone.utc)
    got = parse_when(text, now)
    if text.startswith("Friday"):
        assert (got.weekday(), got.hour) == (4, 8)
    else:
        assert (got.month, got.day, got.hour, got.minute) == expect


# -- R17: redirects never carry credentials; truncated bodies are adapter errors, not crashes -------------------------
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from pit_crew.adapters.base import AdapterError
from pit_crew.adapters.http import json_request


def _serve(handler_cls):
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_r17_redirect_is_not_followed_and_token_not_forwarded():
    seen = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200); self.end_headers(); self.wfile.write(b"{}")

        def log_message(self, *a): pass

    target = _serve(Target)

    class Redirector(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/x"); self.end_headers()

        def log_message(self, *a): pass

    red = _serve(Redirector)
    with pytest.raises(AdapterError) as exc:
        json_request("GET", f"http://127.0.0.1:{red.server_port}/api", token="SECRET-TOKEN", what="HA")
    assert "redirect" in str(exc.value) and "SECRET-TOKEN" not in str(exc.value)
    assert seen == []
    red.shutdown(); target.shutdown()


def test_r17_incomplete_read_is_an_adapter_error():
    class Short(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "500"); self.end_headers()
            self.wfile.write(b'{"a":'); self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)

        def log_message(self, *a): pass

    srv = _serve(Short)
    with pytest.raises(AdapterError):
        json_request("GET", f"http://127.0.0.1:{srv.server_port}/", what="HA")
    srv.shutdown()


def test_r6_lease_is_exclusive(store):
    assert store.lease_acquire("w", "a", 60, now=1000)
    assert not store.lease_acquire("w", "b", 60, now=1010)
    assert store.lease_acquire("w", "a", 60, now=1020)
    assert store.lease_acquire("w", "b", 60, now=1100)       # expired -> taken over


def test_r18_bad_battery_size_is_an_error_not_a_crash():
    from pit_crew.brain import readiness as rd
    from pit_crew.brain.models import VehicleState
    st = VehicleState(name="T", source="t", captured_at=NOW)
    st.set("battery_pct", 50)
    dep = datetime.fromtimestamp(NOW + 36000, tz=timezone.utc)
    res = rd.evaluate(st, dep, datetime.fromtimestamp(NOW, tz=timezone.utc), trip_mi=10, battery_kwh=0)
    assert res["ok"] is False and "battery" in res["error"]


def test_r13_default_zone_is_a_real_zone_when_the_os_names_one():
    import os
    from zoneinfo import ZoneInfo
    from pit_crew.service import _system_zone
    tz = _system_zone()
    assert tz is not None
    if "zoneinfo/" in os.path.realpath("/etc/localtime"):
        assert isinstance(tz, ZoneInfo)        # DST-aware, not today's fixed offset
