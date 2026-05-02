"""Comprehensive tests for akosha.storage.aging (101 uncovered lines).

Targets:
- MigrationStats dataclass
- AgingService: migrate_hot_to_warm, _migrate_batch, _get_eligible_records,
  _quantize_embedding, _generate_summary, _compute_checksum,
  _verify_checksum_compatibility, _delete_from_hot_store,
  _delete_batch_from_hot_store, get_migration_stats
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from akosha.storage.aging import AgingService, MigrationStats

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hot_store(conn=None) -> MagicMock:
    """Build a mock hot_store with a configurable conn."""
    store = MagicMock()
    store.conn = conn
    return store


def _make_warm_store(conn=None) -> MagicMock:
    """Build a mock warm_store with async helpers."""
    store = MagicMock()
    store.conn = conn
    store.insert = AsyncMock()
    store.insert_batch = AsyncMock()
    return store


def _make_eligible_row(
    idx: int = 0,
    content: str = "First sentence. Second sentence. Third sentence. Fourth sentence.",
    embedding: list[float] | None = None,
    timestamp: datetime | None = None,
    content_hash: str = "",
) -> tuple:
    """Return a single row tuple as produced by the SQL query."""
    if embedding is None:
        embedding = [0.1, 0.2, -0.3]
    if timestamp is None:
        timestamp = datetime(2024, 1, 1, tzinfo=UTC)
    return (
        f"sys-{idx}",
        f"conv-{idx}",
        content,
        embedding,
        timestamp,
        {"key": f"val-{idx}"},
        content_hash,
    )


def _setup_conn_with_rows(rows: list[tuple]) -> MagicMock:
    """Create a mock conn whose execute().fetchall() returns *rows*."""
    result = MagicMock()
    result.fetchall.return_value = rows
    conn = MagicMock()
    conn.execute.return_value = result
    return conn


def _count_result(value: int) -> MagicMock:
    """Create a mock execute result whose fetchone returns (value,)."""
    result = MagicMock()
    result.fetchone.return_value = (value,)
    return result


# ===========================================================================
# MigrationStats
# ===========================================================================


class TestMigrationStats:
    """Tests for the MigrationStats dataclass."""

    def test_default_values(self) -> None:
        stats = MigrationStats()
        assert stats.records_migrated == 0
        assert stats.bytes_freed == 0
        assert stats.errors == 0
        assert stats.start_time is None
        assert stats.end_time is None

    def test_custom_values(self) -> None:
        now = datetime.now(UTC)
        later = now + timedelta(seconds=5)
        stats = MigrationStats(
            records_migrated=10,
            bytes_freed=25000,
            errors=2,
            start_time=now,
            end_time=later,
        )
        assert stats.records_migrated == 10
        assert stats.bytes_freed == 25000
        assert stats.errors == 2
        assert stats.start_time is now
        assert stats.end_time is later

    def test_partial_values(self) -> None:
        stats = MigrationStats(records_migrated=3, errors=1)
        assert stats.records_migrated == 3
        assert stats.errors == 1
        assert stats.bytes_freed == 0
        assert stats.start_time is None


# ===========================================================================
# AgingService.__init__
# ===========================================================================


class TestAgingServiceInit:
    """Tests for AgingService construction."""

    def test_stores_hot_store(self) -> None:
        hot = _make_hot_store()
        warm = _make_warm_store()
        svc = AgingService(hot, warm)
        assert svc.hot_store is hot

    def test_stores_warm_store(self) -> None:
        hot = _make_hot_store()
        warm = _make_warm_store()
        svc = AgingService(hot, warm)
        assert svc.warm_store is warm


# ===========================================================================
# _get_eligible_records
# ===========================================================================


class TestGetEligibleRecords:
    """Tests for AgingService._get_eligible_records."""

    @pytest.mark.asyncio
    async def test_returns_dicts_from_rows(self) -> None:
        row = _make_eligible_row()
        conn = _setup_conn_with_rows([row])
        svc = AgingService(_make_hot_store(conn), _make_warm_store())

        records = await svc._get_eligible_records(datetime(2025, 1, 1))

        assert len(records) == 1
        assert records[0]["system_id"] == "sys-0"
        assert records[0]["conversation_id"] == "conv-0"
        assert records[0]["content"] == row[2]
        assert records[0]["embedding"] == row[3]
        assert records[0]["timestamp"] == row[4]
        assert records[0]["metadata"] == row[5]
        assert records[0]["content_hash"] == row[6]

    @pytest.mark.asyncio
    async def test_passes_cutoff_date_to_query(self) -> None:
        conn = _setup_conn_with_rows([])
        svc = AgingService(_make_hot_store(conn), _make_warm_store())
        cutoff = datetime(2024, 6, 15, tzinfo=UTC)

        await svc._get_eligible_records(cutoff)

        conn.execute.assert_called_once()
        call_args = conn.execute.call_args
        assert call_args[0][1] == [cutoff]

    @pytest.mark.asyncio
    async def test_empty_result(self) -> None:
        conn = _setup_conn_with_rows([])
        svc = AgingService(_make_hot_store(conn), _make_warm_store())

        records = await svc._get_eligible_records(datetime(2025, 1, 1))

        assert records == []

    @pytest.mark.asyncio
    async def test_multiple_rows(self) -> None:
        rows = [_make_eligible_row(i) for i in range(4)]
        conn = _setup_conn_with_rows(rows)
        svc = AgingService(_make_hot_store(conn), _make_warm_store())

        records = await svc._get_eligible_records(datetime(2025, 1, 1))

        assert len(records) == 4
        assert records[0]["conversation_id"] == "conv-0"
        assert records[3]["conversation_id"] == "conv-3"

    @pytest.mark.asyncio
    async def test_raises_when_conn_is_none(self) -> None:
        svc = AgingService(_make_hot_store(conn=None), _make_warm_store())

        with pytest.raises(RuntimeError, match="Hot store not initialized"):
            await svc._get_eligible_records(datetime(2025, 1, 1))


# ===========================================================================
# _quantize_embedding
# ===========================================================================


class TestQuantizeEmbedding:
    """Tests for AgingService._quantize_embedding."""

    @pytest.mark.asyncio
    async def test_basic_quantization(self) -> None:
        svc = AgingService(_make_hot_store(), _make_warm_store())
        result = await svc._quantize_embedding([0.1, -0.2, 0.5])
        assert result == [12, -25, 63]  # int(v * 127) for each

    @pytest.mark.asyncio
    async def test_zero_embedding(self) -> None:
        svc = AgingService(_make_hot_store(), _make_warm_store())
        result = await svc._quantize_embedding([0.0, 0.0, 0.0])
        assert result == [0, 0, 0]

    @pytest.mark.asyncio
    async def test_empty_embedding(self) -> None:
        svc = AgingService(_make_hot_store(), _make_warm_store())
        result = await svc._quantize_embedding([])
        assert result == []

    @pytest.mark.asyncio
    async def test_large_values(self) -> None:
        svc = AgingService(_make_hot_store(), _make_warm_store())
        result = await svc._quantize_embedding([1.0, -1.0])
        assert result == [127, -127]

    @pytest.mark.asyncio
    async def test_returns_ints(self) -> None:
        svc = AgingService(_make_hot_store(), _make_warm_store())
        result = await svc._quantize_embedding([0.01, 0.5])
        assert all(isinstance(v, int) for v in result)


# ===========================================================================
# _generate_summary
# ===========================================================================


class TestGenerateSummary:
    """Tests for AgingService._generate_summary."""

    @pytest.mark.asyncio
    async def test_exactly_three_sentences(self) -> None:
        svc = AgingService(_make_hot_store(), _make_warm_store())
        content = "One. Two. Three."
        result = await svc._generate_summary(content)
        assert result == "One. Two. Three"

    @pytest.mark.asyncio
    async def test_more_than_three_sentences_truncates(self) -> None:
        svc = AgingService(_make_hot_store(), _make_warm_store())
        content = "First sentence. Second sentence. Third sentence. Fourth sentence."
        result = await svc._generate_summary(content)
        assert result == "First sentence. Second sentence. Third sentence"

    @pytest.mark.asyncio
    async def test_single_sentence(self) -> None:
        svc = AgingService(_make_hot_store(), _make_warm_store())
        result = await svc._generate_summary("Only one.")
        assert result == "Only one"

    @pytest.mark.asyncio
    async def test_two_sentences(self) -> None:
        svc = AgingService(_make_hot_store(), _make_warm_store())
        result = await svc._generate_summary("Alpha. Beta.")
        assert result == "Alpha. Beta"

    @pytest.mark.asyncio
    async def test_empty_string(self) -> None:
        svc = AgingService(_make_hot_store(), _make_warm_store())
        result = await svc._generate_summary("")
        assert result == ""

    @pytest.mark.asyncio
    async def test_strips_whitespace(self) -> None:
        svc = AgingService(_make_hot_store(), _make_warm_store())
        content = "  A  .   B  .  C  ."
        result = await svc._generate_summary(content)
        assert result == "A. B. C"


# ===========================================================================
# _compute_checksum
# ===========================================================================


class TestComputeChecksum:
    """Tests for AgingService._compute_checksum."""

    def test_returns_sha256_hex(self) -> None:
        svc = AgingService(_make_hot_store(), _make_warm_store())
        result = svc._compute_checksum("hello")
        expected = hashlib.sha256(b"hello").hexdigest()
        assert result == expected

    def test_length_is_64(self) -> None:
        svc = AgingService(_make_hot_store(), _make_warm_store())
        result = svc._compute_checksum("test content")
        assert len(result) == 64

    def test_is_hex_string(self) -> None:
        svc = AgingService(_make_hot_store(), _make_warm_store())
        result = svc._compute_checksum("data")
        int(result, 16)  # Raises if not valid hex

    def test_different_inputs_different_hashes(self) -> None:
        svc = AgingService(_make_hot_store(), _make_warm_store())
        h1 = svc._compute_checksum("alpha")
        h2 = svc._compute_checksum("beta")
        assert h1 != h2

    def test_empty_string(self) -> None:
        svc = AgingService(_make_hot_store(), _make_warm_store())
        result = svc._compute_checksum("")
        assert len(result) == 64


# ===========================================================================
# _verify_checksum_compatibility
# ===========================================================================


class TestVerifyChecksumCompatibility:
    """Tests for AgingService._verify_checksum_compatibility."""

    def test_both_64_chars_returns_true(self) -> None:
        svc = AgingService(_make_hot_store(), _make_warm_store())
        h1 = "a" * 64
        h2 = "b" * 64
        assert svc._verify_checksum_compatibility(h1, h2) is True

    def test_hot_short_returns_false(self) -> None:
        svc = AgingService(_make_hot_store(), _make_warm_store())
        assert svc._verify_checksum_compatibility("short", "b" * 64) is False

    def test_warm_short_returns_false(self) -> None:
        svc = AgingService(_make_hot_store(), _make_warm_store())
        assert svc._verify_checksum_compatibility("a" * 64, "short") is False

    def test_both_short_returns_false(self) -> None:
        svc = AgingService(_make_hot_store(), _make_warm_store())
        assert svc._verify_checksum_compatibility("a", "b") is False

    def test_both_65_chars_returns_false(self) -> None:
        svc = AgingService(_make_hot_store(), _make_warm_store())
        assert svc._verify_checksum_compatibility("a" * 65, "b" * 65) is False

    def test_empty_strings_returns_false(self) -> None:
        svc = AgingService(_make_hot_store(), _make_warm_store())
        assert svc._verify_checksum_compatibility("", "") is False


# ===========================================================================
# _delete_from_hot_store
# ===========================================================================


class TestDeleteFromHotStore:
    """Tests for AgingService._delete_from_hot_store."""

    @pytest.mark.asyncio
    async def test_calls_execute_with_conversation_id(self) -> None:
        conn = MagicMock()
        svc = AgingService(_make_hot_store(conn), _make_warm_store())

        await svc._delete_from_hot_store("conv-42")

        conn.execute.assert_called_once_with(
            "DELETE FROM conversations WHERE conversation_id = ?",
            ["conv-42"],
        )

    @pytest.mark.asyncio
    async def test_raises_when_conn_is_none(self) -> None:
        svc = AgingService(_make_hot_store(conn=None), _make_warm_store())

        with pytest.raises(RuntimeError, match="Hot store not initialized"):
            await svc._delete_from_hot_store("conv-1")


# ===========================================================================
# _delete_batch_from_hot_store
# ===========================================================================


class TestDeleteBatchFromHotStore:
    """Tests for AgingService._delete_batch_from_hot_store."""

    @pytest.mark.asyncio
    async def test_calls_executemany(self) -> None:
        conn = MagicMock()
        svc = AgingService(_make_hot_store(conn), _make_warm_store())
        ids = ["c1", "c2", "c3"]

        await svc._delete_batch_from_hot_store(ids)

        conn.executemany.assert_called_once_with(
            "DELETE FROM conversations WHERE conversation_id = ?",
            [("c1",), ("c2",), ("c3",)],
        )

    @pytest.mark.asyncio
    async def test_empty_list_returns_early(self) -> None:
        conn = MagicMock()
        svc = AgingService(_make_hot_store(conn), _make_warm_store())

        await svc._delete_batch_from_hot_store([])

        conn.executemany.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_when_conn_is_none(self) -> None:
        svc = AgingService(_make_hot_store(conn=None), _make_warm_store())

        with pytest.raises(RuntimeError, match="Hot store not initialized"):
            await svc._delete_batch_from_hot_store(["c1"])

    @pytest.mark.asyncio
    async def test_single_id(self) -> None:
        conn = MagicMock()
        svc = AgingService(_make_hot_store(conn), _make_warm_store())

        await svc._delete_batch_from_hot_store(["only-one"])

        conn.executemany.assert_called_once_with(
            "DELETE FROM conversations WHERE conversation_id = ?",
            [("only-one",)],
        )


# ===========================================================================
# get_migration_stats
# ===========================================================================


class TestGetMigrationStats:
    """Tests for AgingService.get_migration_stats."""

    @pytest.mark.asyncio
    async def test_both_stores_connected(self) -> None:
        hot_conn = MagicMock()
        hot_conn.execute.return_value.fetchone.return_value = (42,)
        warm_conn = MagicMock()
        warm_conn.execute.return_value.fetchone.return_value = (17,)

        svc = AgingService(_make_hot_store(hot_conn), _make_warm_store(warm_conn))

        stats = await svc.get_migration_stats()

        assert stats == {"hot_records": 42, "warm_records": 17}

    @pytest.mark.asyncio
    async def test_hot_conn_none(self) -> None:
        warm_conn = MagicMock()
        warm_conn.execute.return_value.fetchone.return_value = (5,)
        svc = AgingService(_make_hot_store(conn=None), _make_warm_store(warm_conn))

        stats = await svc.get_migration_stats()

        assert stats == {"hot_records": 0, "warm_records": 5}

    @pytest.mark.asyncio
    async def test_warm_conn_none(self) -> None:
        hot_conn = MagicMock()
        hot_conn.execute.return_value.fetchone.return_value = (10,)
        svc = AgingService(_make_hot_store(hot_conn), _make_warm_store(conn=None))

        stats = await svc.get_migration_stats()

        assert stats == {"hot_records": 10, "warm_records": 0}

    @pytest.mark.asyncio
    async def test_both_conn_none(self) -> None:
        svc = AgingService(_make_hot_store(conn=None), _make_warm_store(conn=None))

        stats = await svc.get_migration_stats()

        assert stats == {"hot_records": 0, "warm_records": 0}

    @pytest.mark.asyncio
    async def test_fetchone_returns_none(self) -> None:
        hot_conn = MagicMock()
        hot_conn.execute.return_value.fetchone.return_value = None
        warm_conn = MagicMock()
        warm_conn.execute.return_value.fetchone.return_value = None

        svc = AgingService(_make_hot_store(hot_conn), _make_warm_store(warm_conn))

        stats = await svc.get_migration_stats()

        assert stats == {"hot_records": 0, "warm_records": 0}


# ===========================================================================
# migrate_hot_to_warm (batch mode)
# ===========================================================================


class TestMigrateHotToWarmBatch:
    """Tests for batch migration path (USE_BATCH_MIGRATION=true)."""

    @pytest.fixture
    def batch_svc(self) -> AgingService:
        """Create an AgingService with one eligible record, batch mode."""
        row = _make_eligible_row(
            content="Alpha. Beta. Gamma. Delta.",
            embedding=[0.1, -0.2, 0.3],
            content_hash="a" * 64,
        )
        conn = _setup_conn_with_rows([row])
        return AgingService(_make_hot_store(conn), _make_warm_store())

    @pytest.mark.asyncio
    async def test_batch_migration_returns_stats(self, batch_svc: AgingService) -> None:
        with patch("akosha.storage.aging.os.getenv", return_value="true"):
            stats = await batch_svc.migrate_hot_to_warm(cutoff_days=7)

        assert isinstance(stats, MigrationStats)
        assert stats.records_migrated == 1
        assert stats.errors == 0
        assert stats.start_time is not None
        assert stats.end_time is not None

    @pytest.mark.asyncio
    async def test_batch_insert_called(self, batch_svc: AgingService) -> None:
        with patch("akosha.storage.aging.os.getenv", return_value="true"):
            await batch_svc.migrate_hot_to_warm(cutoff_days=7)

        batch_svc.warm_store.insert_batch.assert_called_once()

    @pytest.mark.asyncio
    async def test_batch_delete_called(self, batch_svc: AgingService) -> None:
        with patch("akosha.storage.aging.os.getenv", return_value="true"):
            await batch_svc.migrate_hot_to_warm(cutoff_days=7)

        batch_svc.hot_store.conn.executemany.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_eligible_records_returns_early(self) -> None:
        conn = _setup_conn_with_rows([])
        svc = AgingService(_make_hot_store(conn), _make_warm_store())

        with patch("akosha.storage.aging.os.getenv", return_value="true"):
            stats = await svc.migrate_hot_to_warm(cutoff_days=7)

        assert stats.records_migrated == 0
        assert stats.errors == 0
        svc.warm_store.insert_batch.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_error_counts_whole_batch(self) -> None:
        row = _make_eligible_row()
        conn = _setup_conn_with_rows([row])
        svc = AgingService(_make_hot_store(conn), _make_warm_store())
        svc.warm_store.insert_batch = AsyncMock(side_effect=RuntimeError("boom"))

        with patch("akosha.storage.aging.os.getenv", return_value="true"):
            stats = await svc.migrate_hot_to_warm(cutoff_days=7)

        assert stats.errors == 1
        assert stats.records_migrated == 0

    @pytest.mark.asyncio
    async def test_batch_bytes_freed(self, batch_svc: AgingService) -> None:
        with patch("akosha.storage.aging.os.getenv", return_value="true"):
            stats = await batch_svc.migrate_hot_to_warm(cutoff_days=7)

        assert stats.bytes_freed == 2500  # 1 record * 2500

    @pytest.mark.asyncio
    async def test_multiple_records_in_one_batch(self) -> None:
        rows = [_make_eligible_row(i) for i in range(3)]
        conn = _setup_conn_with_rows(rows)
        svc = AgingService(_make_hot_store(conn), _make_warm_store())

        with patch("akosha.storage.aging.os.getenv", return_value="true"):
            stats = await svc.migrate_hot_to_warm(cutoff_days=7)

        assert stats.records_migrated == 3
        assert stats.bytes_freed == 7500  # 3 * 2500

    @pytest.mark.asyncio
    async def test_records_exceeding_batch_size_split(self) -> None:
        rows = [_make_eligible_row(i) for i in range(5)]
        conn = _setup_conn_with_rows(rows)
        svc = AgingService(_make_hot_store(conn), _make_warm_store())

        # _get_eligible_records converts tuples to dicts; simulate that
        records_as_dicts = [
            {
                "system_id": r[0],
                "conversation_id": r[1],
                "content": r[2],
                "embedding": r[3],
                "timestamp": r[4],
                "metadata": r[5],
                "content_hash": r[6],
            }
            for r in rows
        ]

        # Directly test _migrate_batch with a small batch_size
        stats = MigrationStats(start_time=datetime.now(UTC))
        with (
            patch("akosha.storage.aging.os.getenv", return_value="true"),
            patch("akosha.models.WarmRecord", MagicMock),
        ):
            result = await svc._migrate_batch(
                records_as_dicts, stats, stats.start_time, batch_size=2
            )

        # 5 records in batches of 2 => 3 batches
        assert result.records_migrated == 5
        assert svc.warm_store.insert_batch.call_count == 3

    @pytest.mark.asyncio
    async def test_embeddings_quantized_to_ints(self, batch_svc: AgingService) -> None:
        with patch("akosha.storage.aging.os.getenv", return_value="true"):
            await batch_svc.migrate_hot_to_warm(cutoff_days=7)

        call = batch_svc.warm_store.insert_batch.call_args
        warm_records = call[0][0]
        for rec in warm_records:
            assert all(isinstance(v, int) for v in rec.embedding)


# ===========================================================================
# migrate_hot_to_warm (sequential / legacy mode)
# ===========================================================================


class TestMigrateHotToWarmSequential:
    """Tests for sequential migration path (USE_BATCH_MIGRATION=false)."""

    @pytest.fixture
    def seq_svc(self) -> AgingService:
        """Create an AgingService with one eligible record, sequential mode."""
        row = _make_eligible_row(
            content="Alpha. Beta. Gamma. Delta.",
            embedding=[0.1, -0.2, 0.3],
            content_hash="a" * 64,
        )
        conn = _setup_conn_with_rows([row])
        return AgingService(_make_hot_store(conn), _make_warm_store())

    @pytest.mark.asyncio
    async def test_sequential_migration_succeeds(self, seq_svc: AgingService) -> None:
        mock_warm_record = MagicMock()
        with (
            patch("akosha.storage.aging.os.getenv", return_value="false"),
            patch("akosha.models.WarmRecord", return_value=mock_warm_record),
        ):
            stats = await seq_svc.migrate_hot_to_warm(cutoff_days=7)

        assert stats.records_migrated == 1
        assert stats.errors == 0

    @pytest.mark.asyncio
    async def test_sequential_insert_called(self, seq_svc: AgingService) -> None:
        mock_warm_record = MagicMock()
        with (
            patch("akosha.storage.aging.os.getenv", return_value="false"),
            patch("akosha.models.WarmRecord", return_value=mock_warm_record),
        ):
            await seq_svc.migrate_hot_to_warm(cutoff_days=7)

        seq_svc.warm_store.insert.assert_called_once_with(mock_warm_record)

    @pytest.mark.asyncio
    async def test_sequential_delete_called(self, seq_svc: AgingService) -> None:
        mock_warm_record = MagicMock()
        with (
            patch("akosha.storage.aging.os.getenv", return_value="false"),
            patch("akosha.models.WarmRecord", return_value=mock_warm_record),
        ):
            await seq_svc.migrate_hot_to_warm(cutoff_days=7)

        seq_svc.hot_store.conn.execute.assert_any_call(
            "DELETE FROM conversations WHERE conversation_id = ?",
            ["conv-0"],
        )

    @pytest.mark.asyncio
    async def test_sequential_checksum_verification(self, seq_svc: AgingService) -> None:
        """When content_hash is present, checksum compatibility is verified."""
        mock_warm_record = MagicMock()
        with (
            patch("akosha.storage.aging.os.getenv", return_value="false"),
            patch("akosha.models.WarmRecord", return_value=mock_warm_record),
        ):
            stats = await seq_svc.migrate_hot_to_warm(cutoff_days=7)

        # content_hash "a"*64 is 64 chars; warm_checksum from sha256 is also 64
        # so verification should pass -- no error increment
        assert stats.errors == 0

    @pytest.mark.asyncio
    async def test_sequential_checksum_mismatch_continues(self, seq_svc: AgingService) -> None:
        """Checksum mismatch logs a warning but does not stop migration."""
        # content_hash of length 32 (not 64) will cause verification to fail
        row = _make_eligible_row(content_hash="a" * 32)
        conn = _setup_conn_with_rows([row])
        svc = AgingService(_make_hot_store(conn), _make_warm_store())

        mock_warm_record = MagicMock()
        with (
            patch("akosha.storage.aging.os.getenv", return_value="false"),
            patch("akosha.models.WarmRecord", return_value=mock_warm_record),
        ):
            stats = await svc.migrate_hot_to_warm(cutoff_days=7)

        # Should still succeed despite checksum mismatch
        assert stats.records_migrated == 1

    @pytest.mark.asyncio
    async def test_sequential_no_content_hash_skips_verification(
        self, seq_svc: AgingService
    ) -> None:
        """When content_hash is empty/falsy, checksum verification is skipped."""
        row = _make_eligible_row(content_hash="")
        conn = _setup_conn_with_rows([row])
        svc = AgingService(_make_hot_store(conn), _make_warm_store())

        mock_warm_record = MagicMock()
        with (
            patch("akosha.storage.aging.os.getenv", return_value="false"),
            patch("akosha.models.WarmRecord", return_value=mock_warm_record),
        ):
            stats = await svc.migrate_hot_to_warm(cutoff_days=7)

        assert stats.records_migrated == 1
        assert stats.errors == 0

    @pytest.mark.asyncio
    async def test_sequential_error_increments_error_count(self, seq_svc: AgingService) -> None:
        """Exception during insert increments errors, continues to next record."""
        row = _make_eligible_row()
        conn = _setup_conn_with_rows([row])
        svc = AgingService(_make_hot_store(conn), _make_warm_store())
        svc.warm_store.insert = AsyncMock(side_effect=RuntimeError("insert fail"))

        with (
            patch("akosha.storage.aging.os.getenv", return_value="false"),
            patch("akosha.models.WarmRecord"),
        ):
            stats = await svc.migrate_hot_to_warm(cutoff_days=7)

        assert stats.errors == 1
        assert stats.records_migrated == 0

    @pytest.mark.asyncio
    async def test_sequential_bytes_freed(self, seq_svc: AgingService) -> None:
        mock_warm_record = MagicMock()
        with (
            patch("akosha.storage.aging.os.getenv", return_value="false"),
            patch("akosha.models.WarmRecord", return_value=mock_warm_record),
        ):
            stats = await seq_svc.migrate_hot_to_warm(cutoff_days=7)

        assert stats.bytes_freed == 2500

    @pytest.mark.asyncio
    async def test_sequential_multiple_records(self) -> None:
        rows = [_make_eligible_row(i) for i in range(4)]
        conn = _setup_conn_with_rows(rows)
        svc = AgingService(_make_hot_store(conn), _make_warm_store())

        mock_warm_record = MagicMock()
        with (
            patch("akosha.storage.aging.os.getenv", return_value="false"),
            patch("akosha.models.WarmRecord", return_value=mock_warm_record),
        ):
            stats = await svc.migrate_hot_to_warm(cutoff_days=7)

        assert stats.records_migrated == 4
        assert svc.warm_store.insert.call_count == 4


# ===========================================================================
# _migrate_batch
# ===========================================================================


class TestMigrateBatch:
    """Direct tests for the _migrate_batch method."""

    @staticmethod
    def _row_to_dict(row: tuple) -> dict:
        """Convert a raw SQL row tuple to the dict format _migrate_batch expects."""
        return {
            "system_id": row[0],
            "conversation_id": row[1],
            "content": row[2],
            "embedding": row[3],
            "timestamp": row[4],
            "metadata": row[5],
            "content_hash": row[6],
        }

    @pytest.mark.asyncio
    async def test_basic_batch(self) -> None:
        records = [self._row_to_dict(_make_eligible_row(i)) for i in range(2)]
        stats = MigrationStats(start_time=datetime.now(UTC))
        conn = MagicMock()
        svc = AgingService(_make_hot_store(conn), _make_warm_store())

        with (
            patch("akosha.storage.aging.os.getenv", return_value="true"),
            patch("akosha.models.WarmRecord", MagicMock),
        ):
            result = await svc._migrate_batch(records, stats, stats.start_time)

        assert result.records_migrated == 2
        assert result.errors == 0
        svc.warm_store.insert_batch.assert_called_once()

    @pytest.mark.asyncio
    async def test_error_in_batch_increments_by_batch_size(self) -> None:
        records = [self._row_to_dict(_make_eligible_row(i)) for i in range(3)]
        stats = MigrationStats(start_time=datetime.now(UTC))
        conn = MagicMock()
        svc = AgingService(_make_hot_store(conn), _make_warm_store())
        svc.warm_store.insert_batch = AsyncMock(side_effect=RuntimeError("fail"))

        with (
            patch("akosha.storage.aging.os.getenv", return_value="true"),
            patch("akosha.models.WarmRecord", MagicMock),
        ):
            result = await svc._migrate_batch(records, stats, stats.start_time)

        assert result.errors == 3
        assert result.records_migrated == 0

    @pytest.mark.asyncio
    async def test_custom_batch_size_splits_correctly(self) -> None:
        records = [self._row_to_dict(_make_eligible_row(i)) for i in range(5)]
        stats = MigrationStats(start_time=datetime.now(UTC))
        conn = MagicMock()
        svc = AgingService(_make_hot_store(conn), _make_warm_store())

        with (
            patch("akosha.storage.aging.os.getenv", return_value="true"),
            patch("akosha.models.WarmRecord", MagicMock),
        ):
            result = await svc._migrate_batch(records, stats, stats.start_time, batch_size=2)

        assert result.records_migrated == 5
        # 5 records / batch_size 2 => 3 insert_batch calls (2, 2, 1)
        assert svc.warm_store.insert_batch.call_count == 3

    @pytest.mark.asyncio
    async def test_end_time_is_set(self) -> None:
        records = [self._row_to_dict(_make_eligible_row())]
        stats = MigrationStats(start_time=datetime.now(UTC))
        conn = MagicMock()
        svc = AgingService(_make_hot_store(conn), _make_warm_store())

        with (
            patch("akosha.storage.aging.os.getenv", return_value="true"),
            patch("akosha.models.WarmRecord", MagicMock),
        ):
            result = await svc._migrate_batch(records, stats, stats.start_time)

        assert result.end_time is not None
        assert result.end_time >= result.start_time

    @pytest.mark.asyncio
    async def test_empty_records_list(self) -> None:
        stats = MigrationStats(start_time=datetime.now(UTC))
        conn = MagicMock()
        svc = AgingService(_make_hot_store(conn), _make_warm_store())

        with (
            patch("akosha.storage.aging.os.getenv", return_value="true"),
            patch("akosha.models.WarmRecord", MagicMock),
        ):
            result = await svc._migrate_batch([], stats, stats.start_time)

        assert result.records_migrated == 0
        svc.warm_store.insert_batch.assert_not_called()
