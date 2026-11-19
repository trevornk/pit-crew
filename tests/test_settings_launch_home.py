"""The settings provider reads the launch home, not the profile scope bound when the plugin registered.

Regression: a multiplex gateway loaded plugins under a transient profile's scope; when that profile was deleted every
settings read raised "Named profile home does not exist" and silently fell back to defaults, on every poll tick.
Needs real Hermes modules (skipped without them)."""

import logging

import pytest

pytest.importorskip("hermes_scope")
pytest.importorskip("hermes_cli.config")

from pit_crew import settings as S  # noqa: E402

PLUGIN_SETTING = {"plugins": {"entries": {"pit-crew": {"settings": {"poll_interval_s": 123}}}}}


@pytest.fixture
def homes(tmp_path, monkeypatch):
    import yaml
    from hermes_constants import reset_hermes_home_key_cache

    root = tmp_path / ".hermes"
    launch = root
    ghost = root / "profiles" / "ghost"
    ghost.mkdir(parents=True)
    (ghost / "config.yaml").write_text("plugins: {}\n")
    (launch / "config.yaml").write_text(yaml.safe_dump(PLUGIN_SETTING))
    monkeypatch.setenv("HERMES_HOME", str(launch))
    reset_hermes_home_key_cache()
    return launch, ghost


def _delete(ghost):
    for f in ghost.iterdir():
        f.unlink()
    ghost.rmdir()


def test_reads_launch_home_after_registering_profile_is_deleted(homes, caplog):
    from hermes_scope import push_state

    launch, ghost = homes
    provider = S.hermes_settings_provider()
    token = push_state(home=str(ghost))  # registered/started under the transient profile's scope
    try:
        assert provider() == {"poll_interval_s": 123}
        _delete(ghost)
        with caplog.at_level(logging.WARNING, logger=S.logger.name):
            for _ in range(5):
                assert provider() == {"poll_interval_s": 123}
        assert not [r for r in caplog.records if "could not read" in r.getMessage()]
    finally:
        token.reset()


def test_unreadable_launch_home_warns_once_then_recovers(homes, caplog):
    launch, _ = homes
    cfg = launch / "config.yaml"
    body = cfg.read_text()
    cfg.write_text("plugins: [unclosed\n")
    provider = S.hermes_settings_provider()
    with caplog.at_level(logging.INFO, logger=S.logger.name):
        results = [provider() for _ in range(6)]
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert all(r == {} for r in results) or len(warnings) <= 1
        assert len(warnings) <= 1
        cfg.write_text(body + "\n# touched\n")
        assert provider() == {"poll_interval_s": 123}
