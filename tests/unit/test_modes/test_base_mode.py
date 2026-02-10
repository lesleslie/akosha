"""Tests for base mode interface."""

from __future__ import annotations

import pytest

from akosha.modes.base import BaseMode, ModeConfig


class DummyMode(BaseMode):
    """Dummy mode for testing."""

    def get_mode_config(self) -> ModeConfig:
        return ModeConfig(
            name="dummy",
            description="Dummy mode for testing",
            redis_enabled=False,
            cold_storage_enabled=False,
            cache_backend="memory",
        )

    async def initialize_cache(self):
        return None

    async def initialize_cold_storage(self):
        return None

    @property
    def requires_external_services(self) -> bool:
        return False


def test_mode_config_creation():
    """Test ModeConfig creation."""
    config = ModeConfig(
        name="test",
        description="Test mode",
        redis_enabled=True,
        cold_storage_enabled=False,
        cache_backend="redis",
    )

    assert config.name == "test"
    assert config.redis_enabled is True
    assert config.cold_storage_enabled is False
    assert config.cache_backend == "redis"


def test_dummy_mode_initialization():
    """Test dummy mode initialization."""
    mode = DummyMode(config={})

    assert mode.mode_config.name == "dummy"
    assert mode.mode_config.redis_enabled is False
    assert mode.mode_config.cold_storage_enabled is False
    assert mode.mode_config.cache_backend == "memory"


def test_dummy_mode_requires_no_services():
    """Test that dummy mode requires no external services."""
    mode = DummyMode(config={})

    assert mode.requires_external_services is False


@pytest.mark.asyncio
async def test_dummy_mode_initialize_cache():
    """Test dummy mode cache initialization."""
    mode = DummyMode(config={})

    cache = await mode.initialize_cache()
    assert cache is None


@pytest.mark.asyncio
async def test_dummy_mode_initialize_cold_storage():
    """Test dummy mode cold storage initialization."""
    mode = DummyMode(config={})

    storage = await mode.initialize_cold_storage()
    assert storage is None


def test_mode_repr():
    """Test mode string representation."""
    mode = DummyMode(config={})

    repr_str = repr(mode)
    assert "Mode" in repr_str
    assert "services_required" in repr_str
    assert "False" in repr_str
