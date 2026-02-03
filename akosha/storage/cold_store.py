"""Cold store: Parquet export to S3/R2 for archival data."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.parquet as pq

if TYPE_CHECKING:
    from akosha.models import ColdRecord

logger = logging.getLogger(__name__)


class ColdStore:
    """Cold store with Parquet export to S3/R2."""

    def __init__(self, bucket: str, prefix: str = "conversations/") -> None:
        """Initialize cold store.

        Args:
            bucket: S3/R2 bucket name
            prefix: Object key prefix
        """
        self.bucket = bucket
        self.prefix = prefix
        # TODO: Initialize Oneiric storage adapter
        self._storage_adapter: Any | None = None  # Placeholder for Oneiric adapter

    async def export_batch(
        self,
        records: list[ColdRecord],
        partition_path: str,
    ) -> str:
        """Export records to Parquet format.

        Steps:
            1. Convert records to PyArrow table with proper schema
            2. Write to temporary Parquet file
            3. Upload to S3/R2 via Oneiric storage adapter
            4. Return object key

        Args:
            records: ColdRecord objects to export
            partition_path: Partition path (e.g., "system-001/2025/01/31")

        Returns:
            S3 object key (e.g., "conversations/system-001/2025/01/31/batch.parquet")
        """
        if not records:
            logger.warning("Empty record batch provided for export")
            raise ValueError("Cannot export empty batch")

        try:
            # Step 1: Convert records to PyArrow table
            table = self._records_to_arrow_table(records)

            # Step 2: Write to temporary Parquet file
            parquet_key = f"{self.prefix}{partition_path}/batch_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.parquet"
            temp_path = await self._write_parquet_file(table)

            # Step 3: Upload to S3/R2
            await self._upload_to_storage(temp_path, parquet_key)

            logger.info(f"Successfully exported {len(records)} records to {parquet_key}")
            return parquet_key

        except Exception as e:
            logger.error(f"Failed to export batch to {partition_path}: {e}")
            raise

    def _records_to_arrow_table(self, records: list[ColdRecord]) -> pa.Table:
        """Convert ColdRecord objects to PyArrow Table.

        Args:
            records: List of ColdRecord objects

        Returns:
            PyArrow Table with proper schema
        """
        data = {
            "system_id": [],
            "conversation_id": [],
            "fingerprint": [],
            "ultra_summary": [],
            "timestamp": [],
            "daily_metrics": [],
        }

        for record in records:
            data["system_id"].append(record.system_id)
            data["conversation_id"].append(record.conversation_id)
            data["fingerprint"].append(record.fingerprint)
            data["ultra_summary"].append(record.ultra_summary)
            data["timestamp"].append(record.timestamp)
            # Serialize dict to JSON string for Parquet storage
            data["daily_metrics"].append(json.dumps(record.daily_metrics))

        # Define schema with proper types
        schema = pa.schema(
            [
                ("system_id", pa.string()),
                ("conversation_id", pa.string()),
                ("fingerprint", pa.binary()),
                ("ultra_summary", pa.string()),
                ("timestamp", pa.timestamp("ns")),
                ("daily_metrics", pa.string()),  # JSON as string for compatibility
            ]
        )

        return pa.Table.from_arrays(
            [pa.array(values, type=schema.field(i).type) for i, values in enumerate(data.values())],
            schema=schema,
        )

    async def _write_parquet_file(self, table: pa.Table) -> Path:
        """Write PyArrow table to temporary Parquet file.

        Uses secure tempfile creation to prevent symlink attacks:
        - Cryptographically random filename (unpredictable)
        - Mode 0600 (owner read/write only)
        - Atomic file creation

        Args:
            table: PyArrow table to write

        Returns:
            Path to temporary Parquet file
        """
        # Create temp directory if it doesn't exist
        temp_dir = Path(tempfile.gettempdir()) / "akosha_cold_export"
        temp_dir.mkdir(exist_ok=True, mode=0o700)  # Owner-only directory

        # Use tempfile.mkstemp for secure temporary file creation
        # This creates a file with:
        # - Cryptographically random filename (prevents prediction)
        # - Mode 0600 (owner read/write only, prevents other users from reading)
        # - Atomic creation (prevents race conditions)
        fd, temp_path = tempfile.mkstemp(
            suffix=".parquet",
            prefix="akosha_export_",
            dir=str(temp_dir),
            text=False,  # Binary mode
        )
        temp_file = Path(temp_path)

        # Set explicit permissions (defense in depth)
        # Note: Using os.chmod() with fd is correct for tempfile.mkstemp()
        os.chmod(fd, 0o600)  # noqa: PTH101  # Owner read/write only

        try:
            # Write with compression for efficiency
            # We need to use the file descriptor directly for security
            with os.fdopen(fd, "wb") as f:
                pq.write_table(
                    table,
                    f,
                    compression="snappy",  # Fast compression/decompression
                    write_statistics=True,
                    use_dictionary=True,
                )

            logger.debug(f"Created secure temporary Parquet file: {temp_file}")
            return temp_file

        except Exception as e:
            logger.error(f"Failed to write Parquet file {temp_file}: {e}")
            # Clean up file descriptor and file
            with contextlib.suppress(Exception):
                os.close(fd)
            if temp_file.exists():
                temp_file.unlink()
            raise

    async def _upload_to_storage(self, temp_path: Path, object_key: str) -> None:
        """Upload file to S3/R2 via Oneiric storage adapter.

        Args:
            temp_path: Path to local file to upload
            object_key: S3/R2 object key
        """
        try:
            # TODO: Implement Oneiric storage adapter integration
            # For now, just log the operation
            logger.info(f"Would upload {temp_path} to s3://{self.bucket}/{object_key}")

            # Clean up temp file after successful upload
            if temp_path.exists():
                temp_path.unlink()
                logger.debug(f"Cleaned up temporary file: {temp_path}")

        except Exception as e:
            logger.error(f"Failed to upload {temp_path}: {e}")
            # Clean up temp file on error too
            if temp_path.exists():
                temp_path.unlink()
            raise

    async def initialize(self) -> None:
        """Initialize cold store (placeholder for future extensions)."""
        # TODO: Initialize Oneiric storage adapter
        logger.info("Cold store initialized")

    async def close(self) -> None:
        """Clean up resources (placeholder for future extensions)."""
        logger.info("Cold store closed")
