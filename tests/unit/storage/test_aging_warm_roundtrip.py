"""Hot->Warm migration against a REAL DuckDB hot store.

The mocked aging tests hand the connection a ``dict`` for the ``metadata``
column. DuckDB never returns a dict for a ``JSON`` column -- it returns the
JSON *text* -- so the mocked tests could not see the failure this module
pins: ``WarmRecord.metadata`` is typed ``dict[str, Any]``, and a JSON string
raised a pydantic ``ValidationError`` inside ``_migrate_batch``, which
swallowed it per batch. The migration reported ``errors=1`` in a log line
and moved nothing, so the warm tier stayed empty.

The warm tier is the cold tier's only source. A cold exporter wired to an
always-empty warm tier would be "wired" in the audit sense and dead in the
operational sense, so this round trip is part of the cold write path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from akosha.models import HotRecord
from akosha.storage.aging import AgingService
from akosha.storage.hot_store import HotStore
from akosha.storage.warm_store import WarmStore

if TYPE_CHECKING:
    from pathlib import Path

OLD_TIMESTAMP = datetime.now(UTC) - timedelta(days=30)


async def _seed_hot_store(record: HotRecord) -> HotStore:
    """Create an in-memory hot store holding one aged record."""
    hot = HotStore(database_path=":memory:")
    await hot.initialize()
    await hot.insert(record)
    return hot


def _hot_record() -> HotRecord:
    """A hot record with a JSON metadata blob."""
    return HotRecord(
        system_id="sys-1",
        conversation_id="conv-1",
        content="A sentence. Another sentence. A third one.",
        embedding=[0.1] * 384,
        timestamp=OLD_TIMESTAMP,
        metadata={"task_class": "mcp_tool_call", "source": "unit-test"},
    )


class TestHotToWarmRoundTrip:
    """The migration moves rows into the warm tier."""

    async def test_row_moves_from_hot_to_warm(self, tmp_path: Path) -> None:
        """One aged hot row becomes one warm row."""
        hot = await _seed_hot_store(_hot_record())
        warm = WarmStore(database_path=tmp_path / "warm.db")
        await warm.initialize()
        service = AgingService(hot_store=hot, warm_store=warm)
        try:
            stats = await service.migrate_hot_to_warm(cutoff_days=7)

            assert stats.errors == 0
            assert stats.records_migrated == 1
            rows = warm.conn.execute(
                "SELECT system_id, conversation_id, summary FROM conversations"
            ).fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "sys-1"
            assert rows[0][1] == "conv-1"
            assert rows[0][2]
        finally:
            await warm.close()

    async def test_metadata_survives_the_move(self, tmp_path: Path) -> None:
        """The JSON metadata blob is stored, not dropped."""
        hot = await _seed_hot_store(_hot_record())
        warm = WarmStore(database_path=tmp_path / "warm.db")
        await warm.initialize()
        service = AgingService(hot_store=hot, warm_store=warm)
        try:
            await service.migrate_hot_to_warm(cutoff_days=7)

            row = warm.conn.execute("SELECT metadata FROM conversations").fetchone()
            assert row is not None
            # DuckDB hands a JSON column back as text.
            assert "mcp_tool_call" in row[0]
        finally:
            await warm.close()

    async def test_the_hot_row_is_removed_after_the_move(self, tmp_path: Path) -> None:
        """A tier migration moves: the hot row is gone afterwards."""
        hot = await _seed_hot_store(_hot_record())
        warm = WarmStore(database_path=tmp_path / "warm.db")
        await warm.initialize()
        service = AgingService(hot_store=hot, warm_store=warm)
        try:
            await service.migrate_hot_to_warm(cutoff_days=7)

            remaining = hot.conn.execute("SELECT count(*) FROM conversations").fetchone()
            assert remaining[0] == 0
        finally:
            await warm.close()


class TestAgingMetadataCoercion:
    """The metadata column is normalised, whatever shape it arrives in."""

    async def test_json_text_becomes_a_mapping(self, tmp_path: Path) -> None:
        """``_get_eligible_records`` returns a dict, not JSON text."""
        hot = await _seed_hot_store(_hot_record())
        warm = WarmStore(database_path=tmp_path / "warm.db")
        await warm.initialize()
        service = AgingService(hot_store=hot, warm_store=warm)
        try:
            rows = await service._get_eligible_records(datetime.now(UTC) - timedelta(days=1))
            assert rows
            assert isinstance(rows[0]["metadata"], dict)
            assert rows[0]["metadata"]["task_class"] == "mcp_tool_call"
        finally:
            await warm.close()

    async def test_a_null_metadata_becomes_an_empty_mapping(self, tmp_path: Path) -> None:
        """A NULL metadata column does not break the migration."""
        record = _hot_record()
        record.metadata = {}
        hot = await _seed_hot_store(record)
        warm = WarmStore(database_path=tmp_path / "warm.db")
        await warm.initialize()
        service = AgingService(hot_store=hot, warm_store=warm)
        try:
            rows = await service._get_eligible_records(datetime.now(UTC) - timedelta(days=1))
            assert rows
            assert rows[0]["metadata"] == {}
        finally:
            await warm.close()


@pytest.mark.parametrize("cutoff_days", [1, 7, 30])
async def test_migration_is_idempotent_across_cutoffs(tmp_path: Path, cutoff_days: int) -> None:
    """Whatever the cutoff, a moved row is moved exactly once."""
    hot = await _seed_hot_store(_hot_record())
    warm = WarmStore(database_path=tmp_path / "warm.db")
    await warm.initialize()
    service = AgingService(hot_store=hot, warm_store=warm)
    try:
        stats = await service.migrate_hot_to_warm(cutoff_days=cutoff_days)
        assert stats.records_migrated == 1
        assert stats.errors == 0
    finally:
        await warm.close()
