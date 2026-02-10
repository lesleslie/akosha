"""Lite mode: In-memory only, zero external dependencies."""

from __future__ import annotations

import logging
from typing import Any

from akosha.modes.base import BaseMode, ModeConfig

logger = logging.getLogger(__name__)


class LiteMode(BaseMode):
    """Lite mode with zero external dependencies.

    Lite mode characteristics:
    - No Redis cache (in-memory only)
    - No cold storage (data lost on restart)
    - Fastest startup time
    - Ideal for development and testing
    - Limited scalability

    Examples:
        >>> mode = LiteMode(config={})
        >>> mode.mode_config.name
        'lite'
        >>> mode.requires_external_services
        False
    """

    def get_mode_config(self) -> ModeConfig:
        """Get lite mode configuration.

        Returns:
            ModeConfig with lite mode settings
        """
        return ModeConfig(
            name="lite",
            description="Lite mode: In-memory only, zero external dependencies",
            redis_enabled=False,
            cold_storage_enabled=False,
            cache_backend="memory",
        )

    async def initialize_cache(self) -> Any:
        """Initialize in-memory cache (no Redis).

        Returns:
            None (indicates in-memory cache should be used)

        Note:
            Lite mode uses Python's built-in dict for caching,
            which is sufficient for development and testing.
        """
        logger.info("Lite mode: Using in-memory cache (no Redis)")
        return None

    async def initialize_cold_storage(self) -> Any:
        """Initialize cold storage (disabled in lite mode).

        Returns:
            None (cold storage disabled)

        Note:
            Cold storage is disabled in lite mode. All data
            is lost when the application restarts.
        """
        logger.info("Lite mode: Cold storage disabled")
        return None

    @property
    def requires_external_services(self) -> bool:
        """Lite mode requires no external services.

        Returns:
            False (no external services needed)
        """
        return False

    def __repr__(self) -> str:
        """String representation of lite mode."""
        return "LiteMode(services_required=False, cache=in-memory, cold_storage=disabled)"
