"""Akosha configuration management using Oneiric MCPBaseSettings pattern.

This module provides centralized configuration management following the
Oneiric pattern with layered configuration loading:
- Defaults in field definitions
- settings/akosha.yaml (committed)
- settings/local.yaml (gitignored)
- Environment variables AKOSHA_*

Configuration loading order (later overrides earlier):
1. Default values in field definitions
2. settings/akosha.yaml (committed, for production defaults)
3. settings/local.yaml (gitignored, for development)
4. Environment variables AKOSHA_{FIELD}

For nested storage configs (HotStorageConfig, etc.), environment variables
use AKOSHA_{FIELD}__{SUBFIELD} format.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, Field, model_validator

if TYPE_CHECKING:
    # Type-checker only: import the real base class so ``class AkoshaConfig``
    # sees a single concrete base (no MRO ambiguity).
    from mcp_common.config.base import MCPBaseSettings
else:
    try:
        from mcp_common.config.base import MCPBaseSettings  # type: ignore[assignment]
    except ImportError:
        # Fallback if mcp-common is not installed. Defining an empty subclass of
        # BaseModel keeps ``MCPBaseSettings`` a single class for the type checker,
        # instead of a ``MCPBaseSettings | BaseModel`` union which would break
        # ``class AkoshaConfig(MCPBaseSettings)``. The ``TYPE_CHECKING`` branch
        # above shadows this fallback for the type checker.
        class MCPBaseSettings(BaseModel):  # type: ignore[no-redef]
            """Fallback base class when ``mcp-common`` is unavailable."""

from akosha.storage.path_resolver import StoragePathResolver

logger = logging.getLogger(__name__)


class HotStorageConfig(BaseModel):
    """Hot storage configuration.

    Attributes:
        backend: Storage backend type (duckdb-memory, duckdb-ssd, pgvector)
        path: Path to hot storage (usually ":memory:" for in-memory)
        pg_url: PostgreSQL connection string for pgvector backend
        write_ahead_log: Enable write-ahead logging
        wal_path: Path to WAL directory

    Configuration can be set via:
    1. settings/akosha.yaml under storage.hot
    2. settings/local.yaml
    3. Environment variables: AKOSHA__STORAGE__HOT__BACKEND, AKOSHA__STORAGE__HOT__PG_URL
    """

    backend: str = Field(
        default="duckdb-memory",
        description=(
            "Storage backend: 'duckdb-memory', 'duckdb-ssd', or 'pgvector'. "
            "Set via AKOSHA__STORAGE__HOT__BACKEND"
        ),
    )
    path: str = Field(
        default=":memory:",
        description="DuckDB database path for OTel ingester (':memory:' for in-memory)",
    )
    pg_url: str = Field(
        default="",
        description=(
            "PostgreSQL connection string for pgvector-backed hot storage. "
            "Required when backend='pgvector'. Set via AKOSHA__STORAGE__HOT__PG_URL"
        ),
    )
    write_ahead_log: bool = Field(default=True)
    wal_path: Path | None = None  # Will be resolved by model_validator

    def __init__(self, **data: Any) -> None:
        # Phase 1.1c: Honor AKOSHA__STORAGE__HOT__BACKEND and AKOSHA__STORAGE__HOT__PG_URL
        _env_backend = os.getenv("AKOSHA__STORAGE__HOT__BACKEND", "")
        _env_pg_url = os.getenv("AKOSHA__STORAGE__HOT__PG_URL", "")
        if _env_backend and "backend" not in data:
            data["backend"] = _env_backend
        if _env_pg_url and "pg_url" not in data:
            data["pg_url"] = _env_pg_url
        super().__init__(**data)

    @model_validator(mode="after")
    def resolve_paths(self) -> HotStorageConfig:
        """Resolve WAL path using StoragePathResolver."""
        if self.wal_path is None:
            resolver = StoragePathResolver()
            self.wal_path = resolver.get_hot_store_wal_path()
        return self


class WarmStorageConfig(BaseModel):
    """Warm storage configuration.

    Attributes:
        backend: Storage backend type (duckdb-ssd, duckdb-hdd)
        path: Path to warm storage directory
        num_partitions: Number of shards for distributed queries
    """

    backend: str = "duckdb-ssd"
    path: Path | None = None  # Will be resolved by model_validator
    num_partitions: int = 256

    @model_validator(mode="after")
    def resolve_paths(self) -> WarmStorageConfig:
        """Resolve warm storage path using StoragePathResolver."""
        if self.path is None:
            resolver = StoragePathResolver()
            self.path = resolver.get_warm_store_path()
        return self


class ColdStorageConfig(BaseModel):
    """Cold storage configuration.

    Attributes:
        backend: Storage backend type (local, s3, azure, gcs)
        bucket: Bucket name for cloud storage
        prefix: Prefix for objects in bucket
        format: File format (parquet)
        region: Cloud region
    """

    backend: str = Field(default_factory=lambda: os.getenv("AKOSHA_COLD_BACKEND", "local"))
    bucket: str = Field(default_factory=lambda: os.getenv("AKOSHA_COLD_BUCKET", "akosha-cold-data"))
    prefix: str = "conversations/"
    format: str = "parquet"
    region: str = Field(default_factory=lambda: os.getenv("AKOSHA_COLD_REGION", "us-west-2"))


class CacheConfig(BaseModel):
    """Cache configuration.

    Attributes:
        backend: Cache backend (redis, memory)
        host: Redis hostname
        port: Redis port
        db: Redis database number
        local_ttl_seconds: TTL for in-memory cache
        redis_ttl_seconds: TTL for Redis cache
    """

    backend: str = Field(default_factory=lambda: os.getenv("AKOSHA_CACHE_BACKEND", "redis"))
    host: str = Field(default_factory=lambda: os.getenv("AKOSHA_REDIS_HOST", "localhost"))
    port: int = Field(default_factory=lambda: int(os.getenv("AKOSHA_REDIS_PORT", "6379")))
    db: int = 0
    local_ttl_seconds: int = 60
    redis_ttl_seconds: int = 3600


class AkoshaConfig(MCPBaseSettings):  # type: ignore[reportUntypedBaseClass]
    """Main Akosha configuration using Oneiric MCPBaseSettings pattern.

    Configuration loading order (later overrides earlier):
    1. Default values in field definitions
    2. settings/akosha.yaml (committed, for production defaults)
    3. settings/local.yaml (gitignored, for development)
    4. Environment variables AKOSHA_{FIELD}

    Attributes:
        server_name: Display name for the MCP server
        mode: Operational mode (lite, standard)
        hot: Hot storage configuration
        warm: Warm storage configuration
        cold: Cold storage configuration
        cache: Cache configuration
        api_port: API server port
        mcp_port: MCP server port
        debug: Enable debug mode
        ingestion_workers: Number of ingestion workers
        max_concurrent_ingests: Maximum concurrent ingestions
        shard_count: Number of shards for distributed queries
    """

    server_name: str = Field(
        default="Akosha Seer",
        description="Display name for Akosha server",
    )
    server_description: str = Field(
        default="Cross-system intelligence and embeddings for the Bodai ecosystem",
        description="Brief description of server functionality",
    )

    # Mode
    mode: str = Field(default_factory=lambda: os.getenv("AKOSHA_MODE", "lite"))

    # Storage
    hot: HotStorageConfig = Field(default_factory=HotStorageConfig)
    warm: WarmStorageConfig = Field(default_factory=WarmStorageConfig)
    cold: ColdStorageConfig = Field(default_factory=ColdStorageConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)

    # API
    api_port: int = Field(default_factory=lambda: int(os.getenv("AKOSHA_API_PORT", "8682")))
    mcp_port: int = Field(default_factory=lambda: int(os.getenv("AKOSHA_MCP_PORT", "3002")))
    debug: bool = False

    # Processing
    ingestion_workers: int = 3
    max_concurrent_ingests: int = 100
    shard_count: int = 256

    # Monitoring
    metrics_enabled: bool = True
    prometheus_port: int = Field(
        default_factory=lambda: int(os.getenv("AKOSHA_PROMETHEUS_PORT", "9090"))
    )

    # Environment
    environment: str = Field(default_factory=lambda: os.getenv("AKOSHA_ENVIRONMENT", "development"))

    model_config = {"arbitrary_types_allowed": True}


def load_config_from_file(config_path: str) -> dict[str, Any]:
    """Load configuration from YAML file.

    Args:
        config_path: Path to YAML configuration file

    Returns:
        Configuration dictionary
    """
    import yaml

    path = Path(config_path).expanduser()
    if not path.exists():
        logger.warning(f"Config file not found: {config_path}")
        return {}

    try:
        with path.open() as f:
            config: dict[str, Any] = yaml.safe_load(f) or {}  # type: ignore[assignment]
        logger.info(f"Loaded configuration from {config_path}")
        return config
    except Exception as e:
        logger.error(f"Failed to load config from {config_path}: {e}")
        return {}


def validate_storage_config(config: AkoshaConfig) -> dict[str, bool]:
    """Validate that storage paths are accessible.

    Args:
        config: Akosha configuration instance

    Returns:
        Dictionary mapping storage tier to validity status
    """
    results: dict[str, bool] = {}

    # Validate warm path. ``config.warm.path`` is typed as ``Path | None``; the
    # walrus + ``is None`` check narrows the type for the rest of the branch.
    if (warm_path := config.warm.path) is None or not warm_path.exists():
        # Either path is missing or doesn't exist on disk — try to create it.
        if warm_path is not None:
            try:
                warm_path.mkdir(parents=True, exist_ok=True)
                results["warm"] = True
                logger.info(f"Created warm storage directory: {warm_path}")
            except Exception as e:
                results["warm"] = False
                logger.error(f"Cannot create warm storage directory {warm_path}: {e}")
        else:
            results["warm"] = False
            logger.error("Warm storage path is not configured")
    else:
        results["warm"] = True

    # Validate WAL path (if enabled)
    if config.hot.write_ahead_log:
        wal_path = config.hot.wal_path
        if wal_path is None or not wal_path.exists():
            if wal_path is not None:
                try:
                    wal_path.mkdir(parents=True, exist_ok=True)
                    results["wal"] = True
                    logger.info(f"Created WAL directory: {wal_path}")
                except Exception as e:
                    results["wal"] = False
                    logger.error(f"Cannot create WAL directory {wal_path}: {e}")
            else:
                results["wal"] = False
                logger.error("WAL path is not configured")
        else:
            results["wal"] = True

    # Cold storage is external, just validate config
    results["cold"] = bool(config.cold.bucket)

    # Hot store is in-memory, always valid
    results["hot"] = True

    return results


def get_config(config_path: str | None = None) -> AkoshaConfig:
    """Get configuration instance.

    Uses the MCPBaseSettings.load() pattern for layered configuration.
    For backward compatibility, supports loading from explicit config path.

    Args:
        config_path: Optional path to YAML config file (for backward compatibility)

    Returns:
        AkoshaConfig instance
    """
    # Use MCPBaseSettings.load() pattern. ``load`` is typed against the base
    # class's self-type, so we cast back to ``AkoshaConfig`` for the caller.
    return cast(
        AkoshaConfig,
        AkoshaConfig.load(
            "akosha", config_path=Path(config_path) if config_path else None
        ),
    )


# Global configuration instance
config = get_config()
