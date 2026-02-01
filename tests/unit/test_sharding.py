"""Tests for shard router (consistent hashing)."""

from __future__ import annotations

from pathlib import Path

import pytest

from akosha.storage.sharding import ShardRouter


class TestShardRouter:
    """Test suite for ShardRouter."""

    @pytest.fixture
    def router_256(self) -> ShardRouter:
        """Create router with 256 shards (default)."""
        return ShardRouter(num_shards=256)

    @pytest.fixture
    def router_16(self) -> ShardRouter:
        """Create router with 16 shards."""
        return ShardRouter(num_shards=16)

    def test_initialization_default(self) -> None:
        """Test router initialization with default shard count."""
        router = ShardRouter()
        assert router.num_shards == 256  # Default from config

    def test_initialization_custom(self) -> None:
        """Test router initialization with custom shard count."""
        router = ShardRouter(num_shards=16)
        assert router.num_shards == 16

    def test_get_shard_consistency(self, router_256: ShardRouter) -> None:
        """Test that same system_id always maps to same shard."""
        system_id = "system-123"
        shard1 = router_256.get_shard(system_id)
        shard2 = router_256.get_shard(system_id)
        shard3 = router_256.get_shard(system_id)

        # All calls should return same shard
        assert shard1 == shard2 == shard3

    def test_get_shard_distribution(self, router_16: ShardRouter) -> None:
        """Test that system_ids distribute across shards."""
        # Test with many system_ids
        system_ids = [f"system-{i}" for i in range(100)]
        shards = [router_16.get_shard(sid) for sid in system_ids]

        # Should use multiple shards (not all in one)
        unique_shards = set(shards)
        assert len(unique_shards) > 10  # At least 10 different shards used

        # All shard IDs should be in valid range
        for shard in shards:
            assert 0 <= shard < 16

    def test_get_shard_different_systems(self, router_256: ShardRouter) -> None:
        """Test that different system_ids map to different shards."""
        shard_a = router_256.get_shard("system-alpha")
        shard_b = router_256.get_shard("system-beta")
        shard_c = router_256.get_shard("system-gamma")

        # Most should be different (statistically)
        # At least one should be different
        assert not (shard_a == shard_b == shard_c)

    def test_get_shard_path(self, router_256: ShardRouter) -> None:
        """Test shard path construction."""
        from akosha.config import config

        system_id = "system-456"
        shard_id = router_256.get_shard(system_id)
        path = router_256.get_shard_path(system_id)

        # Path should be: base/shard_XXX/system-456.duckdb
        expected = config.warm.path / f"shard_{shard_id:03d}" / f"{system_id}.duckdb"
        assert path == expected

    def test_get_shard_path_custom_base(self, router_256: ShardRouter) -> None:
        """Test shard path with custom base path."""
        system_id = "system-789"
        custom_base = Path("/custom/path")
        path = router_256.get_shard_path(system_id, base_path=custom_base)

        # Should use custom base path
        shard_id = router_256.get_shard(system_id)
        expected = custom_base / f"shard_{shard_id:03d}" / f"{system_id}.duckdb"
        assert path == expected

    def test_get_target_shards_all(self, router_16: ShardRouter) -> None:
        """Test getting all target shards (no system filter)."""
        shards = router_16.get_target_shards()

        # Should return all shards
        assert shards == list(range(16))

    def test_get_target_shards_single(self, router_256: ShardRouter) -> None:
        """Test getting single target shard (with system filter)."""
        system_id = "system-specific"
        shards = router_256.get_target_shards(system_id)

        # Should return list with one shard
        assert len(shards) == 1
        assert shards[0] == router_256.get_shard(system_id)

    def test_get_target_shards_none_filter(self, router_256: ShardRouter) -> None:
        """Test get_target_shards with explicit None."""
        shards = router_256.get_target_shards(system_id=None)

        # Should return all shards
        assert len(shards) == 256
        assert shards == list(range(256))

    def test_shard_id_range(self, router_256: ShardRouter) -> None:
        """Test that all shard IDs are in valid range."""
        # Test many system_ids
        for i in range(1000):
            system_id = f"system-{i}"
            shard = router_256.get_shard(system_id)
            assert 0 <= shard < 256

    def test_empty_system_id(self, router_256: ShardRouter) -> None:
        """Test handling of empty system_id."""
        shard = router_256.get_shard("")
        # Should still return valid shard ID
        assert 0 <= shard < 256

    def test_special_characters_system_id(self, router_256: ShardRouter) -> None:
        """Test handling of special characters in system_id."""
        special_ids = [
            "system-with-dashes",
            "system_with_underscores",
            "system.with.dots",
            "system/with/slashes",
            "system:with:colons",
        ]

        # All should produce valid shard IDs
        for system_id in special_ids:
            shard = router_256.get_shard(system_id)
            assert 0 <= shard < 256

    def test_very_long_system_id(self, router_256: ShardRouter) -> None:
        """Test handling of very long system_id."""
        long_id = "system-" + "x" * 10000
        shard = router_256.get_shard(long_id)
        # Should still return valid shard ID
        assert 0 <= shard < 256
