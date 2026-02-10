"""Tests for mode registry and get_mode function."""

from __future__ import annotations

import pytest

from akosha.modes import get_mode, list_modes, LiteMode, StandardMode


def test_list_modes():
    """Test listing all available modes."""
    modes = list_modes()

    assert isinstance(modes, list)
    assert "lite" in modes
    assert "standard" in modes


def test_get_lite_mode():
    """Test getting lite mode instance."""
    mode = get_mode("lite", config={})

    assert isinstance(mode, LiteMode)
    assert mode.mode_config.name == "lite"


def test_get_standard_mode():
    """Test getting standard mode instance."""
    mode = get_mode("standard", config={})

    assert isinstance(mode, StandardMode)
    assert mode.mode_config.name == "standard"


def test_get_mode_case_insensitive():
    """Test that get_mode is case-insensitive."""
    mode_lower = get_mode("lite", config={})
    mode_upper = get_mode("LITE", config={})
    mode_mixed = get_mode("LiTe", config={})

    assert isinstance(mode_lower, LiteMode)
    assert isinstance(mode_upper, LiteMode)
    assert isinstance(mode_mixed, LiteMode)


def test_get_invalid_mode():
    """Test getting invalid mode raises ValueError."""
    with pytest.raises(ValueError, match="Unknown mode"):
        get_mode("invalid", config={})


def test_get_mode_with_config():
    """Test getting mode with custom config."""
    config = {"redis_host": "custom-host", "redis_port": 6380}
    mode = get_mode("standard", config=config)

    assert isinstance(mode, StandardMode)
    assert mode.config == config
