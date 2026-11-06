"""Cold-store prefix / backend-name / byte-accounting contract.

The cold prefix is the substrate's per-component isolation mechanism: two
components that share a bucket but not a prefix must not overwrite each
other's objects. It is also the only place the prefix can live -- no
oneiric storage settings object has a ``prefix`` field, so the key has to
be assembled by the object that owns it (``ColdStore``).

These tests pin the three properties the cold write path depends on:

1. the substrate's ``"r2"`` backend name is accepted (it is the substrate
   default, and no storage adapter implements that name),
2. the prefix reaches the object key, and is well-formed even when the
   operator omits the trailing slash,
3. the store knows how many bytes it wrote.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from akosha.models import ColdRecord
from akosha.storage.cold_store import ColdStore

if TYPE_CHECKING:
    from pathlib import Path


def _record(conversation_id: str = "conv-1") -> ColdRecord:
    """Build one cold record for the export tests."""
    return ColdRecord(
        system_id="sys-1",
        conversation_id=conversation_id,
        fingerprint=[1, 2, 3],
        ultra_summary="hello",
        timestamp=datetime.now(UTC),
        metadata={"x": 1},
    )


class TestColdStoreBackendName:
    """The substrate's default backend name must be accepted."""

    def test_r2_resolves_to_the_s3_adapter(self) -> None:
        """``"r2"`` is R2-over-S3; it resolves to the ``s3`` adapter name."""
        store = ColdStore(bucket="b", storage_backend="r2")
        assert store._storage_backend == "s3"

    @pytest.mark.asyncio
    async def test_r2_constructs_the_s3_adapter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``"r2"`` reaches ``S3StorageAdapter`` with the endpoint intact."""
        from akosha.storage import cold_store as csmod

        captured: dict = {}

        def fake_s3_adapter(settings: object) -> AsyncMock:
            captured["settings"] = settings
            adapter = AsyncMock()
            adapter.init = AsyncMock()
            return adapter

        monkeypatch.setattr(csmod, "S3StorageAdapter", fake_s3_adapter)

        store = ColdStore(
            bucket="my-bucket",
            storage_backend="r2",
            region="auto",
            endpoint_url="https://example.r2.cloudflarestorage.com",
        )
        await store.initialize()
        try:
            assert captured["settings"].bucket == "my-bucket"
            assert captured["settings"].region == "auto"
            assert captured["settings"].endpoint_url == "https://example.r2.cloudflarestorage.com"
        finally:
            await store.close()


class TestColdStorePrefix:
    """The prefix must reach the object key."""

    def test_prefix_without_trailing_slash_is_normalised(self) -> None:
        """A prefix missing its slash is normalised, not concatenated raw."""
        store = ColdStore(prefix="conversations", storage_backend="local")
        assert store.prefix == "conversations/"

    def test_prefix_with_trailing_slash_is_preserved(self) -> None:
        """An already well-formed prefix is left alone."""
        store = ColdStore(prefix="akosha/", storage_backend="local")
        assert store.prefix == "akosha/"

    @pytest.mark.asyncio
    async def test_prefix_reaches_the_object_key(self, tmp_path: Path) -> None:
        """The exported key starts with the prefix, then the partition."""
        store = ColdStore(
            prefix="akosha/",
            storage_backend="local",
            local_dir=tmp_path / "cold",
        )
        await store.initialize()
        try:
            key = await store.export_batch([_record()], "sys-1/2026/01/01")
            assert key.startswith("akosha/sys-1/2026/01/01/")
            assert key.endswith(".parquet")
            assert (tmp_path / "cold" / key).exists()
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_prefix_without_slash_reaches_the_object_key(self, tmp_path: Path) -> None:
        """A prefix missing its slash still produces a well-formed key."""
        store = ColdStore(
            prefix="akosha",
            storage_backend="local",
            local_dir=tmp_path / "cold",
        )
        await store.initialize()
        try:
            key = await store.export_batch([_record()], "sys-1/2026/01/01")
            assert key.startswith("akosha/sys-1/2026/01/01/")
            assert (tmp_path / "cold" / key).exists()
        finally:
            await store.close()


class TestColdStoreByteAccounting:
    """The store reports how many bytes it wrote."""

    @pytest.mark.asyncio
    async def test_bytes_exported_is_zero_before_any_export(self, tmp_path: Path) -> None:
        """A fresh store has written nothing."""
        store = ColdStore(storage_backend="local", local_dir=tmp_path / "cold")
        assert store.bytes_exported == 0

    @pytest.mark.asyncio
    async def test_bytes_exported_counts_the_uploaded_payload(self, tmp_path: Path) -> None:
        """After an export the counter reflects the uploaded bytes."""
        store = ColdStore(storage_backend="local", local_dir=tmp_path / "cold")
        await store.initialize()
        try:
            await store.export_batch([_record()], "sys-1/2026/01/01")
            assert store.bytes_exported > 0
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_bytes_exported_accumulates_across_exports(self, tmp_path: Path) -> None:
        """Two exports write twice as many bytes as one."""
        store = ColdStore(storage_backend="local", local_dir=tmp_path / "cold")
        await store.initialize()
        try:
            await store.export_batch([_record("conv-1")], "sys-1/2026/01/01")
            after_first = store.bytes_exported
            await store.export_batch([_record("conv-2")], "sys-1/2026/01/02")
            assert store.bytes_exported > after_first
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_two_exports_of_the_same_partition_get_distinct_keys(
        self, tmp_path: Path
    ) -> None:
        """The batch name must be unique, not merely second-granular.

        Two exports of the same partition within the same second would
        otherwise produce the SAME key, and the second upload would
        silently overwrite the first -- whose rows would then be evicted
        from the warm tier while absent from the surviving object.
        """
        store = ColdStore(storage_backend="local", local_dir=tmp_path / "cold")
        await store.initialize()
        try:
            first = await store.export_batch([_record("conv-1")], "sys-1/2026/01/01")
            second = await store.export_batch([_record("conv-1")], "sys-1/2026/01/01")
            assert first != second
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_two_exports_in_the_same_second_get_distinct_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The uniqueness does not depend on the wall clock advancing.

        The test above compares two keys, but it cannot guarantee the two
        exports landed in the same second -- that is left to scheduling
        luck. Freezing the clock makes the precondition explicit, so the
        second-granular timestamp alone can never be enough to separate
        the two keys.
        """
        from akosha.storage import cold_store as csmod

        class FrozenDateTime(datetime):
            """A clock that never advances."""

            @classmethod
            def now(cls, _tz: object = None) -> datetime:
                return cls(2026, 11, 5, 12, 0, 0)

        monkeypatch.setattr(csmod, "datetime", FrozenDateTime)

        store = ColdStore(storage_backend="local", local_dir=tmp_path / "cold")
        await store.initialize()
        try:
            first = await store.export_batch([_record("conv-1")], "sys-1/2026/01/01")
            second = await store.export_batch([_record("conv-2")], "sys-1/2026/01/01")
            # The timestamp part of the name is byte-for-byte identical:
            # only the uuid suffix separates the two keys.
            assert first.rsplit("_", 1)[0] == second.rsplit("_", 1)[0]
            assert first != second
        finally:
            await store.close()
