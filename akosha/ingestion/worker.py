"""Ingestion worker: Pull system memories from cloud storage."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from akosha.models import SystemMemoryUpload

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator  # noqa: F401

    from oneiric.adapters.storage.s3 import S3StorageAdapter  # type: ignore[import]

    from akosha.storage.hot_store import HotStore

logger = logging.getLogger(__name__)


class IngestionWorker:
    """Pull-based ingestion worker for system memories.

    Polls cloud storage (Cloudflare R2/S3) for new Session-Buddy uploads
    and ingests them into Akosha's storage tiers.
    """

    def __init__(
        self,
        storage_adapter: S3StorageAdapter,  # type: ignore[import]
        hot_store: HotStore,
        poll_interval_seconds: int = 30,
        max_concurrent_ingests: int = 100,
    ) -> None:
        """Initialize worker.

        Args:
            storage_adapter: Oneiric S3/R2 storage adapter
            hot_store: Hot store for data insertion
            poll_interval_seconds: Polling interval
            max_concurrent_ingests: Maximum concurrent ingestion tasks
        """
        self.storage = storage_adapter
        self.hot_store = hot_store
        self.poll_interval_seconds = poll_interval_seconds
        self.max_concurrent_ingests = max_concurrent_ingests
        self._running = False

    async def run(self) -> None:
        """Main worker loop with concurrent processing."""
        self._running = True
        logger.info("Ingestion worker started")

        # Semaphore for concurrency control
        semaphore = asyncio.Semaphore(self.max_concurrent_ingests)

        while self._running:
            try:
                # 1. Discover new uploads
                uploads = await self._discover_uploads()

                if uploads:
                    logger.info(f"Discovered {len(uploads)} new uploads")

                    # 2. Process uploads concurrently with semaphore protection
                    async def process_with_semaphore(upload: SystemMemoryUpload) -> None:
                        """Process upload with semaphore limiting."""
                        async with semaphore:
                            return await self._process_upload(upload)

                    # Create all tasks
                    tasks = [process_with_semaphore(upload) for upload in uploads]

                    # Execute concurrently and capture results
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    # Log any errors
                    for i, result in enumerate(results):
                        if isinstance(result, Exception):
                            upload = uploads[i]
                            logger.error(
                                f"Upload processing failed for {upload.system_id}/{upload.upload_id}: {result}",
                                exc_info=result if logger.isEnabledFor(logging.DEBUG) else None,
                            )

                # 3. Wait before next poll
                await asyncio.sleep(self.poll_interval_seconds)

            except Exception as e:
                logger.error(f"Ingestion worker error: {e}", exc_info=True)
                await asyncio.sleep(60)  # Backoff on error

    async def _discover_uploads(self) -> list[SystemMemoryUpload]:
        """Discover new uploads from cloud storage.

        Returns:
            List of discovered uploads with system_id, upload_id, and manifest data
        """
        uploads = []

        try:
            # List all system prefixes (pattern: systems/<system-id>/)
            logger.debug("Discovering uploads from cloud storage")

            # Collect system prefixes from async generator
            system_prefixes: list[str] = []
            storage_gen: AsyncGenerator[str, None] = self.storage.list("systems/")  # type: ignore[call-arg, assignment]
            async for prefix in storage_gen:  # type: ignore[union-attr]
                system_id_prefix: str = str(prefix)  # type: ignore[union-attr]
                system_prefixes.append(system_id_prefix)

            for system_prefix in system_prefixes:
                # Extract system_id from prefix (systems/<system-id>/)
                system_id = system_prefix.strip("/").split("/")[-1]

                # Skip if not a valid system prefix
                if not system_id or system_prefix.count("/") < 1:
                    continue

                logger.debug(f"Scanning system: {system_id}")

                # List upload prefixes within this system
                upload_prefix = f"systems/{system_id}/"
                obj_prefixes: list[str] = []
                storage_gen2: AsyncGenerator[str, None] = self.storage.list(upload_prefix)  # type: ignore[call-arg, assignment]
                async for obj in storage_gen2:  # type: ignore[union-attr]
                    obj_str: str = str(obj)  # type: ignore[union-attr]
                    obj_prefixes.append(obj_str)

                for obj in obj_prefixes:
                    # Skip if not a directory prefix
                    if not obj.endswith("/"):
                        continue

                    # Extract upload_id from path (systems/<system-id>/<upload-id>/)
                    upload_id = obj.strip("/").split("/")[-1]

                    # Skip if not a valid upload prefix
                    if not upload_id:
                        continue

                    # Check for manifest.json
                    manifest_path = f"{obj}manifest.json"

                    if await self.storage.exists(manifest_path):  # type: ignore[attr-defined, call-arg]
                        try:
                            # Download and parse manifest
                            manifest_data_bytes = await self.storage.download(manifest_path)  # type: ignore[attr-defined, call-arg]
                            if manifest_data_bytes is None:
                                logger.warning(f"Empty manifest at {manifest_path}")
                                continue
                            manifest = json.loads(manifest_data_bytes)

                            # Parse upload timestamp
                            uploaded_at = datetime.fromisoformat(
                                manifest.get("uploaded_at", datetime.now(UTC).isoformat())
                            )

                            # Create SystemMemoryUpload object
                            upload = SystemMemoryUpload(
                                system_id=system_id,
                                upload_id=upload_id,
                                manifest=manifest,
                                storage_prefix=obj,
                                uploaded_at=uploaded_at,
                            )

                            uploads.append(upload)
                            logger.debug(
                                f"Discovered upload: {system_id}/{upload_id} "
                                f"({manifest.get('conversation_count', '?')} conversations)"
                            )

                        except (json.JSONDecodeError, KeyError, ValueError) as e:
                            logger.warning(f"Failed to parse manifest for {manifest_path}: {e}")
                            continue
                    else:
                        logger.debug(f"No manifest found at {manifest_path}")

            logger.info(f"Discovery complete: found {len(uploads)} uploads")

        except Exception as e:
            logger.error(f"Upload discovery failed: {e}", exc_info=True)

        return uploads

    async def _process_upload(self, upload: SystemMemoryUpload) -> None:
        """Process a single upload.

        Args:
            upload: SystemMemoryUpload object with metadata
        """
        system_id = upload.system_id
        upload_id = upload.upload_id

        logger.info(f"Processing upload: {system_id}/{upload_id}")

        # TODO: Implement extraction, deduplication, insertion
        # 1. Download memory database from storage_prefix
        # 2. Extract conversations and embeddings
        # 3. Deduplicate against hot store
        # 4. Insert new conversations into hot store
        #
        # For now, log the manifest metadata
        logger.debug(f"Upload manifest: {upload.manifest}")

    def stop(self) -> None:
        """Stop the worker."""
        self._running = False
        logger.info("Ingestion worker stopped")
