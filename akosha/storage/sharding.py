"""Sharding layer: Consistent hashing router for 256 shards."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from akosha.config import config

logger = logging.getLogger(__name__)


class ShardRouter:
    """Consistent hashing router for 256 shards."""

    def __init__(self, num_shards: int | None = None) -> None:
        """Initialize shard router.

        Args:
            num_shards: Number of shards (default: from config.shard_count)
        """
        self.num_shards = num_shards if num_shards is not None else config.shard_count
        logger.info(f"ShardRouter initialized with {self.num_shards} shards")

    def get_shard(self, system_id: str) -> int:
        """Get shard ID for a system.

        Args:
            system_id: System identifier

        Returns:
            Shard ID (0 to num_shards-1)
        """
        # Hash system_id with SHA-256
        hash_bytes = hashlib.sha256(system_id.encode("utf-8")).digest()

        # Convert first 4 bytes to integer
        hash_int = int.from_bytes(hash_bytes[:4], byteorder="big")

        # Modulo num_shards to get shard ID
        shard_id = hash_int % self.num_shards

        return shard_id

    def get_shard_path(
        self, system_id: str, base_path: Path | None = None
    ) -> Path:
        """Get database path for a system's shard.

        Args:
            system_id: System identifier
            base_path: Base path for shard storage (default: config.warm.path)

        Returns:
            Path like: /data/akosha/warm/shard_123/system-001.duckdb
        """
        # Get shard ID
        shard_id = self.get_shard(system_id)

        # Use config.warm.path as default base path
        if base_path is None:
            base_path = config.warm.path

        # Build path: base/shard_XXX/system-id.duckdb
        shard_dir = base_path / f"shard_{shard_id:03d}"
        db_path = shard_dir / f"{system_id}.duckdb"

        return db_path

    def get_target_shards(self, system_id: str | None = None) -> list[int]:
        """Get target shards for a query.

        Args:
            system_id: Optional system filter

        Returns:
            List of shard IDs to query
        """
        # If system_id provided: return [get_shard(system_id)]
        if system_id is not None:
            return [self.get_shard(system_id)]

        # Otherwise: return list(range(self.num_shards))
        return list(range(self.num_shards))
