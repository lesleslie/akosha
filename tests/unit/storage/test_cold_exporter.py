"""Warm->Cold exporter: the cold tier's write path.

The cold tier is Parquet batches in object storage. ``ColdStore.export_batch``
is the only producer of such a batch and, until this exporter existed,
nothing called it -- the tier was reachable but not writable.

These tests pin the contract the exporter has to honour:

* the age threshold decides what leaves the warm tier,
* the partition convention is ``system_id/YYYY/MM/DD``,
* the object's key carries the configured cold prefix,
* the exported record carries the documented ``ColdRecord`` shape,
* a failed partition keeps its warm rows (export first, evict second).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest
from prometheus_client import REGISTRY

from akosha.models import WarmRecord
from akosha.storage.cold_exporter import (
    ColdExporter,
    build_partition_path,
    minhash_fingerprint,
)
from akosha.storage.cold_store import ColdStore
from akosha.storage.warm_store import WarmStore

NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)
OLD_TIMESTAMP = datetime(2026, 1, 1, 12, 0, 0)
RECENT_TIMESTAMP = datetime(2026, 11, 1, 12, 0, 0)


def _warm_record(
    conversation_id: str = "conv-1",
    *,
    system_id: str = "sys-1",
    timestamp: datetime = OLD_TIMESTAMP,
    summary: str = "First sentence. Second sentence.",
) -> WarmRecord:
    """Build a warm-tier row for the exporter to age out."""
    return WarmRecord(
        system_id=system_id,
        conversation_id=conversation_id,
        embedding=[1] * 384,
        summary=summary,
        timestamp=timestamp,
        metadata={"task_class": "mcp_tool_call"},
    )


async def _seed_warm_store(path: Path, records: list[WarmRecord]) -> WarmStore:
    """Create and populate a warm store."""
    store = WarmStore(database_path=path)
    await store.initialize()
    for record in records:
        await store.insert(record)
    return store


@pytest.fixture
async def warm_store(tmp_path: Path) -> WarmStore:
    """A warm store with a single aged record."""
    store = await _seed_warm_store(tmp_path / "warm.db", [_warm_record()])
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
async def cold_store(tmp_path: Path) -> ColdStore:
    """A cold store on the local backend (no cloud credentials needed)."""
    store = ColdStore(
        prefix="akosha/",
        storage_backend="local",
        local_dir=tmp_path / "cold",
    )
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


def _parquet_files(cold_root: Path) -> list[Path]:
    """Every Parquet file the cold store wrote."""
    return sorted(cold_root.rglob("*.parquet"))


class TestPartitionPath:
    """The partition convention is ``system_id/YYYY/MM/DD``."""

    def test_partition_path_shape(self) -> None:
        """The date comes from the record's own timestamp."""
        path = build_partition_path("sys-1", datetime(2026, 3, 7, 9, 30))
        assert path == "sys-1/2026/03/07"

    def test_partition_path_sanitises_the_system_id(self) -> None:
        """A system id cannot smuggle extra path segments."""
        path = build_partition_path("sys/../../etc", datetime(2026, 3, 7))
        assert path == "sys_.._.._etc/2026/03/07"
        first = path.split("/")[0]
        assert "/" not in first
        assert first not in (".", "..")

    def test_partition_path_handles_an_empty_system_id(self) -> None:
        """An empty system id still produces a single-component partition."""
        assert build_partition_path("", datetime(2026, 3, 7)) == "_/2026/03/07"


class TestMinhashFingerprint:
    """The cold fingerprint is an int32 MinHash signature."""

    def test_fingerprint_is_a_list_of_ints(self) -> None:
        """The Parquet schema is ``list_(int32)``."""
        fingerprint = minhash_fingerprint("hello world")
        assert isinstance(fingerprint, list)
        assert fingerprint
        assert all(isinstance(value, int) for value in fingerprint)

    def test_fingerprint_values_fit_in_int32(self) -> None:
        """No value may overflow the Arrow int32 column."""
        fingerprint = minhash_fingerprint("hello world")
        assert all(0 <= value < 2**31 for value in fingerprint)

    def test_fingerprint_is_deterministic(self) -> None:
        """The same text always yields the same signature."""
        assert minhash_fingerprint("hello world") == minhash_fingerprint("hello world")

    def test_fingerprint_differs_for_different_text(self) -> None:
        """Different text yields a different signature."""
        assert minhash_fingerprint("hello world") != minhash_fingerprint("goodbye world")

    def test_fingerprint_without_datasketch_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing datasketch degrades to a deterministic SHA-256 expansion."""
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "datasketch":
                raise ImportError("datasketch missing")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        fingerprint = minhash_fingerprint("hello world")
        assert len(fingerprint) == 128
        assert all(0 <= value < 2**31 for value in fingerprint)


class TestColdExporter:
    """The exporter moves aged warm rows into the cold tier."""

    async def test_empty_warm_tier_exports_nothing(
        self, tmp_path: Path, cold_store: ColdStore
    ) -> None:
        """An empty warm tier is a no-op, not an error."""
        warm = await _seed_warm_store(tmp_path / "warm.db", [])
        exporter = ColdExporter(warm_store=warm, cold_store=cold_store)
        try:
            stats = await exporter.export_expired(now=NOW)
            assert stats.records_exported == 0
            assert stats.batches_exported == 0
            assert stats.errors == 0
            assert _parquet_files(tmp_path / "cold") == []
        finally:
            await warm.close()

    async def test_record_older_than_the_threshold_is_exported(
        self, warm_store: WarmStore, cold_store: ColdStore, tmp_path: Path
    ) -> None:
        """A 100-day-old row leaves the warm tier."""
        exporter = ColdExporter(warm_store=warm_store, cold_store=cold_store)
        stats = await exporter.export_expired(now=NOW)

        assert stats.records_exported == 1
        assert stats.batches_exported == 1
        assert stats.errors == 0
        files = _parquet_files(tmp_path / "cold")
        assert len(files) == 1
        assert files[0].relative_to(tmp_path / "cold").parts[0] == "akosha"

    async def test_record_newer_than_the_threshold_is_kept(
        self, tmp_path: Path, cold_store: ColdStore
    ) -> None:
        """A 4-day-old row stays in the warm tier."""
        warm = await _seed_warm_store(
            tmp_path / "warm.db", [_warm_record(timestamp=RECENT_TIMESTAMP)]
        )
        exporter = ColdExporter(warm_store=warm, cold_store=cold_store)
        try:
            stats = await exporter.export_expired(now=NOW)
            assert stats.records_exported == 0
            assert _parquet_files(tmp_path / "cold") == []
            remaining = warm.conn.execute("SELECT count(*) FROM conversations").fetchone()
            assert remaining[0] == 1
        finally:
            await warm.close()

    async def test_the_threshold_is_configurable(
        self, tmp_path: Path, cold_store: ColdStore
    ) -> None:
        """A 4-day-old row exports when the threshold is 1 day."""
        warm = await _seed_warm_store(
            tmp_path / "warm.db", [_warm_record(timestamp=RECENT_TIMESTAMP)]
        )
        exporter = ColdExporter(
            warm_store=warm,
            cold_store=cold_store,
            age_threshold_days=1,
        )
        try:
            stats = await exporter.export_expired(now=NOW)
            assert stats.records_exported == 1
        finally:
            await warm.close()

    async def test_object_key_carries_the_prefix_and_the_partition(
        self, warm_store: WarmStore, cold_store: ColdStore, tmp_path: Path
    ) -> None:
        """The key is ``<prefix><system_id>/YYYY/MM/DD/batch_*.parquet``."""
        exporter = ColdExporter(warm_store=warm_store, cold_store=cold_store)
        await exporter.export_expired(now=NOW)

        files = _parquet_files(tmp_path / "cold")
        assert len(files) == 1
        relative = files[0].relative_to(tmp_path / "cold")
        parts = relative.parts
        assert parts[:5] == ("akosha", "sys-1", "2026", "01", "01")
        assert parts[5].startswith("batch_")
        assert parts[5].endswith(".parquet")

    async def test_exported_record_shape(
        self, warm_store: WarmStore, cold_store: ColdStore, tmp_path: Path
    ) -> None:
        """The Parquet columns carry the documented ``ColdRecord`` fields."""
        exporter = ColdExporter(warm_store=warm_store, cold_store=cold_store)
        await exporter.export_expired(now=NOW)

        files = _parquet_files(tmp_path / "cold")
        table = pq.read_table(files[0])
        assert table.column_names == [
            "system_id",
            "conversation_id",
            "fingerprint",
            "ultra_summary",
            "timestamp",
            "metadata",
        ]
        row = table.to_pylist()[0]
        assert row["system_id"] == "sys-1"
        assert row["conversation_id"] == "conv-1"
        assert row["ultra_summary"] == "First sentence"
        assert isinstance(row["fingerprint"], list)
        assert row["metadata"] == '{"task_class": "mcp_tool_call"}'

    async def test_rows_are_evicted_after_a_successful_export(
        self, warm_store: WarmStore, cold_store: ColdStore
    ) -> None:
        """A tier migration moves: the warm row is gone afterwards."""
        exporter = ColdExporter(warm_store=warm_store, cold_store=cold_store)
        stats = await exporter.export_expired(now=NOW)

        assert stats.records_evicted == 1
        remaining = warm_store.conn.execute("SELECT count(*) FROM conversations").fetchone()
        assert remaining[0] == 0

    async def test_rows_are_kept_when_eviction_is_disabled(
        self, warm_store: WarmStore, cold_store: ColdStore
    ) -> None:
        """``evict_after_export=False`` keeps the warm rows."""
        exporter = ColdExporter(
            warm_store=warm_store,
            cold_store=cold_store,
            evict_after_export=False,
        )
        stats = await exporter.export_expired(now=NOW)

        assert stats.records_exported == 1
        assert stats.records_evicted == 0
        remaining = warm_store.conn.execute("SELECT count(*) FROM conversations").fetchone()
        assert remaining[0] == 1

    async def test_a_failed_partition_keeps_its_rows(
        self,
        tmp_path: Path,
        cold_store: ColdStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed upload must never destroy the source row."""
        warm = await _seed_warm_store(
            tmp_path / "warm.db",
            [
                _warm_record("conv-old", system_id="sys-a", timestamp=datetime(2026, 1, 1)),
                _warm_record("conv-new", system_id="sys-b", timestamp=datetime(2026, 1, 2)),
            ],
        )
        original = cold_store.export_batch

        async def flaky(records: list[Any], partition_path: str) -> str:
            if partition_path.startswith("sys-a/"):
                raise RuntimeError("upload failed")
            return await original(records, partition_path)

        monkeypatch.setattr(cold_store, "export_batch", flaky)

        exporter = ColdExporter(warm_store=warm, cold_store=cold_store)
        try:
            stats = await exporter.export_expired(now=NOW)
            assert stats.errors == 1
            assert stats.records_exported == 1
            assert stats.batches_exported == 1
            # The failed partition's row is still in the warm tier.
            remaining = warm.conn.execute(
                "SELECT conversation_id FROM conversations ORDER BY conversation_id"
            ).fetchall()
            assert [row[0] for row in remaining] == ["conv-old"]
        finally:
            await warm.close()

    async def test_metrics_are_emitted(self, warm_store: WarmStore, cold_store: ColdStore) -> None:
        """The cold-size gauge and the migration counter both move."""
        counter_before = (
            REGISTRY.get_sample_value(
                "akosha_migration_records_total",
                {"source_tier": "warm", "target_tier": "cold", "status": "success"},
            )
            or 0.0
        )

        exporter = ColdExporter(warm_store=warm_store, cold_store=cold_store)
        await exporter.export_expired(now=NOW)

        size_after = REGISTRY.get_sample_value("akosha_cold_store_size_bytes") or 0.0
        counter_after = (
            REGISTRY.get_sample_value(
                "akosha_migration_records_total",
                {"source_tier": "warm", "target_tier": "cold", "status": "success"},
            )
            or 0.0
        )
        # The gauge carries the cold store's cumulative exported bytes.
        assert size_after == float(cold_store.bytes_exported)
        assert size_after > 0
        assert counter_after == counter_before + 1

    async def test_an_uninitialised_warm_store_raises(self, cold_store: ColdStore) -> None:
        """A mis-wired exporter fails loudly instead of exporting nothing."""
        warm = WarmStore(database_path=Path("/nonexistent/never/warm.db"))
        exporter = ColdExporter(warm_store=warm, cold_store=cold_store)
        with pytest.raises(RuntimeError, match="store not initialized"):
            await exporter.export_expired(now=NOW)


class TestColdExporterBatchSize:
    """A cycle is bounded so a huge warm tier cannot exhaust memory."""

    async def test_batch_size_bounds_the_rows_read(
        self, tmp_path: Path, cold_store: ColdStore
    ) -> None:
        """Only ``batch_size`` rows are aged out per cycle."""
        warm = await _seed_warm_store(
            tmp_path / "warm.db",
            [_warm_record(f"conv-{index}") for index in range(5)],
        )
        exporter = ColdExporter(
            warm_store=warm,
            cold_store=cold_store,
            batch_size=2,
        )
        try:
            stats = await exporter.export_expired(now=NOW)
            assert stats.records_exported == 2
            remaining = warm.conn.execute("SELECT count(*) FROM conversations").fetchone()
            assert remaining[0] == 3
        finally:
            await warm.close()


def test_threshold_default_matches_the_documented_90_days() -> None:
    """The default age threshold is the documented 90 days."""
    from akosha.storage.cold_exporter import DEFAULT_AGE_THRESHOLD_DAYS

    assert DEFAULT_AGE_THRESHOLD_DAYS == 90
    assert timedelta(days=DEFAULT_AGE_THRESHOLD_DAYS).days == 90
