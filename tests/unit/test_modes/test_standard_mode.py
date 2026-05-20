"""Tests for standard mode."""

from __future__ import annotations

import builtins
import sys
from types import ModuleType

import pytest

from akosha.modes.standard import StandardMode


def test_standard_mode_config():
    """Test standard mode configuration."""
    mode = StandardMode(config={})

    assert mode.mode_config.name == "standard"
    assert (
        mode.mode_config.description
        == "Standard mode: Full production configuration with Redis and optional cold storage"
    )
    assert mode.mode_config.redis_enabled is True
    assert mode.mode_config.cold_storage_enabled is True
    assert mode.mode_config.cache_backend == "redis"


def test_standard_mode_requires_services():
    """Test that standard mode requires external services."""
    mode = StandardMode(config={})

    assert mode.requires_external_services is True


@pytest.mark.asyncio
async def test_standard_mode_initialize_cache_with_redis():
    """Test standard mode cache initialization with Redis."""
    # This test requires Redis to be running
    # Mark as integration test
    pytest.skip("Requires Redis - integration test")


@pytest.mark.asyncio
async def test_standard_mode_initialize_cache_without_redis():
    """Test standard mode cache initialization without Redis (fallback)."""
    mode = StandardMode(config={"redis_host": "invalid-host"})

    # Should return None but not raise exception
    cache = await mode.initialize_cache()
    assert cache is None  # Fallback to in-memory


@pytest.mark.asyncio
async def test_standard_mode_initialize_cache_success(monkeypatch):
    """Test standard mode cache initialization with a working Redis client."""

    class FakeRedis:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.ping_called = False

        def ping(self):
            self.ping_called = True

    fake_redis = ModuleType("redis")
    fake_redis.Redis = FakeRedis
    monkeypatch.setitem(sys.modules, "redis", fake_redis)

    mode = StandardMode(config={"redis_host": "redis.example", "redis_port": 6380, "redis_db": 2})

    cache = await mode.initialize_cache()

    assert isinstance(cache, FakeRedis)
    assert cache.ping_called is True
    assert cache.kwargs["host"] == "redis.example"
    assert cache.kwargs["port"] == 6380
    assert cache.kwargs["db"] == 2


@pytest.mark.asyncio
async def test_standard_mode_initialize_cache_import_error(monkeypatch):
    """Test standard mode cache initialization when redis is unavailable."""
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "redis":
            raise ImportError("redis missing")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    mode = StandardMode(config={})

    cache = await mode.initialize_cache()
    assert cache is None


@pytest.mark.asyncio
async def test_standard_mode_initialize_cold_storage():
    """Test standard mode cold storage initialization."""
    # This test requires cloud storage credentials
    # Mark as integration test
    pytest.skip("Requires cloud storage - integration test")


@pytest.mark.asyncio
async def test_standard_mode_initialize_cold_storage_fallback():
    """Test standard mode cold storage initialization without credentials (fallback)."""
    mode = StandardMode(config={"cold_bucket": None})

    # Falls back to local storage adapter when no cold bucket configured
    storage = await mode.initialize_cold_storage()
    assert storage is not None


@pytest.mark.asyncio
async def test_standard_mode_initialize_cold_storage_local_success(monkeypatch):
    """Test standard mode cold storage initialization with local backend."""

    import oneiric.adapters.storage as storage_mod

    class FakeLocalSettings:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeLocalAdapter:
        def __init__(self, settings):
            self.settings = settings

    class FakeS3Settings:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeS3Adapter:
        def __init__(self, settings):
            self.settings = settings

    monkeypatch.setattr(storage_mod, "LocalStorageSettings", FakeLocalSettings)
    monkeypatch.setattr(storage_mod, "LocalStorageAdapter", FakeLocalAdapter)
    monkeypatch.setattr(storage_mod, "S3StorageSettings", FakeS3Settings)
    monkeypatch.setattr(storage_mod, "S3StorageAdapter", FakeS3Adapter)

    mode = StandardMode(config={"cold_storage_backend": "local", "local_storage_path": "/tmp/cold"})

    storage = await mode.initialize_cold_storage()

    assert isinstance(storage, FakeLocalAdapter)
    assert storage.settings.kwargs["base_path"] == "/tmp/cold"


@pytest.mark.asyncio
async def test_standard_mode_initialize_cold_storage_s3_success(monkeypatch):
    """Test standard mode cold storage initialization with s3 backend."""

    import oneiric.adapters.storage as storage_mod

    class FakeLocalSettings:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeLocalAdapter:
        def __init__(self, settings):
            self.settings = settings

    class FakeS3Settings:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeS3Adapter:
        def __init__(self, settings):
            self.settings = settings

    monkeypatch.setattr(storage_mod, "LocalStorageSettings", FakeLocalSettings)
    monkeypatch.setattr(storage_mod, "LocalStorageAdapter", FakeLocalAdapter)
    monkeypatch.setattr(storage_mod, "S3StorageSettings", FakeS3Settings)
    monkeypatch.setattr(storage_mod, "S3StorageAdapter", FakeS3Adapter)

    mode = StandardMode(config={"cold_storage_backend": "s3", "cold_bucket": "demo-bucket"})

    storage = await mode.initialize_cold_storage()

    assert isinstance(storage, FakeS3Adapter)
    assert storage.settings.kwargs["bucket"] == "demo-bucket"


@pytest.mark.asyncio
async def test_standard_mode_initialize_cold_storage_unsupported_backend(monkeypatch):
    """Test standard mode cold storage fallback for unsupported backend."""
    import oneiric.adapters.storage as storage_mod

    class FakeLocalSettings:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeLocalAdapter:
        def __init__(self, settings):
            self.settings = settings

    class FakeS3Settings:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeS3Adapter:
        def __init__(self, settings):
            self.settings = settings

    monkeypatch.setattr(storage_mod, "LocalStorageSettings", FakeLocalSettings)
    monkeypatch.setattr(storage_mod, "LocalStorageAdapter", FakeLocalAdapter)
    monkeypatch.setattr(storage_mod, "S3StorageSettings", FakeS3Settings)
    monkeypatch.setattr(storage_mod, "S3StorageAdapter", FakeS3Adapter)

    mode = StandardMode(config={"cold_storage_backend": "weird", "local_storage_path": "/tmp/cold"})

    storage = await mode.initialize_cold_storage()
    assert isinstance(storage, FakeLocalAdapter)
    assert storage.settings.kwargs["base_path"] == "/tmp/cold"


@pytest.mark.asyncio
async def test_standard_mode_initialize_cold_storage_import_error(monkeypatch):
    """Test standard mode cold storage initialization when Oneiric is unavailable."""
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name.startswith("oneiric"):
            raise ImportError("oneiric missing")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    mode = StandardMode(config={})

    storage = await mode.initialize_cold_storage()
    assert storage is None


def test_standard_mode_repr():
    """Test standard mode string representation."""
    mode = StandardMode(config={})

    repr_str = repr(mode)
    assert "StandardMode" in repr_str
    assert "services_required=True" in repr_str
    assert "cache=redis" in repr_str
    assert "cold_storage=cloud" in repr_str
