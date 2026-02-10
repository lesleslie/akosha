"""Akosha operational modes system.

Provides different operational modes for Akosha:
- Lite mode: Zero external dependencies, in-memory only
- Standard mode: Full production configuration with Redis and cloud storage
"""

from __future__ import annotations

from typing import Any

from akosha.modes.base import BaseMode, ModeConfig
from akosha.modes.lite import LiteMode
from akosha.modes.standard import StandardMode

# Mode registry
_MODE_REGISTRY: dict[str, type[BaseMode]] = {
    "lite": LiteMode,
    "standard": StandardMode,
}


def get_mode(mode_name: str, config: dict[str, Any]) -> BaseMode:
    """Get mode instance by name.

    Args:
        mode_name: Name of the mode (lite, standard)
        config: Configuration dictionary for the mode

    Returns:
        Mode instance

    Raises:
        ValueError: If mode name is not recognized

    Examples:
        >>> mode = get_mode("lite", {})
        >>> mode.mode_config.name
        'lite'

        >>> mode = get_mode("standard", {"redis_host": "localhost"})
        >>> mode.mode_config.redis_enabled
        True
    """
    mode_class = _MODE_REGISTRY.get(mode_name.lower())
    if not mode_class:
        valid_modes = ", ".join(_MODE_REGISTRY.keys())
        raise ValueError(
            f"Unknown mode: {mode_name}. Valid modes: {valid_modes}"
        )
    return mode_class(config=config)


def list_modes() -> list[str]:
    """List all available mode names.

    Returns:
        List of mode names

    Examples:
        >>> list_modes()
        ['lite', 'standard']
    """
    return list(_MODE_REGISTRY.keys())


__all__ = [
    "BaseMode",
    "ModeConfig",
    "LiteMode",
    "StandardMode",
    "get_mode",
    "list_modes",
]
