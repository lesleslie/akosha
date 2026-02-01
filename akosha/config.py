"""Akosha configuration management."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class HotStorageConfig(BaseModel):
    """Hot storage configuration."""

    backend: str = "duckdb-memory"
    path: Path = Field(default_factory=lambda: Path("/data/akosha/hot"))
    write_ahead_log: bool = True
    wal_path: Path = Field(default_factory=lambda: Path("/data/akosha/wal"))


class WarmStorageConfig(BaseModel):
    """Warm storage configuration."""

    backend: str = "duckdb-ssd"
    path: Path = Field(default_factory=lambda: Path("/data/akosha/warm"))
    num_partitions: int = 256


class ColdStorageConfig(BaseModel):
    """Cold storage configuration."""

    backend: str = "s3"  # or azure, gcs
    bucket: str = Field(default_factory=lambda: os.getenv("AKOSHA_COLD_BUCKET", "akosha-cold-data"))
    prefix: str = "conversations/"
    format: str = "parquet"


class CacheConfig(BaseModel):
    """Cache configuration."""

    backend: str = "redis"
    host: str = Field(default_factory=lambda: os.getenv("REDIS_HOST", "localhost"))
    port: int = 6379
    db: int = 0
    local_ttl_seconds: int = 60
    redis_ttl_seconds: int = 3600


class AkoshaConfig(BaseSettings):
    """Main Akosha configuration."""

    # Storage
    hot: HotStorageConfig = Field(default_factory=HotStorageConfig)
    warm: WarmStorageConfig = Field(default_factory=WarmStorageConfig)
    cold: ColdStorageConfig = Field(default_factory=ColdStorageConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)

    # API
    api_port: int = 8000
    mcp_port: int = 3002
    debug: bool = False

    # Processing
    ingestion_workers: int = 3
    max_concurrent_ingests: int = 100
    shard_count: int = 256

    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
    )


# Global configuration instance
config = AkoshaConfig()
