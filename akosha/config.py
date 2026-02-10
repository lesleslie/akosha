"""Akosha configuration management with environment variable overrides.

This module provides centralized configuration management with support for:
- Environment variable overrides (highest priority)
- YAML config file loading
- Pydantic validation
- Path resolution with StoragePathResolver (XDG-compliant)
- Multi-environment support (dev, staging, prod)

Priority order for configuration values:
1. Environment variables (AKOSHA_*)
2. YAML config file
3. StoragePathResolver for paths
4. Pydantic defaults (lowest priority)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from akosha.storage.path_resolver import StoragePathResolver

logger = logging.getLogger(__name__)


class HotStorageConfig(BaseModel):
    """Hot storage configuration.

    Attributes:
        backend: Storage backend type (duckdb-memory, duckdb-ssd)
        path: Path to hot storage (usually ":memory:" for in-memory)
        write_ahead_log: Enable write-ahead logging
        wal_path: Path to WAL directory
    """

    backend: str = "duckdb-memory"
    path: str = ":memory:"
    write_ahead_log: bool = True
    wal_path: Path | None = None  # Will be resolved by model_validator

    @model_validator(mode="after")
    def resolve_paths(self) -> "HotStorageConfig":
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
    def resolve_paths(self) -> "WarmStorageConfig":
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


class AkoshaConfig(BaseSettings):
    """Main Akosha configuration.

    This class loads configuration from multiple sources:
    1. Environment variables (AKOSHA_*)
    2. YAML config file (if AKOSHA_CONFIG is set)
    3. Pydantic defaults

    Attributes:
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
    prometheus_port: int = Field(default_factory=lambda: int(os.getenv("AKOSHA_PROMETHEUS_PORT", "9090")))

    # Environment
    environment: str = Field(default_factory=lambda: os.getenv("AKOSHA_ENVIRONMENT", "development"))

    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        env_prefix="akosha_",
    )


def load_config_from_file(config_path: str) -> dict[str, Any]:
    """Load configuration from YAML file.

    Args:
        config_path: Path to YAML configuration file

    Returns:
        Configuration dictionary
    """
    path = Path(config_path).expanduser()
    if not path.exists():
        logger.warning(f"Config file not found: {config_path}")
        return {}

    try:
        with open(path) as f:
            config = yaml.safe_load(f) or {}
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

    # Validate warm path
    warm_path = config.warm.path
    if warm_path.exists():
        results["warm"] = True
    else:
        # Try to create it
        try:
            warm_path.mkdir(parents=True, exist_ok=True)
            results["warm"] = True
            logger.info(f"Created warm storage directory: {warm_path}")
        except Exception as e:
            results["warm"] = False
            logger.error(f"Cannot create warm storage directory {warm_path}: {e}")

    # Validate WAL path (if enabled)
    if config.hot.write_ahead_log:
        wal_path = config.hot.wal_path
        if wal_path.exists():
            results["wal"] = True
        else:
            try:
                wal_path.mkdir(parents=True, exist_ok=True)
                results["wal"] = True
                logger.info(f"Created WAL directory: {wal_path}")
            except Exception as e:
                results["wal"] = False
                logger.error(f"Cannot create WAL directory {wal_path}: {e}")

    # Cold storage is external, just validate config
    results["cold"] = bool(config.cold.bucket)

    # Hot store is in-memory, always valid
    results["hot"] = True

    return results


def get_config(config_path: str | None = None) -> AkoshaConfig:
    """Get configuration instance.

    Args:
        config_path: Optional path to YAML config file

    Returns:
        AkoshaConfig instance
    """
    # Load from file if provided
    if config_path:
        file_config = load_config_from_file(config_path)
        # Merge with environment variables
        # (Pydantic will handle env vars with higher priority)
        return AkoshaConfig(**file_config)

    # Use default loading (env vars + defaults)
    return AkoshaConfig()


# Global configuration instance
config = get_config()
