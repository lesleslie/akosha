"""Branch-focused tests for storage path resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

import akosha.storage.path_resolver as path_resolver


def _resolver() -> path_resolver.StoragePathResolver:
    """Create an uninitialized resolver for direct branch testing."""
    return path_resolver.StoragePathResolver.__new__(path_resolver.StoragePathResolver)


def test_detect_environment_prefers_explicit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit AKOSHA_ENV should win before other detection paths."""
    resolver = _resolver()
    monkeypatch.setenv("AKOSHA_ENV", "staging")
    monkeypatch.setattr(path_resolver.StoragePathResolver, "_is_container", lambda self: False)

    assert resolver._detect_environment() == "staging"


def test_detect_environment_detects_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """Container detection should return the container environment."""
    resolver = _resolver()
    monkeypatch.delenv("AKOSHA_ENV", raising=False)
    monkeypatch.setattr(path_resolver.StoragePathResolver, "_is_container", lambda self: True)
    monkeypatch.setattr(path_resolver.sys, "modules", {})
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    assert resolver._detect_environment() == "container"


def test_detect_environment_defaults_to_test_when_pytest_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pytest execution should be detected as the test environment."""
    resolver = _resolver()
    monkeypatch.delenv("AKOSHA_ENV", raising=False)
    monkeypatch.setattr(path_resolver.StoragePathResolver, "_is_container", lambda self: False)
    monkeypatch.setattr(path_resolver.sys, "modules", {"pytest": object()})
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    assert resolver._detect_environment() == "test"


def test_detect_environment_defaults_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without overrides, container signals, or pytest, the default is local."""
    resolver = _resolver()
    monkeypatch.delenv("AKOSHA_ENV", raising=False)
    monkeypatch.setattr(path_resolver.StoragePathResolver, "_is_container", lambda self: False)
    monkeypatch.setattr(path_resolver.sys, "modules", {})
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    assert resolver._detect_environment() == "local"


def test_is_container_detects_dockerenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """The .dockerenv marker should identify container runtime."""

    def fake_exists(self: Path) -> bool:
        return str(self) == "/.dockerenv"

    monkeypatch.setattr(path_resolver.Path, "exists", fake_exists, raising=False)

    assert _resolver()._is_container() is True


def test_is_container_detects_cgroup_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """Docker-style cgroup entries should identify container runtime."""

    def fake_exists(self: Path) -> bool:
        return str(self) == "/proc/1/cgroup"

    def fake_read_text(self: Path) -> str:
        return "1:cpu:/docker/abc123"

    monkeypatch.setattr(path_resolver.Path, "exists", fake_exists, raising=False)
    monkeypatch.setattr(path_resolver.Path, "read_text", fake_read_text, raising=False)

    assert _resolver()._is_container() is True


def test_is_container_handles_cgroup_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unreadable cgroup files should fail closed."""

    def fake_exists(self: Path) -> bool:
        return str(self) == "/proc/1/cgroup"

    def fake_read_text(self: Path) -> str:
        raise OSError("denied")

    monkeypatch.setattr(path_resolver.Path, "exists", fake_exists, raising=False)
    monkeypatch.setattr(path_resolver.Path, "read_text", fake_read_text, raising=False)

    assert _resolver()._is_container() is False


def test_is_container_returns_false_for_non_container_cgroup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cgroup file without container markers should not be treated as a container."""

    def fake_exists(self: Path) -> bool:
        return str(self) == "/proc/1/cgroup"

    def fake_read_text(self: Path) -> str:
        return "1:cpu:/user.slice"

    monkeypatch.setattr(path_resolver.Path, "exists", fake_exists, raising=False)
    monkeypatch.setattr(path_resolver.Path, "read_text", fake_read_text, raising=False)

    assert _resolver()._is_container() is False


def test_resolve_base_path_container_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Container mode should use the dedicated data volume by default."""
    resolver = _resolver()
    resolver.env = "container"
    resolver.project_dir = Path("/tmp/project")
    monkeypatch.delenv("AKOSHA_DATA_PATH", raising=False)

    assert resolver._resolve_base_path() == Path("/data/akosha")


def test_resolve_base_path_local_delegates_to_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local mode should delegate to the XDG path helper."""
    resolver = _resolver()
    resolver.env = "local"
    resolver.project_dir = Path("/tmp/project")
    monkeypatch.delenv("AKOSHA_DATA_PATH", raising=False)
    monkeypatch.setattr(resolver, "_get_xdg_data_path", lambda: Path("/xdg/data"))

    assert resolver._resolve_base_path() == Path("/xdg/data")


@pytest.mark.parametrize(
    ("is_windows", "is_macos", "env_vars", "expected"),
    [
        (True, False, {"LOCALAPPDATA": "/win/local"}, Path("/win/local") / "akosha"),
        (True, False, {}, Path("/tmp/home") / ".akosha" / "data"),
        (False, True, {"XDG_DATA_HOME": "/mac/xdg"}, Path("/mac/xdg") / "akosha"),
        (
            False,
            True,
            {},
            Path("/tmp/home") / "Library" / "Application Support" / "akosha",
        ),
        (False, False, {"XDG_DATA_HOME": "/linux/xdg"}, Path("/linux/xdg") / "akosha"),
        (False, False, {}, Path("/tmp/home") / ".local" / "share" / "akosha"),
    ],
)
def test_get_xdg_data_path_platform_variants(
    monkeypatch: pytest.MonkeyPatch,
    is_windows: bool,
    is_macos: bool,
    env_vars: dict[str, str],
    expected: Path,
) -> None:
    """The XDG data path helper should honor each platform's fallback chain."""
    monkeypatch.setattr(path_resolver, "IS_WINDOWS", is_windows)
    monkeypatch.setattr(path_resolver, "IS_MACOS", is_macos)
    monkeypatch.setattr(path_resolver.Path, "home", lambda: Path("/tmp/home"))
    for key in ["LOCALAPPDATA", "XDG_DATA_HOME"]:
        monkeypatch.delenv(key, raising=False)
    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)

    assert _resolver()._get_xdg_data_path() == expected


@pytest.mark.parametrize(
    ("is_windows", "is_macos", "env_vars", "expected"),
    [
        (True, False, {"APPDATA": "/win/app"}, Path("/win/app") / "akosha"),
        (True, False, {}, Path("/tmp/home") / ".akosha" / "config"),
        (False, True, {"XDG_CONFIG_HOME": "/mac/config"}, Path("/mac/config") / "akosha"),
        (False, True, {}, Path("/tmp/home") / "Library" / "Preferences" / "akosha"),
        (False, False, {"XDG_CONFIG_HOME": "/linux/config"}, Path("/linux/config") / "akosha"),
        (False, False, {}, Path("/tmp/home") / ".config" / "akosha"),
    ],
)
def test_get_config_dir_platform_variants(
    monkeypatch: pytest.MonkeyPatch,
    is_windows: bool,
    is_macos: bool,
    env_vars: dict[str, str],
    expected: Path,
) -> None:
    """The config directory helper should honor each platform's fallback chain."""
    monkeypatch.setattr(path_resolver, "IS_WINDOWS", is_windows)
    monkeypatch.setattr(path_resolver, "IS_MACOS", is_macos)
    monkeypatch.setattr(path_resolver.Path, "home", lambda: Path("/tmp/home"))
    for key in ["APPDATA", "XDG_CONFIG_HOME"]:
        monkeypatch.delenv(key, raising=False)
    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)

    assert _resolver().get_config_dir() == expected


@pytest.mark.parametrize(
    ("is_windows", "is_macos", "env_vars", "expected"),
    [
        (True, False, {"LOCALAPPDATA": "/win/local"}, Path("/win/local") / "akosha" / "cache"),
        (True, False, {}, Path("/tmp/home") / ".akosha" / "cache"),
        (False, True, {"XDG_CACHE_HOME": "/mac/cache"}, Path("/mac/cache") / "akosha"),
        (False, True, {}, Path("/tmp/home") / "Library" / "Caches" / "akosha"),
        (False, False, {"XDG_CACHE_HOME": "/linux/cache"}, Path("/linux/cache") / "akosha"),
        (False, False, {}, Path("/tmp/home") / ".cache" / "akosha"),
    ],
)
def test_get_cache_dir_platform_variants(
    monkeypatch: pytest.MonkeyPatch,
    is_windows: bool,
    is_macos: bool,
    env_vars: dict[str, str],
    expected: Path,
) -> None:
    """The cache directory helper should honor each platform's fallback chain."""
    monkeypatch.setattr(path_resolver, "IS_WINDOWS", is_windows)
    monkeypatch.setattr(path_resolver, "IS_MACOS", is_macos)
    monkeypatch.setattr(path_resolver.Path, "home", lambda: Path("/tmp/home"))
    for key in ["LOCALAPPDATA", "XDG_CACHE_HOME"]:
        monkeypatch.delenv(key, raising=False)
    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)

    assert _resolver().get_cache_dir() == expected
