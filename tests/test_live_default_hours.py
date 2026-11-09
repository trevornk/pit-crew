"""Live mode stays time-limited; its default length is the `live_default_hours` setting (1-24 h, default 1)."""

import pytest


# -- 5. the default live length is a setting (1-24 h) -------------------------------------------------------------------------
@pytest.mark.parametrize("value,expect", [(None, 1), (8, 8), (24, 24), (100, 24), (0, 1), ("abc", 1), (2.6, 3)])
def test_live_default_hours_setting(make_service, value, expect):
    svc = make_service({} if value is None else {"live_default_hours": value})
    assert svc.settings.get("live_default_hours") == expect
    assert svc.clamp_hours(None) == expect
    info = svc.arm(by="t")
    assert info["hours"] == expect


def test_old_arm_default_hours_setting_still_counts():
    from pit_crew.settings import Settings
    assert Settings(lambda: {"arm_default_hours": 6}).get("live_default_hours") == 6
    assert Settings(lambda: {"arm_default_hours": 6, "live_default_hours": 2}).get("live_default_hours") == 2


def test_explicit_hours_keep_their_own_limits_and_nothing_arms_by_itself(make_service):
    svc = make_service({"live_default_hours": 8})
    assert svc.armed_info()["armed"] is False                   # a setting never arms
    assert svc.clamp_hours(48) == 48 and svc.clamp_hours(1000) == 72


def test_page_go_live_uses_the_setting(make_service):
    from pit_crew.dashboard import plugin_api as api
    make_service({"live_default_hours": 8})
    assert api.summary()["armed"]["default_hours"] == 8
    out = api.arm({"confirmed": True})
    assert out["ok"] and out["hours"] == 8
    assert api.arm({})["ok"] is False                              # still needs the confirmation click


def test_manifest_declares_live_default_hours():
    from pathlib import Path

    import yaml
    manifest = yaml.safe_load((Path(__file__).resolve().parent.parent / "plugin.yaml").read_text())
    entry = manifest["config_schema"]["live_default_hours"]
    assert entry["default"] == 1 and "1 to 24" in entry["description"]
    assert "arm_default_hours" not in manifest["config_schema"]
