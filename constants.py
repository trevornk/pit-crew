"""Single source of truth for the plugin's identity.

Renaming the plugin means changing the values here, ``plugin.yaml`` (``name``), the
``desktop/plugin.js`` id and ``dashboard/manifest.json`` — nothing else hard-codes them.
"""

from __future__ import annotations

import re
from pathlib import Path

def _read_version() -> str:
    """The running plugin's version, read from plugin.yaml (the one place it is written down)."""
    try:
        text = (Path(__file__).resolve().parent / "plugin.yaml").read_text(encoding="utf-8")
        m = re.search(r"^version:\s*[\"']?([^\s\"'#]+)", text, re.MULTILINE)
        return m.group(1) if m else "unknown"
    except OSError:
        return "unknown"


PLUGIN_ID = "pit-crew"
VERSION = _read_version()
DISPLAY_NAME = "Pit Crew"
SLASH_COMMAND = "car"
TOOLSET = "pit_crew"

# Secrets are read through Hermes' secret scope under these names (declared in plugin.yaml).
ENV_HA_TOKEN = "PIT_CREW_HA_TOKEN"
ENV_TESLAMATE_TOKEN = "PIT_CREW_TESLAMATE_TOKEN"
ENV_TESLA_TOKEN = "PIT_CREW_TESLA_TOKEN"                    # Teslemetry / Tessie static API token

# Entry-point group third parties use to ship adapters (see docs/ADAPTERS.md).
ADAPTER_ENTRY_POINT_GROUP = "pit_crew.adapters"

DB_FILENAME = "data.db"
