"""Storage path resolver for Akosha.

Implements environment-aware path resolution with XDG Base Directory compliance.
Supports local development, containerized production, and testing environments.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

# Platform detection
IS_WINDOWS: Final = sys.platform == "win32"
IS_MACOS: Final = sys.platform == "darwin"
IS_LINUX: Final = sys.platform.startswith("linux")


class StoragePathResolver:
    """Resolves storage paths based on deployment environment."""

    def __init__(self, env: str | None = None, project_dir: Path | None = None) -> None:
        """Initialize path resolver.

        Args:
            env: Force specific environment ('local', 'container', 'development', 'test')
            project_dir: Current project directory (defaults to cwd)
        """
        self.env = env or self._detect_environment()
        self.project_dir = project_dir or Path.cwd()
        self.base_path = self._resolve_base_path()

        # Warn if using legacy path
        self._warn_if_legacy()

    def _detect_environment(self) -> str:
        """Auto-detect deployment environment.

        Returns:
            Environment type: 'container', 'local', 'development', or 'test'
        """
        # Check for explicit environment variable
        if env_var := os.getenv("AKOSHA_ENV"):
            return env_var

        # Container detection
        if self._is_container():
            return "container"

        # Check if running in tests
        if "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST"):
            return "test"

        # Default to local development
        return "local"

    def _is_container(self) -> bool:
        """Check if running in a container.

        Returns:
            True if running in Docker/Podman/container
        """
        # Check for Docker marker file
        if Path("/.dockerenv").exists():
            return True

        # Check for container cgroup
        if Path("/proc/1/cgroup").exists():
            try:
                cgroup = Path("/proc/1/cgroup").read_text()
                if "/docker/" in cgroup or "/kubepods/" in cgroup:
                    return True
            except (OSError, IOError):
                pass

        return False

    def _resolve_base_path(self) -> Path:
        """Resolve base storage path by environment.

        Returns:
            Base path for Akosha data storage
        """
        # Check for explicit override (highest priority)
        if override := os.getenv("AKOSHA_DATA_PATH"):
            return Path(override)

        if self.env == "container":
            # Production: Use dedicated data volume
            return Path(os.getenv("AKOSHA_DATA_PATH", "/data/akosha"))

        elif self.env == "local":
            # Local dev: XDG compliance with fallbacks
            return self._get_xdg_data_path()

        elif self.env == "development":
            # Dev mode: Project-scoped but isolated
            return self.project_dir / ".akosha" / "data"

        elif self.env == "test":
            # Testing: Use /tmp for ephemeral data
            return Path("/tmp") / "akosha" / "test"

        else:
            # Fallback
            logger.warning(f"Unknown environment '{self.env}', using local development paths")
            return self._get_xdg_data_path()

    def _get_xdg_data_path(self) -> Path:
        """Get XDG data directory path.

        Returns:
            Path to XDG data directory for Akosha
        """
        if IS_WINDOWS:
            # Windows: %LOCALAPPDATA%\akosha
            local_app_data = os.getenv("LOCALAPPDATA")
            if local_app_data:
                return Path(local_app_data) / "akosha"
            # Fallback
            return Path.home() / ".akosha" / "data"

        if IS_MACOS:
            # macOS: Prefer XDG, fallback to traditional
            xdg_data = os.getenv("XDG_DATA_HOME")
            if xdg_data:
                return Path(xdg_data) / "akosha"
            # Traditional macOS location
            return Path.home() / "Library" / "Application Support" / "akosha"

        # Linux and others: XDG_DATA_HOME with fallback
        xdg_data = os.getenv("XDG_DATA_HOME")
        if xdg_data:
            return Path(xdg_data) / "akosha"
        # Standard XDG fallback
        return Path.home() / ".local" / "share" / "akosha"

    def _warn_if_legacy(self) -> None:
        """Warn if legacy project-local data path is detected."""
        legacy_path = self.project_dir / "data"
        if legacy_path.exists() and any(legacy_path.iterdir()):
            logger.warning(
                f"⚠️  Legacy data path detected: {legacy_path}\n"
                f"    New location: {self.base_path}\n"
                f"    Run 'akosha migrate' to transfer data.\n"
                f"    Legacy path will be ignored in future versions."
            )

    def get_warm_store_path(self) -> Path:
        """Get warm store database path.

        Returns:
            Path to warm.db file
        """
        # Check for explicit override
        if override := os.getenv("AKOSHA_WARM_PATH"):
            return Path(override)

        return self.base_path / "warm" / "warm.db"

    def get_warm_store_dir(self) -> Path:
        """Get warm store directory (parent of warm.db).

        Returns:
            Path to warm directory
        """
        return self.get_warm_store_path().parent

    def get_hot_store_wal_path(self) -> Path:
        """Get hot store WAL path.

        Returns:
            Path to WAL directory
        """
        # Check for explicit override
        if override := os.getenv("AKOSHA_WAL_PATH"):
            return Path(override)

        return self.base_path / "wal"

    def get_cold_store_cache_path(self) -> Path:
        """Get cold store local cache path.

        Returns:
            Path to cold storage cache
        """
        return self.base_path / "cold" / "cache"

    def get_config_dir(self) -> Path:
        """Get configuration directory.

        Returns:
            Path to config directory
        """
        if IS_WINDOWS:
            app_data = os.getenv("APPDATA")
            if app_data:
                return Path(app_data) / "akosha"
            return Path.home() / ".akosha" / "config"

        # macOS/Linux: XDG_CONFIG_HOME
        xdg_config = os.getenv("XDG_CONFIG_HOME")
        if xdg_config:
            return Path(xdg_config) / "akosha"

        # Fallbacks
        if IS_MACOS:
            return Path.home() / "Library" / "Preferences" / "akosha"
        return Path.home() / ".config" / "akosha"

    def get_cache_dir(self) -> Path:
        """Get cache directory.

        Returns:
            Path to cache directory
        """
        if IS_WINDOWS:
            local_app_data = os.getenv("LOCALAPPDATA")
            if local_app_data:
                return Path(local_app_data) / "akosha" / "cache"
            return Path.home() / ".akosha" / "cache"

        # macOS/Linux: XDG_CACHE_HOME
        xdg_cache = os.getenv("XDG_CACHE_HOME")
        if xdg_cache:
            return Path(xdg_cache) / "akosha"

        # Fallbacks
        if IS_MACOS:
            return Path.home() / "Library" / "Caches" / "akosha"
        return Path.home() / ".cache" / "akosha"

    def ensure_directories(self) -> None:
        """Create all necessary directories if they don't exist."""
        directories = [
            self.get_warm_store_dir(),
            self.get_hot_store_wal_path(),
            self.get_cold_store_cache_path(),
            self.get_config_dir(),
            self.get_cache_dir(),
        ]

        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)

        logger.debug(f"Ensured directories exist: {self.base_path}")


def get_default_resolver() -> StoragePathResolver:
    """Get default path resolver instance.

    Returns:
        StoragePathResolver for current environment
    """
    return StoragePathResolver()


# Convenience functions for common use cases
def get_warm_store_path() -> Path:
    """Get warm store database path using default resolver.

    Returns:
        Path to warm.db
    """
    return get_default_resolver().get_warm_store_path()


def get_config_dir() -> Path:
    """Get config directory using default resolver.

    Returns:
        Path to config directory
    """
    return get_default_resolver().get_config_dir()
