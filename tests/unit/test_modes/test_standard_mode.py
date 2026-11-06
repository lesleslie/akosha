"""Tests for standard mode."""

from __future__ import annotations

import builtins
import sys
from pathlib import Path
from types import ModuleType

import pytest

from akosha.modes.standard import StandardMode
from akosha.storage.cold_store import ColdStore


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
async def test_standard_mode_initialize_cold_storage_local_success():
    """Test standard mode cold storage initialization with local backend."""
    mode = StandardMode(config={"cold_storage_backend": "local", "local_storage_path": "/tmp/cold"})

    storage = await mode.initialize_cold_storage()

    assert isinstance(storage, ColdStore)
    assert storage._storage_backend == "local"
    assert storage._local_dir == Path("/tmp/cold")


@pytest.mark.asyncio
async def test_standard_mode_initialize_cold_storage_s3_success():
    """Test standard mode cold storage initialization with s3 backend."""
    mode = StandardMode(config={"cold_storage_backend": "s3", "cold_bucket": "demo-bucket"})

    storage = await mode.initialize_cold_storage()

    assert isinstance(storage, ColdStore)
    assert storage._storage_backend == "s3"
    assert storage.bucket == "demo-bucket"


@pytest.mark.asyncio
async def test_standard_mode_initialize_cold_storage_unsupported_backend():
    """Test standard mode cold storage fallback for unsupported backend."""
    mode = StandardMode(config={"cold_storage_backend": "weird", "local_storage_path": "/tmp/cold"})

    storage = await mode.initialize_cold_storage()

    assert isinstance(storage, ColdStore)
    assert storage._storage_backend == "local"
    assert storage._local_dir == Path("/tmp/cold")


@pytest.mark.asyncio
async def test_standard_mode_cold_storage_carries_the_prefix():
    """The configured prefix reaches the cold store that builds the key.

    The prefix is the substrate's per-component isolation mechanism, and
    no oneiric storage settings object has a ``prefix`` field -- the only
    place it can live is the object that assembles the object key.
    """
    mode = StandardMode(config={"cold_storage_backend": "local", "local_storage_path": "/tmp/cold"})

    storage = await mode.initialize_cold_storage()

    assert isinstance(storage, ColdStore)
    assert storage.prefix.endswith("/")


@pytest.mark.asyncio
async def test_standard_mode_cold_storage_honours_the_flat_prefix_key():
    """A caller-supplied flat ``cold_prefix`` still wins.

    The flat keys are the CLI's explicit ``--config`` form. The prefix
    used to be read from them before the nested-``cold:``-block fix; it
    has to keep working, because the prefix decides which key space in
    the bucket Akosha writes to.
    """
    mode = StandardMode(
        config={
            "cold_storage_backend": "local",
            "local_storage_path": "/tmp/cold",
            "cold_prefix": "flat-prefix/",
        }
    )

    storage = await mode.initialize_cold_storage()

    assert isinstance(storage, ColdStore)
    assert storage.prefix == "flat-prefix/"


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
