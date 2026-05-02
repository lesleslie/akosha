"""Tests for storage path resolution."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from akosha.storage.path_resolver import StoragePathResolver


def test_resolver_does_not_warn_on_legacy_path(tmp_path: Path, monkeypatch, caplog) -> None:
    """Legacy paths should not spam startup logs."""
    legacy_path = tmp_path / "data"
    legacy_path.mkdir()
    (legacy_path / "warm").mkdir()

    monkeypatch.setenv("AKOSHA_ENV", "local")
    monkeypatch.chdir(tmp_path)

    caplog.clear()
    resolver = StoragePathResolver(project_dir=tmp_path)

    assert resolver.base_path.name == "akosha"
    assert resolver.base_path != legacy_path
    assert not [record for record in caplog.records if record.levelname == "WARNING"]


class TestStoragePathResolverInit:
    """Test StoragePathResolver initialization."""

    def test_default_env_detection(self):
        resolver = StoragePathResolver()
        assert resolver.env in ("local", "test", "container", "development")

    def test_forced_env(self):
        resolver = StoragePathResolver(env="container")
        assert resolver.env == "container"

    def test_forced_test_env(self):
        resolver = StoragePathResolver(env="test")
        assert resolver.env == "test"
        assert resolver.base_path == Path("/tmp") / "akosha" / "test"

    def test_custom_project_dir(self, tmp_path):
        resolver = StoragePathResolver(project_dir=tmp_path)
        assert resolver.project_dir == tmp_path

    @patch.dict(os.environ, {"AKOSHA_ENV": "staging"}, clear=False)
    def test_env_from_env_var(self):
        resolver = StoragePathResolver()
        assert resolver.env == "staging"

    @patch.dict(os.environ, {"AKOSHA_DATA_PATH": "/custom/data"}, clear=False)
    def test_data_path_override(self):
        resolver = StoragePathResolver(env="local")
        assert resolver.base_path == Path("/custom/data")

    def test_unknown_env_falls_back(self):
        resolver = StoragePathResolver(env="unknown_env_xyz")
        assert isinstance(resolver.base_path, Path)


class TestStoragePathResolverIsContainer:
    """Test _is_container detection."""

    def test_no_container(self):
        resolver = StoragePathResolver(env="local")
        result = resolver._is_container()
        assert isinstance(result, bool)


class TestStoragePathResolverMethods:
    """Test path resolution methods."""

    def test_get_warm_store_path(self):
        resolver = StoragePathResolver(env="test")
        path = resolver.get_warm_store_path()
        assert isinstance(path, Path)
        assert str(path).endswith("warm.db")

    @patch.dict(os.environ, {"AKOSHA_WARM_PATH": "/custom/warm.db"}, clear=False)
    def test_get_warm_store_path_override(self):
        resolver = StoragePathResolver(env="test")
        path = resolver.get_warm_store_path()
        assert path == Path("/custom/warm.db")

    def test_get_warm_store_dir(self):
        resolver = StoragePathResolver(env="test")
        path = resolver.get_warm_store_dir()
        assert isinstance(path, Path)
        assert path.name == "warm"

    def test_get_hot_store_wal_path(self):
        resolver = StoragePathResolver(env="test")
        path = resolver.get_hot_store_wal_path()
        assert isinstance(path, Path)
        assert path.name == "wal"

    @patch.dict(os.environ, {"AKOSHA_WAL_PATH": "/custom/wal"}, clear=False)
    def test_get_hot_store_wal_path_override(self):
        resolver = StoragePathResolver(env="test")
        path = resolver.get_hot_store_wal_path()
        assert path == Path("/custom/wal")

    def test_get_cold_store_cache_path(self):
        resolver = StoragePathResolver(env="test")
        path = resolver.get_cold_store_cache_path()
        assert isinstance(path, Path)
        assert "cache" in str(path)

    def test_get_config_dir(self):
        resolver = StoragePathResolver(env="test")
        path = resolver.get_config_dir()
        assert isinstance(path, Path)

    def test_get_cache_dir(self):
        resolver = StoragePathResolver(env="test")
        path = resolver.get_cache_dir()
        assert isinstance(path, Path)

    def test_get_xdg_data_path(self):
        resolver = StoragePathResolver(env="local")
        path = resolver._get_xdg_data_path()
        assert isinstance(path, Path)

    def test_development_base_path(self, tmp_path):
        resolver = StoragePathResolver(env="development", project_dir=tmp_path)
        assert resolver.base_path == tmp_path / ".akosha" / "data"

    def test_container_base_path(self):
        resolver = StoragePathResolver(env="container")
        assert isinstance(resolver.base_path, Path)


class TestEnsureDirectories:
    """Test ensure_directories method."""

    def test_creates_directories(self, tmp_path):
        resolver = StoragePathResolver(env="test")
        # Override base_path to tmp_path so we don't pollute /tmp
        resolver.base_path = tmp_path / "akosha_test"
        resolver.ensure_directories()
        assert (tmp_path / "akosha_test" / "warm").exists()
        assert (tmp_path / "akosha_test" / "wal").exists()
        assert (tmp_path / "akosha_test" / "cold" / "cache").exists()


class TestModuleLevelFunctions:
    """Test module-level convenience functions."""

    def test_get_default_resolver(self):
        from akosha.storage.path_resolver import get_default_resolver

        resolver = get_default_resolver()
        assert isinstance(resolver, StoragePathResolver)

    def test_get_warm_store_path(self):
        from akosha.storage.path_resolver import get_warm_store_path

        path = get_warm_store_path()
        assert isinstance(path, Path)
        assert str(path).endswith("warm.db")

    def test_get_config_dir(self):
        from akosha.storage.path_resolver import get_config_dir

        path = get_config_dir()
        assert isinstance(path, Path)
