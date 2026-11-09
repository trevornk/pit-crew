"""Built-in adapters. Importing this package registers them with the adapter registry."""

from __future__ import annotations

from . import base
from .homeassistant import build_homeassistant
from .simulator import build_simulator, build_simulator_history
from .tesla_direct import build_tesla
from .teslamate import build_teslamate_history, build_teslamate_live


def register_builtin_adapters() -> None:
    """Idempotent registration of the adapters that ship with the plugin."""
    if "simulator" not in base._ADAPTERS:
        base.register_adapter("simulator", build_simulator)
        base.register_history("simulator", build_simulator_history)
    if "homeassistant" not in base._ADAPTERS:
        base.register_adapter("homeassistant", build_homeassistant)
    if "tesla" not in base._ADAPTERS:
        base.register_adapter("tesla", build_tesla)
    if "teslamate" not in base._ADAPTERS:
        base.register_adapter("teslamate", build_teslamate_live)
        base.register_history("teslamate", build_teslamate_history)


register_builtin_adapters()
