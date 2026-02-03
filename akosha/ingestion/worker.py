"""Ingestion worker: Pull system memories from cloud storage."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING

from akosha.models import SystemMemoryUpload
from akosha.models.schemas import (
    SystemMemoryUploadManifest,
    validate_storage_prefix,
    validate_system_id,
    validate_upload_id,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from oneiric.adapters.storage.s3 import S3StorageAdapter  # type: ignore[import]

    from akosha.storage.hot_store import HotStore

logger = logging.getLogger(__name__)


class IngestionWorker:
    """Pull-based ingestion worker for system memories.

    Polls cloud storage (Cloudflare R2/S3) for new Session-Buddy uploads
    and ingests them into Akosha's storage tiers.
    """

    # Maximum limits to prevent memory exhaustion
    MAX_SYSTEM_PREFIXES = 10_000
    MAX_UPLOAD_PREFIXES = 100_000
    MAX_CONCURRENT_SCANS = 1_000

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
        """Discover new uploads from cloud storage using concurrent processing.

        Performance optimizations:
        - Concurrent system scanning with asyncio.gather()
        - Concurrent manifest downloads
        - Semaphore protection for rate limiting

        Returns:
            List of discovered uploads with system_id, upload_id, and manifest data
        """
        uploads = []

        try:
            # List all system prefixes (pattern: systems/<system-id>/)
            logger.debug("Discovering uploads from cloud storage")

            # Feature flag for concurrent discovery (default: true)
            use_concurrent_discovery = (
                os.getenv("USE_CONCURRENT_DISCOVERY", "true").lower() == "true"
            )

            if use_concurrent_discovery:
                # Concurrent discovery: 20-30x faster
                return await self._discover_uploads_concurrent()
            else:
                # Sequential discovery (legacy)
                return await self._discover_uploads_sequential()

        except Exception as e:
            logger.error(f"Upload discovery failed: {e}", exc_info=True)
            return uploads

    async def _discover_uploads_concurrent(self) -> list[SystemMemoryUpload]:
        """Concurrent upload discovery with parallel processing.

        Returns:
            List of discovered uploads
        """
        try:
            logger.debug("Scanning systems concurrently")

            # Step 1: Collect all system prefixes with limit protection
            system_prefixes = await self._collect_system_prefixes()

            # Step 2: Scan all systems concurrently
            logger.debug(f"Scanning {len(system_prefixes)} systems in parallel")
            system_results = await self._scan_systems_concurrent(system_prefixes)

            # Step 3: Flatten results and filter errors
            uploads = self._flatten_scan_results(system_results)

            logger.info(f"Concurrent discovery complete: found {len(uploads)} uploads")
            return uploads

        except Exception as e:
            logger.error(f"Concurrent upload discovery failed: {e}", exc_info=True)
            return []

    async def _scan_system(self, system_id: str, _system_prefix: str) -> list[SystemMemoryUpload]:
        """Scan a single system for uploads.

        Args:
            system_id: System identifier
            _system_prefix: Storage prefix for this system (currently unused, reserved for future)

        Returns:
            List of uploads from this system
        """
        try:
            logger.debug(f"Scanning system: {system_id}")
            upload_prefix = f"systems/{system_id}/"
            obj_prefixes = await self._collect_upload_prefixes(upload_prefix, system_id)
            return await self._process_upload_prefixes(system_id, obj_prefixes)

        except Exception as e:
            logger.error(f"Failed to scan system {system_id}: {e}")
            return []

    async def _discover_uploads_sequential(self) -> list[SystemMemoryUpload]:
        """Sequential upload discovery (legacy implementation).

        Returns:
            List of discovered uploads
        """
        uploads = []

        try:
            logger.debug("Discovering uploads from cloud storage (sequential)")

            # Collect system prefixes from async generator
            storage_gen: AsyncGenerator[str] = self.storage.list("systems/")  # type: ignore[call-arg, assignment]
            system_prefixes: list[str] = []
            async for prefix in storage_gen:  # type: ignore[union-attr]
                system_prefixes.append(prefix)  # type: ignore[union-attr]  # noqa: FURB138  # List comprehension not possible with async gen

            # Process each system sequentially
            for system_prefix in system_prefixes:
                system_id = self._extract_system_id(system_prefix)
                if not system_id:
                    continue

                system_uploads = await self._scan_system(system_id, system_prefix)
                uploads.extend(system_uploads)

            logger.info(f"Sequential discovery complete: found {len(uploads)} uploads")
            return uploads

        except Exception as e:
            logger.error(f"Sequential upload discovery failed: {e}", exc_info=True)
            return []

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

    # Helper methods for reducing complexity

    async def _collect_system_prefixes(self) -> list[str]:
        """Collect system prefixes from storage with limit protection.

        Returns:
            List of system prefixes (max MAX_SYSTEM_PREFIXES)
        """
        system_prefixes: list[str] = []
        storage_gen: AsyncGenerator[str] = self.storage.list("systems/")  # type: ignore[call-arg, assignment]
        async for prefix in storage_gen:  # type: ignore[union-attr]
            system_prefixes.append(prefix)  # type: ignore[union-attr]
            if len(system_prefixes) >= self.MAX_SYSTEM_PREFIXES:
                logger.warning(
                    f"System prefix limit reached ({self.MAX_SYSTEM_PREFIXES}), "
                    "stopping discovery to prevent memory exhaustion"
                )
                break
        return system_prefixes

    async def _scan_systems_concurrent(
        self, system_prefixes: list[str]
    ) -> list[SystemMemoryUpload | Exception]:
        """Scan multiple systems concurrently.

        Args:
            system_prefixes: List of system prefixes to scan

        Returns:
            List of scan results (uploads or exceptions)
        """
        scan_tasks = []
        for system_prefix in system_prefixes[: self.MAX_CONCURRENT_SCANS]:
            system_id = self._extract_system_id(system_prefix)
            if system_id:
                scan_tasks.append(self._scan_system(system_id, system_prefix))

        if not scan_tasks:
            return []

        return await asyncio.gather(*scan_tasks, return_exceptions=True)

    def _flatten_scan_results(
        self, system_results: list[SystemMemoryUpload | Exception]
    ) -> list[SystemMemoryUpload]:
        """Flatten scan results and filter errors.

        Args:
            system_results: List of scan results from concurrent execution

        Returns:
            Flattened list of uploads without errors
        """
        uploads: list[SystemMemoryUpload] = []
        for result in system_results:
            if isinstance(result, Exception):
                logger.error(f"System scan failed: {result}")
            elif result:
                uploads.extend(result)
        return uploads

    def _extract_system_id(self, system_prefix: str) -> str | None:
        """Extract system ID from storage prefix.

        Args:
            system_prefix: Storage prefix like "systems/<system-id>/"

        Returns:
            System ID or None if invalid
        """
        if system_prefix.count("/") < 1:
            return None
        system_id = system_prefix.strip("/").split("/")[-1]
        return system_id or None

    async def _collect_upload_prefixes(
        self, upload_prefix: str, system_id: str
    ) -> list[str]:
        """Collect upload prefixes for a system with limit protection.

        Args:
            upload_prefix: Prefix to list (e.g., "systems/<system-id>/")
            system_id: System ID for logging

        Returns:
            List of upload prefixes
        """
        obj_prefixes: list[str] = []
        storage_gen: AsyncGenerator[str] = self.storage.list(upload_prefix)  # type: ignore[call-arg, assignment]
        async for obj in storage_gen:  # type: ignore[union-attr]
            obj_prefixes.append(obj)  # type: ignore[union-attr]
            if len(obj_prefixes) >= self.MAX_UPLOAD_PREFIXES:
                logger.warning(
                    f"Upload prefix limit reached ({self.MAX_UPLOAD_PREFIXES}) "
                    f"for system {system_id}, stopping scan"
                )
                break
        return obj_prefixes

    async def _process_upload_prefixes(
        self, system_id: str, obj_prefixes: list[str]
    ) -> list[SystemMemoryUpload]:
        """Process upload prefixes and extract manifests.

        Args:
            system_id: System identifier
            obj_prefixes: List of upload prefixes to process

        Returns:
            List of discovered uploads
        """
        uploads: list[SystemMemoryUpload] = []

        for obj in obj_prefixes:
            if not obj.endswith("/"):
                continue

            upload_id = obj.strip("/").split("/")[-1]
            if not upload_id:
                continue

            upload = await self._try_create_upload(system_id, upload_id, obj)
            if upload:
                uploads.append(upload)

        return uploads

    async def _try_create_upload(
        self, system_id: str, upload_id: str, obj: str
    ) -> SystemMemoryUpload | None:
        """Try to create a SystemMemoryUpload from manifest.

        Args:
            system_id: System identifier
            upload_id: Upload identifier
            obj: Storage object prefix

        Returns:
            SystemMemoryUpload or None if validation fails
        """
        manifest_path = f"{obj}manifest.json"

        if not await self.storage.exists(manifest_path):  # type: ignore[attr-defined, call-arg]
            logger.debug(f"No manifest found at {manifest_path}")
            return None

        try:
            manifest_data_bytes = await self.storage.download(manifest_path)  # type: ignore[attr-defined, call-arg]
            if manifest_data_bytes is None:
                logger.warning(f"Empty manifest at {manifest_path}")
                return None

            manifest_dict = json.loads(manifest_data_bytes)
            manifest = SystemMemoryUploadManifest(**manifest_dict)

            validated_system_id = validate_system_id(system_id)
            validated_upload_id = validate_upload_id(upload_id)
            validated_storage_prefix = validate_storage_prefix(obj)

            upload = SystemMemoryUpload(
                system_id=validated_system_id,
                upload_id=validated_upload_id,
                manifest=manifest.model_dump(),
                storage_prefix=validated_storage_prefix,
                uploaded_at=manifest.uploaded_at,
            )

            logger.debug(
                f"Discovered upload: {validated_system_id}/{validated_upload_id} "
                f"({manifest.conversation_count} conversations)"
            )
            return upload

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"Failed to parse manifest for {manifest_path}: {e}")
            return None
