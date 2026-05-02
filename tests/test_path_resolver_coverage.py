"""Tests for akosha/storage/path_resolver.py — StoragePathResolver, convenience functions."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from akosha.storage.path_resolver import (
    StoragePathResolver,
    get_config_dir,
    get_default_resolver,
    get_warm_store_path,
)


class TestStoragePathResolverInit:
    def test_init_with_env(self):
        r = StoragePathResolver(env="container")
        assert r.env == "container"

    def test_init_uses_project_dir(self):
        r = StoragePathResolver(env="test", project_dir=Path("/my/proj"))
        assert r.project_dir == Path("/my/proj")

    def test_init_resolves_base_path(self):
        r = StoragePathResolver(env="test")
        assert r.base_path == Path("/tmp/akosha/test")


class TestDetectEnvironment:
    @patch.dict("os.environ", {"AKOSHA_ENV": "container"}, clear=False)
    def test_env_var_overrides(self):
        r = StoragePathResolver()
        assert r.env == "container"

    @patch.dict("os.environ", {}, clear=False)
    @patch("akosha.storage.path_resolver.StoragePathResolver._is_container", return_value=True)
    def test_container_detected(self, mock_is_container):
        # Ensure AKOSHA_ENV not set
        import os

        os.environ.pop("AKOSHA_ENV", None)
        r = StoragePathResolver()
        assert r.env == "container"

    @patch.dict("os.environ", {}, clear=False)
    @patch("akosha.storage.path_resolver.StoragePathResolver._is_container", return_value=False)
    def test_test_env_detected(self, mock_is_container):
        import os

        os.environ.pop("AKOSHA_ENV", None)
        with patch.dict("sys.modules", {"pytest": MagicMock()}):
            r = StoragePathResolver()
            assert r.env == "test"

    @patch.dict("os.environ", {"PYTEST_CURRENT_TEST": "test_foo"}, clear=False)
    @patch("akosha.storage.path_resolver.StoragePathResolver._is_container", return_value=False)
    def test_pytest_env_var_detected(self, mock_is_container):
        import os

        os.environ.pop("AKOSHA_ENV", None)
        r = StoragePathResolver()
        assert r.env == "test"

    @patch.dict("os.environ", {}, clear=False)
    @patch("akosha.storage.path_resolver.StoragePathResolver._is_container", return_value=False)
    def test_defaults_to_local(self, mock_is_container):
        import os

        os.environ.pop("AKOSHA_ENV", None)
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        # Must remove pytest from sys.modules to test default detection
        pytest_mod = sys.modules.pop("pytest", None)
        try:
            r = StoragePathResolver()
            assert r.env == "local"
        finally:
            if pytest_mod:
                sys.modules["pytest"] = pytest_mod


class TestIsContainer:
    def test_dockerenv_file(self, tmp_path):
        r = StoragePathResolver(env="test")
        with patch("pathlib.Path.exists", return_value=True):
            # First call for /.dockerenv
            r._is_container()  # just ensure no error


class TestResolveBasePath:
    @patch.dict("os.environ", {"AKOSHA_DATA_PATH": "/custom/data"}, clear=False)
    def test_explicit_override_highest_priority(self):
        r = StoragePathResolver(env="test")
        assert r.base_path == Path("/custom/data")

    def test_container_env(self):
        r = StoragePathResolver(env="container")
        assert r.base_path == Path("/data/akosha")

    def test_container_with_data_path(self):
        with patch.dict("os.environ", {"AKOSHA_DATA_PATH": "/mnt/data"}):
            r = StoragePathResolver(env="container")
            assert r.base_path == Path("/mnt/data")

    def test_local_env(self):
        r = StoragePathResolver(env="local")
        # Should use XDG path
        assert "akosha" in str(r.base_path)

    def test_development_env(self):
        r = StoragePathResolver(env="development", project_dir=Path("/my/proj"))
        assert r.base_path == Path("/my/proj/.akosha/data")

    def test_test_env(self):
        r = StoragePathResolver(env="test")
        assert r.base_path == Path("/tmp/akosha/test")

    def test_unknown_env_falls_back(self):
        r = StoragePathResolver(env="unknown_env_xyz")
        assert "akosha" in str(r.base_path)


class TestGetXDGDataPath:
    def test_xdg_data_home_override(self):
        r = StoragePathResolver(env="local")
        with patch.dict("os.environ", {"XDG_DATA_HOME": "/xdg/data"}):
            path = r._get_xdg_data_path()
            assert path == Path("/xdg/data/akosha")

    def test_macos_traditional_path(self):
        r = StoragePathResolver(env="local")
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("XDG_DATA_HOME", None)
            with (
                patch("akosha.storage.path_resolver.IS_MACOS", True),
                patch("akosha.storage.path_resolver.IS_WINDOWS", False),
            ):
                path = r._get_xdg_data_path()
                assert "Library" in str(path)
                assert "akosha" in str(path)

    def test_windows_localappdata(self):
        r = StoragePathResolver(env="local")
        with (
            patch("akosha.storage.path_resolver.IS_WINDOWS", True),
            patch("akosha.storage.path_resolver.IS_MACOS", False),
            patch.dict("os.environ", {"LOCALAPPDATA": "C:\\Users\\test\\AppData\\Local"}),
        ):
            path = r._get_xdg_data_path()
            assert path == Path("C:\\Users\\test\\AppData\\Local/akosha")


class TestGetWarmStorePath:
    def test_default_path(self):
        r = StoragePathResolver(env="test")
        assert r.get_warm_store_path() == Path("/tmp/akosha/test/warm/warm.db")

    @patch.dict("os.environ", {"AKOSHA_WARM_PATH": "/custom/warm.db"}, clear=False)
    def test_env_override(self):
        r = StoragePathResolver(env="test")
        assert r.get_warm_store_path() == Path("/custom/warm.db")

    def test_warm_store_dir(self):
        r = StoragePathResolver(env="test")
        assert r.get_warm_store_dir() == Path("/tmp/akosha/test/warm")


class TestGetHotStoreWalPath:
    def test_default_path(self):
        r = StoragePathResolver(env="test")
        assert r.get_hot_store_wal_path() == Path("/tmp/akosha/test/wal")

    @patch.dict("os.environ", {"AKOSHA_WAL_PATH": "/custom/wal"}, clear=False)
    def test_env_override(self):
        r = StoragePathResolver(env="test")
        assert r.get_hot_store_wal_path() == Path("/custom/wal")


class TestGetColdStoreCachePath:
    def test_default_path(self):
        r = StoragePathResolver(env="test")
        assert r.get_cold_store_cache_path() == Path("/tmp/akosha/test/cold/cache")


class TestGetConfigDir:
    def test_xdg_config_home(self):
        r = StoragePathResolver(env="local")
        with patch.dict("os.environ", {"XDG_CONFIG_HOME": "/xdg/config"}):
            assert r.get_config_dir() == Path("/xdg/config/akosha")

    def test_windows_appdata(self):
        r = StoragePathResolver(env="local")
        with (
            patch("akosha.storage.path_resolver.IS_WINDOWS", True),
            patch("akosha.storage.path_resolver.IS_MACOS", False),
            patch.dict("os.environ", {"APPDATA": "C:\\AppData"}),
        ):
            assert r.get_config_dir() == Path("C:\\AppData/akosha")


class TestGetCacheDir:
    def test_xdg_cache_home(self):
        r = StoragePathResolver(env="local")
        with patch.dict("os.environ", {"XDG_CACHE_HOME": "/xdg/cache"}):
            assert r.get_cache_dir() == Path("/xdg/cache/akosha")

    def test_windows_localappdata(self):
        r = StoragePathResolver(env="local")
        with (
            patch("akosha.storage.path_resolver.IS_WINDOWS", True),
            patch("akosha.storage.path_resolver.IS_MACOS", False),
            patch.dict("os.environ", {"LOCALAPPDATA": "C:\\LocalAppData"}),
        ):
            assert r.get_cache_dir() == Path("C:\\LocalAppData/akosha/cache")


class TestEnsureDirectories:
    def test_creates_directories(self, tmp_path):
        r = StoragePathResolver(env="test")
        # Override base_path to tmp
        r.base_path = tmp_path / "akosha_test"
        r.ensure_directories()
        assert (r.base_path / "warm").exists()
        assert (r.base_path / "wal").exists()
        assert (r.base_path / "cold" / "cache").exists()
        assert r.get_config_dir().parent.exists()
        assert r.get_cache_dir().parent.exists()


class TestGetDefaultResolver:
    def test_returns_resolver(self):
        r = get_default_resolver()
        assert isinstance(r, StoragePathResolver)


class TestConvenienceFunctions:
    def test_get_warm_store_path(self):
        path = get_warm_store_path()
        assert isinstance(path, Path)
        assert path.name == "warm.db"

    def test_get_config_dir(self):
        path = get_config_dir()
        assert isinstance(path, Path)
        assert "akosha" in str(path)
