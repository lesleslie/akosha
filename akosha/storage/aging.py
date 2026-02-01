"""Tier aging service: Hot->Warm->Cold data migration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from akosha.storage.hot_store import HotStore
    from akosha.storage.warm_store import WarmStore

logger = logging.getLogger(__name__)


@dataclass
class MigrationStats:
    """Statistics for tier migration."""

    records_migrated: int = 0
    bytes_freed: int = 0
    errors: int = 0
    start_time: datetime | None = None
    end_time: datetime | None = None


class AgingService:
    """Service for migrating data between storage tiers."""

    def __init__(self, hot_store: HotStore, warm_store: WarmStore) -> None:
        """Initialize aging service.

        Args:
            hot_store: Hot store instance (source)
            warm_store: Warm store instance (destination)
        """
        self.hot_store = hot_store
        self.warm_store = warm_store
        logger.info("Aging service initialized")

    async def migrate_hot_to_warm(self, cutoff_days: int = 7) -> MigrationStats:
        """Migrate records from hot to warm tier.

        For records older than cutoff_days:
            1. Compress embedding: FLOAT[384] -> INT8[384]
            2. Generate 3-sentence summary
            3. Insert into warm store
            4. Verify checksum
            5. Delete from hot store

        Args:
            cutoff_days: Migrate records older than this many days

        Returns:
            Migration statistics
        """
        start_time = datetime.now(UTC)
        stats = MigrationStats(start_time=start_time)
        cutoff_date = datetime.now(UTC) - timedelta(days=cutoff_days)

        logger.info(f"Starting hot->warm migration for records older than {cutoff_date}")

        # Get records to migrate
        records_to_migrate = await self._get_eligible_records(cutoff_date)
        total_records = len(records_to_migrate)

        if total_records == 0:
            logger.info("No records eligible for migration")
            stats.end_time = datetime.now(UTC)
            return stats

        logger.info(f"Found {total_records} records eligible for migration")

        # Process each record
        for idx, hot_record in enumerate(records_to_migrate, 1):
            try:
                # Step 1: Compress embedding (FLOAT -> INT8)
                compressed_embedding = await self._quantize_embedding(
                    hot_record["embedding"]
                )

                # Step 2: Generate summary
                summary = await self._generate_summary(hot_record["content"])

                # Step 3: Insert into warm store
                from akosha.models import WarmRecord

                warm_record = WarmRecord(
                    system_id=hot_record["system_id"],
                    conversation_id=hot_record["conversation_id"],
                    embedding=compressed_embedding,
                    summary=summary,
                    timestamp=hot_record["timestamp"],
                    metadata=hot_record["metadata"],
                )
                await self.warm_store.insert(warm_record)

                # Step 4: Verify integrity (checksum comparison)
                hot_checksum = hot_record.get("content_hash", "")
                if hot_checksum:
                    warm_checksum = self._compute_checksum(summary)
                    if not self._verify_checksum_compatibility(hot_checksum, warm_checksum):
                        logger.warning(
                            f"Checksum mismatch for {hot_record['conversation_id']}, "
                            "continuing anyway"
                        )

                # Step 5: Delete from hot store
                await self._delete_from_hot_store(hot_record["conversation_id"])

                stats.records_migrated += 1

                # Calculate approximate bytes freed
                # Full content (avg 2KB) + float embedding (1.5KB) - int8 embedding (384B) - summary (500B)
                stats.bytes_freed += 2500

                # Log progress every 100 records
                if idx % 100 == 0:
                    logger.info(f"Migration progress: {idx}/{total_records} records")

            except Exception as e:
                stats.errors += 1
                logger.error(
                    f"Failed to migrate record {hot_record.get('conversation_id', 'unknown')}: {e}"
                )

        stats.end_time = datetime.now(UTC)
        duration = (stats.end_time - start_time).total_seconds()

        logger.info(
            f"Migration complete: {stats.records_migrated} records migrated, "
            f"{stats.errors} errors, {stats.bytes_freed / 1024 / 1024:.2f} MB freed "
            f"in {duration:.2f}s"
        )

        return stats

    async def _get_eligible_records(self, cutoff_date: datetime) -> list[dict]:
        """Get records older than cutoff date from hot store.

        Args:
            cutoff_date: Cutoff datetime for migration eligibility

        Returns:
            List of hot records ready for migration
        """
        if not self.hot_store.conn:
            raise RuntimeError("Hot store not initialized")

        # Query records older than cutoff date
        result = self.hot_store.conn.execute(
            """
            SELECT
                system_id,
                conversation_id,
                content,
                embedding,
                timestamp,
                metadata,
                content_hash
            FROM conversations
            WHERE timestamp < ?
            ORDER BY timestamp ASC
        """,
            [cutoff_date],
        ).fetchall()

        return [
            {
                "system_id": row[0],
                "conversation_id": row[1],
                "content": row[2],
                "embedding": row[3],
                "timestamp": row[4],
                "metadata": row[5],
                "content_hash": row[6],
            }
            for row in result
        ]

    async def _quantize_embedding(self, float_embedding: list[float]) -> list[int]:
        """Quantize float embedding to INT8.

        TODO: Implement proper quantization:
            - Scale values to [-127, 126] range
            - Handle outliers with clipping
            - Consider preserving precision with scaling factor

        Args:
            float_embedding: FLOAT[384] embedding

        Returns:
            INT8[384] quantized embedding
        """
        # Placeholder: simple rounding without proper scaling
        # In production, this should use proper quantization:
        # - Calculate scale factor: scale = 127 / max(abs(embedding))
        # - Clip and convert: int(min(max(v * scale, -127), 126))
        return [int(v * 127) for v in float_embedding]

    async def _generate_summary(self, content: str) -> str:
        """Generate 3-sentence extractive summary.

        TODO: Implement proper summarization:
            - Use extractive summarization (TextRank, LexRank)
            - Or abstractive (transformer model)
            - Ensure exactly 3 sentences
            - Preserve key information

        Args:
            content: Full content to summarize

        Returns:
            3-sentence summary
        """
        # Placeholder: return first 3 sentences
        sentences = [s.strip() for s in content.split(".") if s.strip()]
        if len(sentences) <= 3:
            return ". ".join(sentences)
        return ". ".join(sentences[:3])

    def _compute_checksum(self, content: str) -> str:
        """Compute SHA-256 checksum for verification.

        Args:
            content: Content to hash

        Returns:
            Hexadecimal checksum
        """
        import hashlib

        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _verify_checksum_compatibility(
        self, hot_checksum: str, warm_checksum: str
    ) -> bool:
        """Verify that warm record is derived from hot record.

        Note: Checksums won't match exactly (content vs summary),
        but we can verify format and prefix compatibility.

        Args:
            hot_checksum: Original hot store checksum
            warm_checksum: Warm store summary checksum

        Returns:
            True if checksums are compatible
        """
        # Placeholder: basic format validation
        # In production, this might verify:
        # - Both are valid SHA-256 hex strings
        # - Same system_id prefix in hash
        # - Compatible timestamp ranges
        return len(hot_checksum) == len(warm_checksum) == 64

    async def _delete_from_hot_store(self, conversation_id: str) -> None:
        """Delete migrated record from hot store.

        Args:
            conversation_id: Conversation ID to delete
        """
        if not self.hot_store.conn:
            raise RuntimeError("Hot store not initialized")

        self.hot_store.conn.execute(
            "DELETE FROM conversations WHERE conversation_id = ?",
            [conversation_id],
        )

    async def get_migration_stats(self) -> dict[str, int]:
        """Get current statistics about tier sizes.

        Returns:
            Dictionary with record counts for each tier
        """
        hot_count = 0
        warm_count = 0

        if self.hot_store.conn:
            result = self.hot_store.conn.execute(
                "SELECT COUNT(*) FROM conversations"
            ).fetchone()
            hot_count = result[0] if result else 0

        if self.warm_store.conn:
            result = self.warm_store.conn.execute(
                "SELECT COUNT(*) FROM conversations"
            ).fetchone()
            warm_count = result[0] if result else 0

        return {
            "hot_records": hot_count,
            "warm_records": warm_count,
        }
