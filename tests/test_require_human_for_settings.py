"""require_human_for: defaults, cleaning, and migration from the old approval_confirmation setting."""

import pytest

from pit_crew.settings import APPROVAL_COMMANDS, LEGACY_STRICT_COMMANDS, Settings


def snap(raw):
    return Settings(provider=lambda: raw).get("require_human_for")


def test_default_follows_hermes():
    assert snap({}) == []


def test_explicit_strict_migrates_to_the_five_old_commands():
    assert snap({"approval_confirmation": "strict"}) == list(LEGACY_STRICT_COMMANDS)
    assert "honk" not in snap({"approval_confirmation": "strict"})


@pytest.mark.parametrize("raw", [{"approval_confirmation": "hermes_gate"}, {"approval_confirmation": ""}, {}])
def test_hermes_gate_or_unset_means_empty(raw):
    assert snap(raw) == []


def test_new_setting_wins_over_the_old_one():
    assert snap({"approval_confirmation": "strict", "require_human_for": ["honk"]}) == ["honk"]
    assert snap({"approval_confirmation": "strict", "require_human_for": []}) == []


def test_values_are_cleaned_to_known_approval_commands():
    assert snap({"require_human_for": ["Unlock", "lock", "bogus", "flash", "unlock"]}) == ["unlock", "flash"]
    assert snap({"require_human_for": "open_trunk, honk"}) == ["open_trunk", "honk"]
    assert set(APPROVAL_COMMANDS) == {"unlock", "open_trunk", "open_frunk", "vent_windows", "sentry_off", "honk", "flash"}
