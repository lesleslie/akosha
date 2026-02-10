"""Base mode interface for Akosha operational modes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class ModeConfig(BaseModel):
    """Configuration for a specific operational mode.

    Attributes:
        name: Mode name (lite, standard)
        description: Human-readable description
        redis_enabled: Whether Redis cache is enabled
        cold_storage_enabled: Whether cold storage is enabled
        cache_backend: Cache backend type (memory, redis)
    """

    name: str
    description: str
    redis_enabled: bool
    cold_storage_enabled: bool
    cache_backend: str


class BaseMode(ABC):
    """Base class for operational modes.

    Each mode defines how Akosha initializes its components:
    - Cache layer (in-memory or Redis)
    - Cold storage (disabled or cloud storage)
    - External service dependencies

    Attributes:
        config: Mode configuration dictionary
        mode_config: Typed mode configuration
    """

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize mode with configuration.

        Args:
            config: Configuration dictionary for the mode
        """
        self.config = config
        self.mode_config = self.get_mode_config()

    @abstractmethod
    def get_mode_config(self) -> ModeConfig:
        """Get mode-specific configuration.

        Returns:
            ModeConfig instance with mode settings
        """
        pass

    @abstractmethod
    async def initialize_cache(self) -> Any:
        """Initialize cache layer for this mode.

        Returns:
            Cache client instance or None for in-memory cache

        Examples:
            >>> mode = LiteMode(config={})
            >>> cache = await mode.initialize_cache()
            >>> cache  # None for lite mode (in-memory)
        """
        pass

    @abstractmethod
    async def initialize_cold_storage(self) -> Any:
        """Initialize cold storage for this mode.

        Returns:
            Storage adapter instance or None if disabled

        Examples:
            >>> mode = LiteMode(config={})
            >>> storage = await mode.initialize_cold_storage()
            >>> storage  # None for lite mode (disabled)
        """
        pass

    @property
    @abstractmethod
    def requires_external_services(self) -> bool:
        """Check if mode requires external services.

        Returns:
            True if mode requires Redis, cloud storage, or other external services

        Examples:
            >>> mode = LiteMode(config={})
            >>> mode.requires_external_services
            False

            >>> mode = StandardMode(config={})
            >>> mode.requires_external_services
            True
        """
        pass

    def __repr__(self) -> str:
        """String representation of mode."""
        return f"Mode(name={self.mode_config.name}, services_required={self.requires_external_services})"
