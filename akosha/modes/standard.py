"""Standard mode: Full production configuration with Redis and cloud storage."""

from __future__ import annotations

import logging
from typing import Any

from akosha.modes.base import BaseMode, ModeConfig

logger = logging.getLogger(__name__)


class StandardMode(BaseMode):
    """Standard mode with full production features.

    Standard mode characteristics:
    - Redis cache for improved performance
    - Cloud storage for cold tier (S3/Azure/GCS)
    - Production-ready scalability
    - Persistent storage
    - Requires external services

    Graceful degradation:
    - Falls back to in-memory cache if Redis unavailable
    - Falls back to local storage if cloud storage unavailable
    - Logs warnings but continues operation

    Examples:
        >>> mode = StandardMode(config={"redis_host": "localhost"})
        >>> mode.mode_config.name
        'standard'
        >>> mode.requires_external_services
        True
    """

    def get_mode_config(self) -> ModeConfig:
        """Get standard mode configuration.

        Returns:
            ModeConfig with standard mode settings
        """
        return ModeConfig(
            name="standard",
            description="Standard mode: Full production configuration with Redis and cloud storage",
            redis_enabled=True,
            cold_storage_enabled=True,
            cache_backend="redis",
        )

    async def initialize_cache(self) -> Any:
        """Initialize Redis cache with graceful fallback.

        Returns:
            Redis client instance if available, None for in-memory fallback

        Note:
            If Redis is unavailable, falls back to in-memory cache
            and logs a warning. Application continues normally.
        """
        try:
            import redis

            redis_host = self.config.get("redis_host", "localhost")
            redis_port = self.config.get("redis_port", 6379)
            redis_db = self.config.get("redis_db", 0)

            logger.info(f"Standard mode: Connecting to Redis at {redis_host}:{redis_port}")

            redis_client = redis.Redis(
                host=redis_host,
                port=redis_port,
                db=redis_db,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )

            # Test connection
            redis_client.ping()

            logger.info("Standard mode: Redis cache initialized successfully")
            return redis_client

        except ImportError:
            logger.warning(
                "Standard mode: redis package not installed, using in-memory cache. "
                "Install with: pip install redis"
            )
            return None

        except Exception as e:
            logger.warning(
                f"Standard mode: Redis unavailable ({e}), using in-memory cache. "
                "Start Redis with: docker run -d -p 6379:6379 redis"
            )
            return None

    async def initialize_cold_storage(self) -> Any:
        """Initialize Oneiric cold storage with graceful fallback.

        Returns:
            Storage adapter instance if available, None if disabled

        Note:
            If cloud storage is unavailable, logs a warning and
            continues without cold storage functionality.
        """
        try:
            from oneiric.adapters.storage import (
                LocalStorageAdapter,
                LocalStorageSettings,
                S3StorageAdapter,
                S3StorageSettings,
            )

            backend = self.config.get("cold_storage_backend", "local")
            bucket = self.config.get("cold_bucket", "akosha-cold-data")
            self.config.get("cold_prefix", "conversations/")

            logger.info(f"Standard mode: Initializing {backend} cold storage (bucket: {bucket})")

            # Create storage adapter based on backend type
            if backend == "local":
                settings = LocalStorageSettings(
                    base_path=self.config.get("local_storage_path", "./data/cold"),
                )
                storage = LocalStorageAdapter(settings)
            elif backend == "s3":
                settings = S3StorageSettings(
                    bucket=bucket,
                )
                storage = S3StorageAdapter(settings)
            else:
                logger.warning(
                    f"Standard mode: Unsupported cold storage backend '{backend}', "
                    f"falling back to local storage"
                )
                settings = LocalStorageSettings(
                    base_path=self.config.get("local_storage_path", "./data/cold"),
                )
                storage = LocalStorageAdapter(settings)

            logger.info(f"Standard mode: Cold storage initialized successfully ({backend})")
            return storage

        except ImportError as e:
            logger.warning(
                f"Standard mode: Oneiric storage adapter not available ({e}), cold storage disabled. "
                "Install Oneiric with: pip install oneiric"
            )
            return None

        except Exception as e:
            logger.warning(
                f"Standard mode: Cold storage unavailable ({e}), continuing without cold storage. "
                "Configure storage credentials or use local backend."
            )
            return None

    @property
    def requires_external_services(self) -> bool:
        """Standard mode requires external services.

        Returns:
            True (Redis and cloud storage required for full functionality)
        """
        return True

    def __repr__(self) -> str:
        """String representation of standard mode."""
        return "StandardMode(services_required=True, cache=redis, cold_storage=cloud)"
