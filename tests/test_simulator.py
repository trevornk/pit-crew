import json

import pytest

from pit_crew.adapters.base import AdapterError
from pit_crew.adapters.simulator import SimulatorAdapter, SimulatorHistory
from pit_crew.brain.commands import COMMANDS
from pit_crew.brain.models import FIELD_NAMES


@pytest.fixture
def sim(store, clock):
    return SimulatorAdapter(store=store, seed=7, events_enabled=False, wall_clock=clock)


def test_sim_declares_everything(sim):
    caps = sim.capabilities()
    assert caps.state_fields == frozenset(FIELD_NAMES) and caps.commands == frozenset(COMMANDS)
    assert sim.simulated and sim.label == "Simulated car"


def test_initial_state_is_plausible(sim, clock):
    st = sim.fetch_state(clock())
    assert st.simulated
    assert 50 < st.value("battery_pct") < 70
    assert st.value("charging_state") == "charging"
    assert st.value("locked") is True and st.value("location_zone") == "home"
    assert st.value("doors_open") == [] and st.value("charge_eta_min") > 0
    assert "latitude" not in json.dumps(st.to_public())


def test_charging_adds_soc_until_limit(sim, clock):
    before = sim.fetch_state(clock()).value("battery_pct")
    sim.control("advance", minutes=60)
    mid = sim.fetch_state(clock()).value("battery_pct")
    assert mid > before + 5  # ~6.6 kW for an hour ≈ +8%
    sim.control("advance", minutes=60 * 12)
    end = sim.fetch_state(clock())
    assert end.value("battery_pct") == pytest.approx(80.0, abs=0.2)
    assert end.value("charging_state") == "complete"


def test_climate_moves_cabin_temp(sim, clock):
    sim.send_command("climate_on", {"temp_f": 72})
    t0 = sim.fetch_state(clock()).value("cabin_temp_f")
    sim.control("advance", minutes=10)
    st = sim.fetch_state(clock())
    assert st.value("climate_on") and abs(st.value("cabin_temp_f") - 72) < abs(t0 - 72) + 0.01
    assert st.value("target_temp_f") == 72


def test_drive_uses_energy_and_leaves_home(sim, clock):
    sim.control("set_soc", percent=60)
    sim.control("drive", miles=30, to="work")
    st = sim.fetch_state(clock())
    assert st.value("location_zone") == "work" and st.value("plugged_in") is False
    assert st.value("battery_pct") == pytest.approx(60 - 30 * 0.265 / 75 * 100, abs=2.0)
    assert st.value("charging_state") == "disconnected"


def test_commands_change_state_and_unplugged_charge_fails(sim, clock):
    sim.send_command("unlock", {})
    sim.send_command("open_frunk", {})
    sim.send_command("vent_windows", {})
    st = sim.fetch_state(clock())
    assert st.value("locked") is False and st.value("frunk_open") is True
    assert len(st.value("windows_open")) == 4
    sim.control("unplug")
    with pytest.raises(AdapterError):
        sim.send_command("charge_start", {})


def test_events_are_seeded_and_fire(store, clock):
    a = SimulatorAdapter(store=store, seed=11, events_enabled=True, wall_clock=clock)
    first = [e["kind"] for e in a._load()["events"]]
    assert 3 <= len(first) <= 5
    a.control("advance", minutes=60 * 100)
    assert a._load()["events"] == []  # all fired
    # seeded: a second fresh store with the same seed yields the same schedule
    from pit_crew.store import open_store
    import tempfile, pathlib
    s2 = open_store(pathlib.Path(tempfile.mkdtemp()) / "d.db")
    b = SimulatorAdapter(store=s2, seed=11, events_enabled=True, wall_clock=clock)
    assert [e["kind"] for e in b._load()["events"]] == first
    s2.close()


def test_state_persists_across_adapter_instances(store, clock):
    a = SimulatorAdapter(store=store, seed=3, events_enabled=False, wall_clock=clock)
    a.send_command("unlock", {})
    b = SimulatorAdapter(store=store, seed=3, events_enabled=False, wall_clock=clock)
    assert b.fetch_state(clock()).value("locked") is False


def test_sleep_freezes_freshness(sim, clock):
    with pytest.raises(AdapterError):
        sim.control("sleep")  # charging cars stay awake
    sim.send_command("charge_stop", {})
    sim.control("sleep")
    t0 = sim.fetch_state(clock()).fields["battery_pct"].updated_at
    clock.advance(45 * 60)
    st = sim.fetch_state(clock())
    assert st.value("asleep") is True
    assert st.age_s("battery_pct", clock()) >= 44 * 60 and st.fields["battery_pct"].updated_at == t0


def test_unknown_action(sim):
    with pytest.raises(AdapterError):
        sim.control("explode")


def test_history_is_seeded_and_consistent(clock):
    h = SimulatorHistory(seed=5, days=30, now=clock)
    d1 = h.drives(0, 1e12)
    d2 = SimulatorHistory(seed=5, days=30, now=clock).drives(0, 1e12)
    assert d1 and [x.miles for x in d1] == [x.miles for x in d2]
    assert all(x.wh_per_mi and 200 < x.wh_per_mi < 450 for x in d1)
    assert h.charges(0, 1e12) and h.battery_health().health_pct > 90
