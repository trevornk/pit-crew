import json

from pit_crew import tools


def test_car_status_simulator_default(service):
    out = json.loads(tools.car_status({}))
    assert out["ok"] and out["vehicle"]["simulated"] is True
    assert out["adapter"]["label"] == "Simulated car"
    assert "battery" in out and out["battery"]["battery_pct"] > 0
    assert "summary" in out and "%" in out["summary"]
    assert out["safety"]["armed"] is False
    assert "unlock" in out["can_do"]
    assert "latitude" not in json.dumps(out)


def test_car_status_never_raises_on_bad_adapter(make_service):
    svc = make_service({"adapter": "nope"})
    out = json.loads(tools.car_status({}))
    assert out["ok"] is False and "error" in out


def test_misconfigured_adapter_is_reported_cleanly_everywhere(make_service):
    import importlib.util
    from pathlib import Path

    from pit_crew import slash

    svc = make_service({"adapter": "homeassistant", "ha_url": "http://127.0.0.1:9"})   # no token, no slug
    out = json.loads(tools.car_status({}))
    assert out["ok"] is False and "token" in out["error"] and out["adapter"]["label"] == "not connected"
    assert "isn't connected to your car yet" in slash.handle("")
    pytest = __import__("pytest")
    fastapi = pytest.importorskip("fastapi")
    spec = importlib.util.spec_from_file_location("car_api_x", Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    s = mod.summary()
    assert s["ok"] is False and s["adapter"]["label"] == "not connected" and s["simulated"] is False
    assert json.loads(tools.car_command({"command": "lock"}))["status"] in ("blocked", "unsupported", "invalid")
