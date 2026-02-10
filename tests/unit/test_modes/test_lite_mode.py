"""Tests for lite mode."""

from __future__ import annotations

import pytest

from akosha.modes.lite import LiteMode


def test_lite_mode_config():
    """Test lite mode configuration."""
    mode = LiteMode(config={})

    assert mode.mode_config.name == "lite"
    assert mode.mode_config.description == "Lite mode: In-memory only, zero external dependencies"
    assert mode.mode_config.redis_enabled is False
    assert mode.mode_config.cold_storage_enabled is False
    assert mode.mode_config.cache_backend == "memory"


def test_lite_mode_requires_no_services():
    """Test that lite mode requires no external services."""
    mode = LiteMode(config={})

    assert mode.requires_external_services is False


@pytest.mark.asyncio
async def test_lite_mode_initialize_cache():
    """Test lite mode cache initialization (in-memory)."""
    mode = LiteMode(config={})

    cache = await mode.initialize_cache()
    assert cache is None  # None indicates in-memory cache


@pytest.mark.asyncio
async def test_lite_mode_initialize_cold_storage():
    """Test lite mode cold storage initialization (disabled)."""
    mode = LiteMode(config={})

    storage = await mode.initialize_cold_storage()
    assert storage is None  # Cold storage disabled in lite mode


def test_lite_mode_repr():
    """Test lite mode string representation."""
    mode = LiteMode(config={})

    repr_str = repr(mode)
    assert "LiteMode" in repr_str
    assert "services_required=False" in repr_str
    assert "cache=in-memory" in repr_str
    assert "cold_storage=disabled" in repr_str
