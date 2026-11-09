import pytest

from pit_crew.brain.commands import (COMMANDS, ArgError, canonical_args, command_catalog, get_spec,
                                       validate_args)


def test_risk_classes_and_approval_requirements():
    assert get_spec("lock").requires_approval is False and get_spec("lock").safe_direction
    for risky in ("unlock", "open_trunk", "open_frunk", "vent_windows", "sentry_off", "honk", "flash"):
        assert get_spec(risky).requires_approval, risky
    for comfort in ("climate_on", "set_temp", "charge_limit", "charge_start", "charge_stop", "seat_heat"):
        assert get_spec(comfort).risk == "comfort" and not get_spec(comfort).requires_approval
    assert get_spec("honk").risk == "attention"


def test_validate_bounds():
    spec = get_spec("set_temp")
    assert validate_args(spec, {"temp_f": "72"}) == {"temp_f": 72.0}
    with pytest.raises(ArgError):
        validate_args(spec, {"temp_f": 120})
    with pytest.raises(ArgError):
        validate_args(spec, {})
    with pytest.raises(ArgError):
        validate_args(get_spec("charge_limit"), {"percent": 20})
    assert validate_args(get_spec("charge_limit"), {"percent": 85.4}) == {"percent": 85}
    with pytest.raises(ArgError):
        validate_args(get_spec("charge_amps"), {"amps": 400})


def test_unknown_args_rejected_but_flags_pass():
    spec = get_spec("unlock")
    with pytest.raises(ArgError):
        validate_args(spec, {"bogus": 1})
    assert validate_args(spec, {"override_zone": "true"}) == {"override_zone": True}


def test_enum_and_bool_coercion():
    assert validate_args(get_spec("seat_heat"), {"seat": "Rear Left", "level": 2}) == {"seat": "rear_left", "level": 2}
    with pytest.raises(ArgError):
        validate_args(get_spec("seat_heat"), {"seat": "roof", "level": 1})
    assert validate_args(get_spec("defrost"), {"on": "off"}) == {"on": False}
    assert validate_args(get_spec("defrost"), {}) == {"on": True}


def test_canonical_args_stable():
    assert canonical_args({"b": 1, "a": 2}) == canonical_args({"a": 2, "b": 1})


def test_catalog_covers_all():
    assert {c["command"] for c in command_catalog()} == set(COMMANDS)



def test_sim_usage_lists_park_home_and_plug_in_and_desktop_install_is_documented(service):
    from pit_crew import slash
    usage = slash.handle("sim")
    assert "park_home" in usage and "plug_in" in usage and "unlock" in usage
    assert "/car sim" in slash.HELP
    out = slash.handle("desktop-install")
    assert "another machine" in out.lower() or "ANOTHER machine" in out
    assert "desktop-plugins" in out and "hermes://plugin/install?repo=trevornk/pit-crew" in out


def test_readme_quickstart_runs_as_written(service):
    """P0-2: the documented /car sim + unlock sequence must behave exactly as the README says."""
    from pit_crew import slash
    for line in ("sim drive 25 away", "sim park_home", "sim plug_in", "sim advance 90"):
        assert slash.handle(line).startswith("Simulator:"), line
    # ... and the unlock lines that follow are a real dry run, then a live (simulated) command awaiting Hermes' approval
    res = service.run_command("unlock", {}, surface="test", session="s")
    assert res["status"] == "dry_run" and "NOT contacted" in res["message"]
    assert slash.handle("sim advance 90").startswith("Simulator:")
    service.arm(1, by="t")
    res = service.run_command("unlock", {}, surface="test", session="s")
    assert res["status"] == "blocked" and "no confirmed human approval" in res["message"]   # only Hermes' prompt can supply it


def test_readme_try_it_sequence_runs_as_written(service):
    """The README's 'Try it in 60 seconds' sequence: unlocked + away -> alert -> /car lock -> live -> sent."""
    from pit_crew import slash
    for line in ("sim leave_unlocked", "sim drive 10 away", "sim advance 15"):
        assert slash.handle(line).startswith("Simulator:"), line
    service.watcher.poll_once(force=True)
    service.adapter.control("advance", minutes=12)
    service.watcher.poll_once(force=True)
    assert "unlocked and away from home" in slash.handle("alerts")
    assert "NOT contacted" in slash.handle("lock")                     # practice mode
    assert "Live for 1 h" in slash.handle("live 1 confirm")
    out = slash.handle("lock")
    assert "Sent lock" in out and "confirmed" in out
    assert "Back to practice mode" in slash.handle("practice")
