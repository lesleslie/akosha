"""Tests for standard mode."""

from __future__ import annotations

import pytest

from akosha.modes.standard import StandardMode


def test_standard_mode_config():
    """Test standard mode configuration."""
    mode = StandardMode(config={})

    assert mode.mode_config.name == "standard"
    assert mode.mode_config.description == "Standard mode: Full production configuration with Redis and cloud storage"
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
async def test_standard_mode_initialize_cold_storage():
    """Test standard mode cold storage initialization."""
    # This test requires cloud storage credentials
    # Mark as integration test
    pytest.skip("Requires cloud storage - integration test")


@pytest.mark.asyncio
async def test_standard_mode_initialize_cold_storage_fallback():
    """Test standard mode cold storage initialization without credentials (fallback)."""
    mode = StandardMode(config={"cold_bucket": None})

    # Should return None but not raise exception
    storage = await mode.initialize_cold_storage()
    assert storage is None  # Cold storage unavailable


def test_standard_mode_repr():
    """Test standard mode string representation."""
    mode = StandardMode(config={})

    repr_str = repr(mode)
    assert "StandardMode" in repr_str
    assert "services_required=True" in repr_str
    assert "cache=redis" in repr_str
    assert "cold_storage=cloud" in repr_str
