"""Cold store: Parquet export to local/S3/GCS/Azure for archival data."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq

from oneiric.adapters.storage.azure import AzureBlobStorageAdapter, AzureBlobStorageSettings
from oneiric.adapters.storage.gcs import GCSStorageAdapter, GCSStorageSettings
from oneiric.adapters.storage.local import LocalStorageAdapter, LocalStorageSettings
from oneiric.adapters.storage.s3 import S3StorageAdapter, S3StorageSettings

if TYPE_CHECKING:
    from akosha.models import ColdRecord

logger = logging.getLogger(__name__)

StorageBackendName = Literal["local", "s3", "gcs", "azure"]


class ColdStore:
    """Cold store with Parquet export to local/S3/GCS/Azure.

    Backends are wired through oneiric storage adapters. R2 (Cloudflare) is
    S3-compatible and uses the ``s3`` backend with a custom ``endpoint_url``.
    """

    def __init__(
        self,
        bucket: str = "",
        prefix: str = "conversations/",
        *,
        storage_backend: StorageBackendName = "s3",
        local_dir: Path | None = None,
        # S3 / R2
        region: str | None = None,
        endpoint_url: str | None = None,
        # GCS
        project: str | None = None,
        credentials_file: Path | None = None,
        # Azure Blob
        container: str | None = None,
        connection_string: str | None = None,
        account_url: str | None = None,
    ) -> None:
        """Initialize cold store.

        Args:
            bucket: S3 or GCS bucket name. Required when ``storage_backend`` is
                ``"s3"`` or ``"gcs"``; unused for ``"local"`` and ``"azure"``.
                Kept positional/backwards-compatible with the prior signature.
            prefix: Object key prefix prepended to every export.
            storage_backend: One of ``"local"``, ``"s3"``, ``"gcs"``, ``"azure"``.
                ``"s3"`` is the default to preserve historical behavior.
            local_dir: Filesystem root for the ``"local"`` backend. Defaults to
                ``~/.akosha/cold-store`` when unset.
            region: S3 region (``s3`` backend only).
            endpoint_url: S3 endpoint URL (use R2's endpoint for Cloudflare R2).
            project: GCP project (``gcs`` backend only).
            credentials_file: GCP service-account JSON path (``gcs`` only).
            container: Azure Blob container name (``azure`` backend only).
            connection_string: Azure Blob connection string.
            account_url: Azure Blob account URL.
        """
        self.bucket = bucket
        self.prefix = prefix
        self._storage_backend: StorageBackendName = storage_backend
        self._local_dir: Path = local_dir or Path.home() / ".akosha" / "cold-store"
        self._region = region
        self._endpoint_url = endpoint_url
        self._project = project
        self._credentials_file = credentials_file
        self._container = container
        self._connection_string = connection_string
        self._account_url = account_url
        # Populated by ``initialize()``; remains None until then.
        self._storage_adapter: Any | None = None

    async def export_batch(
        self,
        records: list[ColdRecord],
        partition_path: str,
    ) -> str:
        """Export records to Parquet format.

        Steps:
            1. Convert records to PyArrow table with proper schema
            2. Write to temporary Parquet file
            3. Upload to the configured backend via its oneiric storage adapter
            4. Return object key

        Lazy-initializes the storage adapter on first use so callers don't
        have to remember to await ``initialize()`` in the common case.

        Args:
            records: ColdRecord objects to export
            partition_path: Partition path (e.g., "system-001/2025/01/31")

        Returns:
            Object key (e.g., "conversations/system-001/2025/01/31/batch.parquet")
        """
        if not records:
            logger.warning("Empty record batch provided for export")
            raise ValueError("Cannot export empty batch")

        # Lazy-init: callers that already awaited initialize() will hit the
        # fast-path; callers that didn't will get the adapter built here.
        if self._storage_adapter is None:
            await self.initialize()

        try:
            # Step 1: Convert records to PyArrow table
            table = self._records_to_arrow_table(records)

            # Step 2: Write to temporary Parquet file
            parquet_key = f"{self.prefix}{partition_path}/batch_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.parquet"
            temp_path = await self._write_parquet_file(table)

            # Step 3: Upload via configured backend
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
        data: dict[str, list[Any]] = {
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
        """Upload Parquet file to the configured backend via its oneiric adapter.

        oneiric storage adapter methods (``init``, ``save``, ``upload``,
        ``cleanup``, ``health``) are all ``async def``; we await them directly.

        Args:
            temp_path: Path to local Parquet file to upload.
            object_key: Backend-specific object key (S3 key, GCS blob name,
                Azure blob name, or local relative path).

        Raises:
            RuntimeError: If ``initialize()`` has not been called yet.
        """
        if self._storage_adapter is None:
            raise RuntimeError(
                "ColdStore._storage_adapter is not initialized. "
                "Call await store.initialize() before exporting."
            )

        try:
            data = temp_path.read_bytes()
            backend = self._storage_backend
            if backend == "local":
                # LocalStorageAdapter.save returns the saved key.
                await self._storage_adapter.save(object_key, data)
            elif backend == "s3":
                # S3StorageAdapter.upload returns None.
                await self._storage_adapter.upload(object_key, data)
            elif backend == "gcs":
                await self._storage_adapter.upload(object_key, data)
            elif backend == "azure":
                await self._storage_adapter.upload(object_key, data)
            else:
                raise RuntimeError(f"Unknown storage backend: {backend}")

            logger.info(
                "Successfully uploaded %s (%d bytes) to %s backend at key %s",
                temp_path,
                len(data),
                backend,
                object_key,
            )

            # Clean up temp file after successful upload.
            if temp_path.exists():
                temp_path.unlink()
                logger.debug("Cleaned up temporary file: %s", temp_path)

        except Exception as e:
            logger.exception("Failed to upload %s to %s", temp_path, object_key)
            # Clean up temp file on error too.
            if temp_path.exists():
                temp_path.unlink()
            raise

    async def initialize(self) -> None:
        """Construct the oneiric storage adapter for the configured backend.

        Raises ``ValueError`` for ``s3``/``gcs``/``azure`` when the required
        identifier (bucket or container) is missing.
        """
        if self._storage_backend == "local":
            settings = LocalStorageSettings(
                base_path=self._local_dir,
                create_parents=True,
            )
            adapter: Any = LocalStorageAdapter(settings=settings)
        elif self._storage_backend == "s3":
            if not self.bucket:
                raise ValueError("S3 backend requires a non-empty 'bucket' argument")
            settings = S3StorageSettings(
                bucket=self.bucket,
                region=self._region,
                endpoint_url=self._endpoint_url,
            )
            adapter = S3StorageAdapter(settings=settings)
        elif self._storage_backend == "gcs":
            if not self.bucket:
                raise ValueError("GCS backend requires a non-empty 'bucket' argument")
            settings = GCSStorageSettings(
                bucket=self.bucket,
                project=self._project,
                credentials_file=self._credentials_file,
            )
            adapter = GCSStorageAdapter(settings=settings)
        elif self._storage_backend == "azure":
            if not self._container:
                raise ValueError("Azure backend requires a non-empty 'container' argument")
            settings = AzureBlobStorageSettings(
                container=self._container,
                connection_string=self._connection_string,
                account_url=self._account_url,
            )
            adapter = AzureBlobStorageAdapter(settings=settings)
        else:
            raise ValueError(f"Unknown storage backend: {self._storage_backend}")

        # Adapter init() creates the local dir for LocalStorageAdapter and is
        # idempotent for the cloud adapters.
        await adapter.init()

        self._storage_adapter = adapter
        logger.info(
            "Cold store initialized with %s backend (bucket=%r container=%r)",
            self._storage_backend,
            self.bucket,
            self._container,
        )

    async def close(self) -> None:
        """Forward to the adapter's ``cleanup()`` and clear the cached adapter."""
        if self._storage_adapter is not None:
            adapter, self._storage_adapter = self._storage_adapter, None
            cleanup = getattr(adapter, "cleanup", None)
            if callable(cleanup):
                await cleanup()
            logger.info("Cold store closed (%s backend)", self._storage_backend)

    async def health(self) -> bool:
        """Probe the underlying adapter's ``health()`` method.

        Returns ``False`` until ``initialize()`` has been called and the
        adapter has not yet reported unhealthy.
        """
        if self._storage_adapter is None:
            return False
        return bool(await self._storage_adapter.health())
