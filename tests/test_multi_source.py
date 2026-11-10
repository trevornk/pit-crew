"""Connections: one command route + an optional TeslaMate state source next to ANY real command adapter (brief 15).

Home Assistant (every profile), direct Teslemetry and direct Tessie share ONE merge (adapters/state_sources.py). The
simulator is never overlaid; adapter=teslamate is read-only and refuses commands in plain words. Identity: a VIN
mismatch between the primary and TeslaMate stops the merge. Future stamps are quarantined.

Fixtures use FAKE VINs only (``FAKEVIN...``-style 17-character strings that are not real vehicles). No network beyond
the local fakes.
"""

import copy
import json
import logging

import pytest

from fakes import FIXTURES, FakeHA, FakeTeslaMate, load_fixture
from pit_crew.adapters import base, tesla_direct as td
from pit_crew.adapters.base import CommandNotSupported
from pit_crew.adapters.homeassistant import HomeAssistantAdapter, parse_ha_time
from pit_crew.adapters.profile import load_profile
from pit_crew.adapters.state_sources import FUTURE_SKEW_S, StateSources, normalize_vin, quarantine_future
from pit_crew.adapters.teslamate import READ_ONLY_REASON, TeslaMateClient, TeslaMateOverlay
from pit_crew.brain.models import FieldValue
from pit_crew.constants import ENV_HA_TOKEN, ENV_TESLA_TOKEN
from pit_crew.safety.approval import ApprovalEvidence

from test_fresh_state import NOW as HA_NOW, ScriptedTeslaMate, iso, live, stamp, LOCK, TRUNK

AUTO = ApprovalEvidence("hermes_approval", "approvals off", auto=True)
DATA = json.loads((FIXTURES.parent / "tesla_vehicle_data_synthetic.json").read_text())
NOW = 1_790_000_100.0            # the synthetic vehicle_data sections are stamped 1_790_000_000 (100 s old)
VIN_A = "FAKEVN0A000000001"      # fake, never a real car
VIN_B = "FAKEVN0B000000002"


class FakeTessieOrTeslemetry:
    """Stands in for LibraryClient (no tesla-fleet-api, no network)."""

    def __init__(self):
        self.state = "online"
        self.data = json.loads(json.dumps(DATA))
        self.calls = []

    def vehicle_state(self):
        self.calls.append("vehicle_state")
        return self.state

    def vehicle_data(self, with_location):
        self.calls.append("vehicle_data")
        return json.loads(json.dumps(self.data))

    def wake(self):
        self.calls.append("wake")

    def command(self, name, args):
        self.calls.append("command:" + name)
        return True, ""


class VinTeslaMate(ScriptedTeslaMate):
    """TeslaMateApi fake whose /api/v1/cars reports ``vin`` for car 1 (``None``: the fixture's REDACTED)."""

    def __init__(self, vin=None):
        super().__init__()
        self.vin = vin
        self.status["car_status"]["locked"] = True

    def handle(self, h, method, path, body):
        if method == "GET" and path == "/api/v1/cars":
            cars = copy.deepcopy(load_fixture("teslamate_cars.json"))
            if self.vin is not None:
                cars["data"]["cars"][0]["car_details"]["vin"] = self.vin
            return h._reply(200, cars)
        return super().handle(h, method, path, body)


@pytest.fixture
def tm():
    fake = VinTeslaMate()
    yield fake
    fake.stop()


def direct(make_service, clock, client, mode="teslemetry", vin=VIN_A, **settings):
    """A CarService on the REAL direct-adapter factory (build_tesla) with a fake library client."""
    kind = f"tesla-ms-{mode}"
    base.register_adapter(kind, lambda ctx: td.build_tesla(ctx, client_factory=lambda cfg: client), replace=True)
    clock.t = NOW
    cfg = {"adapter": kind, "history": "none", "tesla_mode": mode, "tesla_vin": vin}
    cfg.update(settings)
    svc = make_service(cfg, secrets={ENV_TESLA_TOKEN: "fake-token"})
    svc.arm(1, by="t")
    return svc


# == overlay next to each real adapter ================================================================================
@pytest.mark.parametrize("mode", ["teslemetry", "tessie"])
def test_direct_adapter_merges_newer_teslamate_reading_per_field(make_service, clock, tm, mode):
    c = FakeTessieOrTeslemetry()
    c.data["response"]["vehicle_state"]["locked"] = False            # the service's reading: 100 s old
    svc = direct(make_service, clock, c, mode=mode, teslamate_url=tm.url)
    st = svc.get_state(force=True)
    assert st.value("locked") is True and st.fields["locked"].source == "teslamate"   # TeslaMate: now - 60
    assert st.fields["locked"].estimated                                               # (the awake-car estimate)
    assert st.fields["battery_pct"].source in (mode, "teslamate")
    assert st.fields["range_mi"].source in (mode, "teslamate")
    out = svc.status(force=True)
    assert out["adapter"]["state_sources"] == [mode, "teslamate"]
    assert out["adapter"]["fresher_state_from"] == "teslamate (read-only)"
    assert out["freshness"]["field_sources"]["locked"] == "teslamate"
    assert "no per-field times" in out["freshness"]["source_note"]
    # the adapter's own cache holds the service's reading only
    assert svc.adapter._cache.fields["locked"].value is False and svc.adapter._cache.fields["locked"].source is None
    assert all(r[0] == "GET" for r in tm.requests)


def test_direct_adapter_newer_own_reading_wins_and_is_labelled(make_service, clock, tm):
    c = FakeTessieOrTeslemetry()
    c.data["response"]["vehicle_state"].update(locked=False, timestamp=int((NOW - 5) * 1000))
    svc = direct(make_service, clock, c, teslamate_url=tm.url)
    st = svc.get_state(force=True)
    assert st.value("locked") is False and st.fields["locked"].source == "teslemetry"


def test_without_teslamate_direct_adapter_is_unchanged(make_service, clock):
    c = FakeTessieOrTeslemetry()
    svc = direct(make_service, clock, c)
    st = svc.get_state(force=True)
    assert all(f.source is None for f in st.fields.values())
    assert svc.describe_adapter()["state_sources"] == ["teslemetry"]
    assert "fresher_state_from" not in svc.describe_adapter()


def test_home_assistant_teslemetry_profile_gets_the_same_overlay(tm):
    """'All profiles': the HA teslemetry integration profile with TeslaMate next to it."""
    from test_ha_tesla_integrations import NOW as T_NOW, states
    sts = copy.deepcopy(states("teslemetry"))
    profile = load_profile("teslemetry_ha")
    lock_eid = profile["fields"]["locked"]["entity"].replace("{slug}", "demo_teslemetry")
    for s in sts:
        if s["entity_id"] == lock_eid:
            s["state"] = "unlocked"
            s["last_reported"] = s["last_updated"] = iso(T_NOW - 3600)
    ha = FakeHA(sts, apply_effects=False)
    try:
        sources = StateSources("home_assistant", TeslaMateOverlay(TeslaMateClient(tm.url)))
        a = HomeAssistantAdapter(ha.url, FakeHA.TOKEN, profile, "demo_teslemetry", sources=sources)
        st = a.fetch_state(T_NOW)
    finally:
        ha.stop()
    assert st.value("locked") is True and st.fields["locked"].source == "teslamate"
    assert a.describe()["state_sources"] == ["home_assistant", "teslamate"]


def test_home_assistant_status_lists_its_state_sources(make_service, clock, tm):
    ha = FakeHA()
    try:
        assert live(make_service, ha, clock).status(force=True)["adapter"]["state_sources"] == ["home_assistant"]
        svc = live(make_service, ha, clock, teslamate_url=tm.url)
        assert svc.status(force=True)["adapter"]["state_sources"] == ["home_assistant", "teslamate"]
    finally:
        ha.stop()


def test_simulator_is_never_overlaid(make_service, clock, tm):
    svc = make_service({"adapter": "simulator", "teslamate_url": tm.url, "history": "none"})
    st = svc.get_state(force=True)
    svc.status(force=True)
    assert all(f.source is None for f in st.fields.values())
    assert svc.describe_adapter()["state_sources"] == ["simulator"]
    assert svc.adapter.state_sources is None
    assert not [r for r in tm.requests if "/status" in r[1]]                 # TeslaMate was never even asked


# == overlay outage / motion ==========================================================================================
def test_direct_adapter_with_teslamate_down_reads_the_service_only(make_service, clock, tm):
    tm.down = True
    c = FakeTessieOrTeslemetry()
    svc = direct(make_service, clock, c, mode="tessie", teslamate_url=tm.url)
    st = svc.get_state(force=True)
    assert st.value("locked") is True and st.fields["locked"].source == "tessie"
    note = st.to_public(now=clock.t)["freshness"]["source_note"]
    assert "TeslaMate could not be read" in note and "Tessie's only" in note
    assert svc.adapter.state_sources.overlay_ok is False


def test_conflicting_motion_teslamate_driving_blocks_the_trunk_on_a_direct_adapter(make_service, clock, tm):
    c = FakeTessieOrTeslemetry()
    c.data["response"]["drive_state"].update(shift_state="P", speed=0, timestamp=int((NOW - 2) * 1000))
    tm.status["driving_details"].update(shift_state="D", speed=30)
    tm.status["state"] = "driving"
    svc = direct(make_service, clock, c, teslamate_url=tm.url)
    st = svc.get_state(force=True)
    assert st.motion() is True and st.fields["moving"].source == "teslamate"
    res = svc.run_command("open_trunk", {}, surface="tool", approval=AUTO)
    assert res["status"] == "blocked" and "command:open_trunk" not in c.calls


def test_conflicting_motion_service_driving_wins_over_teslamate_parked(make_service, clock, tm):
    c = FakeTessieOrTeslemetry()
    c.data["response"]["drive_state"].update(shift_state="D", speed=20, timestamp=int((NOW - 900) * 1000))
    svc = direct(make_service, clock, c, teslamate_url=tm.url)            # TeslaMate: parked, newer
    st = svc.get_state(force=True)
    assert st.motion() is True and st.value("shift_state") == "D"


# == read-back with the overlay on a direct adapter ===================================================================
def test_direct_readback_confirms_from_teslamate_only_after_the_baseline(make_service, clock, tm):
    c = FakeTessieOrTeslemetry()
    c.data["response"]["vehicle_state"]["locked"] = True                 # the service never re-reports in this test
    tm.status["car_status"]["locked"] = True
    looks = []
    real = tm.handle

    def handle(h, method, path, body):
        if "/status" in path:
            looks.append(path)
            if len(looks) >= 3:                       # preflight, baseline right after the send, then the change
                tm.status["car_status"]["locked"] = False
        return real(h, method, path, body)
    tm.handle = handle
    svc = direct(make_service, clock, c, teslamate_url=tm.url, verify_timeout_s=0, verify_interval_s=1)
    orig = c.command

    def command(name, args):
        clock.advance(1)
        return orig(name, args)
    c.command = command
    res = svc.run_command("unlock", {}, surface="tool", approval=AUTO)
    assert "command:unlock" in c.calls
    assert res["readback"]["status"] == "confirmed", res
    assert svc._last_state.fields["locked"].source == "teslamate"


def test_direct_readback_never_confirms_from_a_teslamate_value_seen_at_the_baseline(make_service, clock, tm):
    c = FakeTessieOrTeslemetry()
    tm.status["car_status"]["locked"] = True
    svc = direct(make_service, clock, c, teslamate_url=tm.url, verify_timeout_s=0, verify_interval_s=1)
    orig = c.command

    def command(name, args):
        clock.advance(1)
        tm.status["car_status"]["locked"] = False                         # already visible at the baseline look
        return orig(name, args)
    c.command = command
    res = svc.run_command("unlock", {}, surface="tool", approval=AUTO)
    assert res["readback"]["status"] != "confirmed"


# == identity =========================================================================================================
def test_vin_mismatch_stops_the_merge_and_names_the_car_id_setting(make_service, clock, caplog):
    fake = VinTeslaMate(vin=VIN_B)
    try:
        caplog.set_level(logging.DEBUG)
        c = FakeTessieOrTeslemetry()
        c.data["response"]["vehicle_state"]["locked"] = False
        svc = direct(make_service, clock, c, vin=VIN_A, teslamate_url=fake.url)
        st = svc.get_state(force=True)
        assert st.value("locked") is False and st.fields["locked"].source == "teslemetry"
        assert not any(f.source == "teslamate" for f in st.fields.values())
        out = svc.status(force=True)
        note = out["freshness"]["source_note"]
        assert "teslamate_car_id" in note and "different car" in note
        assert "teslamate_car_id" in out["adapter"]["state_sources_note"]
        assert not [r for r in fake.requests if "/status" in r[1]]         # TeslaMate's state was not even read
        assert svc.adapter.state_sources.overlay_ok is False
        dump = json.dumps(out) + caplog.text
        assert VIN_A not in dump and VIN_B not in dump                     # never logged or shown
        assert len([r for r in fake.requests if r[1] == "/api/v1/cars"]) == 1   # looked up once
        svc.run_command("lock", {}, surface="tool", approval=AUTO)
        assert "command:lock" in c.calls                                   # commands still go through the service
        assert not [r for r in fake.requests if "/status" in r[1]]         # and no baseline look at the other car
    finally:
        fake.stop()


def test_matching_or_unknown_vin_merges(make_service, clock):
    for vin in (VIN_A.lower(), None):                                     # same car (any case) / REDACTED in TeslaMate
        fake = VinTeslaMate(vin=vin)
        try:
            c = FakeTessieOrTeslemetry()
            c.data["response"]["vehicle_state"]["locked"] = False
            svc = direct(make_service, clock, c, vin=VIN_A, teslamate_url=fake.url)
            st = svc.get_state(force=True)
            assert st.fields["locked"].source == "teslamate", vin
        finally:
            fake.stop()


def test_home_assistant_has_no_vin_so_no_identity_lookup(make_service, clock, tm):
    ha = FakeHA()
    try:
        svc = live(make_service, ha, clock, teslamate_url=tm.url)
        svc.get_state(force=True)
        assert not [r for r in tm.requests if r[1] == "/api/v1/cars"]
    finally:
        ha.stop()


def test_normalize_vin():
    assert normalize_vin(" fakevn0a000000001 ") == VIN_A
    assert normalize_vin("REDACTED") is None and normalize_vin("") is None and normalize_vin(None) is None
    assert normalize_vin("IIIIIIIIIIIIIIIII") is None                    # I/O/Q never appear in a VIN


# == future timestamps ================================================================================================
def test_quarantine_future_clamps_small_skew_and_ignores_the_rest():
    now = 1000.0
    fields = {"locked": FieldValue(True, now + 30), "battery_pct": FieldValue(50, now + FUTURE_SKEW_S + 1),
              "shift_state": FieldValue("D", now + 3600), "speed_mph": FieldValue(0.0, now + 3600),
              "range_mi": FieldValue(200, now - 10)}
    held = quarantine_future(fields, now)
    assert sorted(held) == ["battery_pct", "shift_state", "speed_mph"]
    assert fields["locked"].updated_at == now
    assert not fields["battery_pct"].available and not fields["speed_mph"].available
    assert fields["shift_state"].value == "D" and fields["shift_state"].updated_at == now and fields["shift_state"].estimated
    assert fields["range_mi"].updated_at == now - 10
    assert quarantine_future(fields, now) == []                            # idempotent


def test_home_assistant_future_stamp_is_ignored_with_a_note(make_service, clock):
    ha = FakeHA()
    try:
        for eid in ha.states:
            stamp(ha, eid, HA_NOW - 300)
        ha.set_state(LOCK, "unlocked")
        stamp(ha, LOCK, HA_NOW + 3600)                                     # a broken clock: an hour ahead
        stamp(ha, TRUNK, HA_NOW + 30)                                      # ordinary skew
        svc = live(make_service, ha, clock)
        st = svc.get_state(force=True)
        assert not st.fields["locked"].available
        assert st.fields["trunk_open"].updated_at == HA_NOW
        note = st.to_public(now=clock.t)["freshness"].get("source_note") or st.hints["source_note"]
        assert "in the future" in note and "locked" in note
        res = svc.run_command("lock", {}, surface="tool", approval=AUTO)
        assert "already" not in res.get("readback", {})
    finally:
        ha.stop()


def test_direct_section_time_in_the_future_is_ignored_but_moving_is_kept(make_service, clock):
    c = FakeTessieOrTeslemetry()
    c.data["response"]["vehicle_state"]["timestamp"] = int((NOW + 7200) * 1000)
    c.data["response"]["drive_state"].update(shift_state="D", speed=25, timestamp=int((NOW + 7200) * 1000))
    svc = direct(make_service, clock, c)
    st = svc.get_state(force=True)
    assert not st.fields["locked"].available
    assert st.motion() is True and st.fields["shift_state"].estimated


def test_teslamate_future_state_since_is_not_a_real_time(make_service, clock, tm):
    tm.status["state"] = "asleep"
    tm.status["state_since"] = iso(NOW + 3600)
    c = FakeTessieOrTeslemetry()
    svc = direct(make_service, clock, c, teslamate_url=tm.url)
    st = svc.get_state(force=True)
    tm_fields = [f for f in st.fields.values() if f.source == "teslamate"]
    assert all(f.updated_at is None or f.updated_at <= NOW for f in st.fields.values() if f.available)
    assert all(f.estimated for f in tm_fields)
    assert "state_since was more than" in st.hints["source_note"]


def test_direct_section_without_a_car_time_is_an_estimate(make_service, clock):
    c = FakeTessieOrTeslemetry()
    c.data["response"]["vehicle_state"].pop("timestamp")
    c.data["response"]["vehicle_state"]["locked"] = False
    svc = direct(make_service, clock, c, zone_rule="anywhere")           # (no zones configured in this test)
    st = svc.get_state(force=True)
    assert st.fields["locked"].estimated and st.fields["locked"].updated_at == NOW
    assert not st.fields["battery_pct"].estimated
    res = svc.run_command("unlock", {}, surface="tool", approval=AUTO)
    assert "command:unlock" in c.calls and "already" not in res.get("readback", {}), res["message"]
    assert res["readback"]["status"] != "confirmed"          # the estimated pre-command "unlocked" is no proof


# == TeslaMate standalone: read-only ==================================================================================
def test_teslamate_only_refuses_commands_in_plain_words(make_service, clock, tm):
    svc = make_service({"adapter": "teslamate", "teslamate_url": tm.url, "history": "none"})
    svc.arm(1, by="t")
    res = svc.run_command("unlock", {}, surface="tool", approval=AUTO)
    assert res["ok"] is False and res["status"] == "unsupported"
    assert READ_ONLY_REASON in res["message"]
    assert "Home Assistant, Teslemetry or Tessie under Which car" in READ_ONLY_REASON
    with pytest.raises(CommandNotSupported, match="read-only"):
        svc.adapter.send_command("unlock", {})
    assert all(r[0] == "GET" for r in tm.requests)
    assert svc.describe_adapter()["state_sources"] == ["teslamate"]
    assert svc.adapter.state_sources is None
