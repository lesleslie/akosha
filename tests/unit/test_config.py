"""Tests for akosha.config — AkoshaConfig, storage configs, and helpers."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch


class TestHotStorageConfig:
    """Test HotStorageConfig model."""

    def test_defaults(self):
        from akosha.config import HotStorageConfig

        cfg = HotStorageConfig()
        assert cfg.backend == "duckdb-memory"
        assert cfg.path == ":memory:"
        assert cfg.write_ahead_log is True
        assert cfg.wal_path is not None  # resolved by validator

    def test_custom_values(self):
        from akosha.config import HotStorageConfig

        cfg = HotStorageConfig(
            backend="duckdb-ssd",
            path="/tmp/hot.db",
            write_ahead_log=False,
            wal_path=Path("/custom/wal"),
        )
        assert cfg.backend == "duckdb-ssd"
        assert cfg.path == "/tmp/hot.db"
        assert cfg.write_ahead_log is False
        assert cfg.wal_path == Path("/custom/wal")


class TestWarmStorageConfig:
    """Test WarmStorageConfig model."""

    def test_defaults(self):
        from akosha.config import WarmStorageConfig

        cfg = WarmStorageConfig()
        assert cfg.backend == "duckdb-ssd"
        assert cfg.path is not None  # resolved by validator
        assert cfg.num_partitions == 256

    def test_custom_values(self):
        from akosha.config import WarmStorageConfig

        cfg = WarmStorageConfig(
            backend="duckdb-hdd",
            path=Path("/tmp/warm"),
            num_partitions=128,
        )
        assert cfg.backend == "duckdb-hdd"
        assert cfg.num_partitions == 128


class TestColdStorageConfig:
    """Test ColdStorageConfig model."""

    def test_defaults(self):
        from akosha.config import ColdStorageConfig

        cfg = ColdStorageConfig()
        assert cfg.backend == "local"
        assert cfg.bucket == "akosha-cold-data"
        assert cfg.prefix == "conversations/"
        assert cfg.format == "parquet"
        assert cfg.region == "us-west-2"

    @patch.dict(os.environ, {"AKOSHA_COLD_BACKEND": "s3", "AKOSHA_COLD_BUCKET": "my-bucket"})
    def test_env_overrides(self):
        from akosha.config import ColdStorageConfig

        cfg = ColdStorageConfig()
        assert cfg.backend == "s3"
        assert cfg.bucket == "my-bucket"


class TestCacheConfig:
    """Test CacheConfig model."""

    def test_defaults(self):
        from akosha.config import CacheConfig

        cfg = CacheConfig()
        assert cfg.backend == "redis"
        assert cfg.host == "localhost"
        assert cfg.port == 6379
        assert cfg.db == 0
        assert cfg.local_ttl_seconds == 60
        assert cfg.redis_ttl_seconds == 3600

    @patch.dict(os.environ, {"AKOSHA_CACHE_BACKEND": "memory", "AKOSHA_REDIS_PORT": "7000"})
    def test_env_overrides(self):
        from akosha.config import CacheConfig

        cfg = CacheConfig()
        assert cfg.backend == "memory"
        assert cfg.port == 7000


class TestAkoshaConfig:
    """Test AkoshaConfig main settings."""

    def test_defaults(self):
        from akosha.config import AkoshaConfig

        cfg = AkoshaConfig()
        assert cfg.mode == "lite"
        assert cfg.api_port == 8682
        assert cfg.mcp_port == 3002
        assert cfg.debug is False
        assert cfg.ingestion_workers == 3
        assert cfg.max_concurrent_ingests == 100
        assert cfg.shard_count == 256
        assert cfg.metrics_enabled is True
        assert cfg.prometheus_port == 9090

    def test_storage_configs_created(self):
        from akosha.config import AkoshaConfig

        cfg = AkoshaConfig()
        assert isinstance(cfg.hot, object)
        assert isinstance(cfg.warm, object)
        assert isinstance(cfg.cold, object)
        assert isinstance(cfg.cache, object)

    @patch.dict(os.environ, {"AKOSHA_MODE": "standard", "AKOSHA_API_PORT": "9000"})
    def test_env_overrides(self):
        from akosha.config import AkoshaConfig

        cfg = AkoshaConfig()
        assert cfg.mode == "standard"
        assert cfg.api_port == 9000


class TestLoadConfigFromFile:
    """Test load_config_from_file helper."""

    def test_nonexistent_file(self):
        from akosha.config import load_config_from_file

        result = load_config_from_file("/nonexistent/path.yaml")
        assert result == {}

    def test_valid_yaml(self, tmp_path):
        from akosha.config import load_config_from_file

        config_file = tmp_path / "config.yaml"
        config_file.write_text("mode: standard\napi_port: 9999\n")
        result = load_config_from_file(str(config_file))
        assert result["mode"] == "standard"
        assert result["api_port"] == 9999

    def test_empty_yaml(self, tmp_path):
        from akosha.config import load_config_from_file

        config_file = tmp_path / "empty.yaml"
        config_file.write_text("")
        result = load_config_from_file(str(config_file))
        assert result == {}

    def test_malformed_yaml(self, tmp_path):
        from akosha.config import load_config_from_file

        config_file = tmp_path / "bad.yaml"
        config_file.write_text("mode: [unclosed")
        result = load_config_from_file(str(config_file))
        assert result == {}


class TestValidateStorageConfig:
    """Test validate_storage_config helper."""

    def test_warm_path_exists(self, tmp_path):
        from akosha.config import AkoshaConfig, validate_storage_config

        cfg = AkoshaConfig()
        cfg.warm.path = tmp_path
        cfg.hot.write_ahead_log = False
        result = validate_storage_config(cfg)
        assert result["warm"] is True
        assert result["hot"] is True
        assert result["cold"] is True

    def test_warm_path_created(self, tmp_path):
        from akosha.config import AkoshaConfig, validate_storage_config

        new_dir = tmp_path / "sub" / "deep"
        cfg = AkoshaConfig()
        cfg.warm.path = new_dir
        cfg.hot.write_ahead_log = False
        result = validate_storage_config(cfg)
        assert result["warm"] is True
        assert new_dir.exists()

    def test_cold_empty_bucket(self):
        from akosha.config import AkoshaConfig, validate_storage_config

        cfg = AkoshaConfig()
        cfg.warm.path = Path("/tmp")
        cfg.cold.bucket = ""
        cfg.hot.write_ahead_log = False
        result = validate_storage_config(cfg)
        assert result["cold"] is False


class TestGetConfig:
    """Test get_config helper."""

    def test_default_config(self):
        from akosha.config import get_config

        cfg = get_config()
        assert cfg.api_port == 8682

    def test_config_from_file(self, tmp_path):
        from akosha.config import get_config

        config_file = tmp_path / "akosha.yaml"
        config_file.write_text("mode: standard\n")
        cfg = get_config(str(config_file))
        assert cfg.mode == "standard"

    def test_config_from_nonexistent_file(self):
        from akosha.config import get_config

        cfg = get_config("/nonexistent.yaml")
        # Should still return a valid config with defaults
        assert cfg.api_port == 8682
