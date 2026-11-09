"""Single source of truth for the plugin's identity.

Renaming the plugin means changing the values here, ``plugin.yaml`` (``name``), the
``desktop/plugin.js`` id and ``dashboard/manifest.json`` — nothing else hard-codes them.
"""

from __future__ import annotations

PLUGIN_ID = "pit-crew"
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
