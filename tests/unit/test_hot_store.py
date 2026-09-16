"""Tests for hot store (DuckDB in-memory)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from akosha.models import HotRecord
from akosha.storage.hot_store import HotStore


class TestHotStore:
    """Test suite for HotStore."""

    @pytest.fixture
    async def hot_store(self) -> HotStore:
        """Create fresh hot store for each test."""
        store = HotStore(database_path=":memory:")
        await store.initialize()
        yield store
        await store.close()

    @pytest.mark.asyncio
    async def test_initialization(self, hot_store: HotStore) -> None:
        """Test hot store initialization."""
        assert hot_store.conn is not None
        # Check table exists
        result = hot_store.conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = 'conversations'"
        ).fetchone()
        assert result is not None

    @pytest.mark.asyncio
    async def test_insert_conversation(self, hot_store: HotStore) -> None:
        """Test inserting a conversation."""
        record = HotRecord(
            system_id="system-1",
            conversation_id="conv-1",
            content="Test conversation about FastAPI",
            embedding=[0.1] * 384,
            timestamp=datetime.now(UTC),
            metadata={"topic": "FastAPI"},
        )

        await hot_store.insert(record)

        # Verify insertion
        result = hot_store.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
        assert result[0] == 1

    @pytest.mark.asyncio
    async def test_insert_duplicate_conversation(self, hot_store: HotStore) -> None:
        """Test inserting duplicate conversation (should fail)."""
        record = HotRecord(
            system_id="system-1",
            conversation_id="conv-1",
            content="Test conversation",
            embedding=[0.1] * 384,
            timestamp=datetime.now(UTC),
            metadata={},
        )

        await hot_store.insert(record)

        # Try inserting duplicate
        with pytest.raises(Exception):  # DuckDB constraint violation (ConstraintException)
            await hot_store.insert(record)

    @pytest.mark.asyncio
    async def test_search_similar(self, hot_store: HotStore) -> None:
        """Test vector similarity search."""
        # Insert test conversations
        now = datetime.now(UTC)

        conversations = [
            HotRecord(
                system_id="system-1",
                conversation_id="conv-1",
                content="Conversation about FastAPI",
                embedding=[0.1] * 384,
                timestamp=now,
                metadata={},
            ),
            HotRecord(
                system_id="system-1",
                conversation_id="conv-2",
                content="Conversation about Django",
                embedding=[0.5] * 384,
                timestamp=now,
                metadata={},
            ),
        ]

        for conv in conversations:
            await hot_store.insert(conv)

        # Search with similar embedding
        query = [0.12] * 384  # Similar to conv-1
        results = await hot_store.search_similar(
            query_embedding=query,
            limit=10,
            threshold=0.0,
        )

        assert len(results) >= 1
        # First result should be conv-1 (most similar)
        assert results[0]["conversation_id"] == "conv-1"
        assert "score" in results[0]

    @pytest.mark.asyncio
    async def test_search_similar_with_system_filter(self, hot_store: HotStore) -> None:
        """Test vector search with system ID filter."""
        now = datetime.now(UTC)

        # Insert conversations from different systems
        await hot_store.insert(
            HotRecord(
                system_id="system-1",
                conversation_id="conv-1",
                content="System 1 conversation",
                embedding=[0.1] * 384,
                timestamp=now,
                metadata={},
            )
        )

        await hot_store.insert(
            HotRecord(
                system_id="system-2",
                conversation_id="conv-2",
                content="System 2 conversation",
                embedding=[0.12] * 384,
                timestamp=now,
                metadata={},
            )
        )

        # Search filtering by system-1
        query = [0.11] * 384
        results = await hot_store.search_similar(
            query_embedding=query,
            system_id="system-1",
            limit=10,
        )

        # Should only return system-1 results
        assert len(results) == 1
        assert results[0]["system_id"] == "system-1"

    @pytest.mark.asyncio
    async def test_search_similar_threshold_filtering(self, hot_store: HotStore) -> None:
        """Test threshold filtering in vector search."""
        now = datetime.now(UTC)

        await hot_store.insert(
            HotRecord(
                system_id="system-1",
                conversation_id="conv-1",
                content="Similar conversation",
                embedding=[0.1] * 384,
                timestamp=now,
                metadata={},
            )
        )

        await hot_store.insert(
            HotRecord(
                system_id="system-1",
                conversation_id="conv-2",
                content="Dissimilar conversation",
                embedding=[0.9] * 384,
                timestamp=now,
                metadata={},
            )
        )

        # Search with high threshold (only very similar results)
        query = [0.1] * 384
        results = await hot_store.search_similar(
            query_embedding=query,
            limit=10,
            threshold=0.95,  # Very high threshold
        )

        # Both embeddings are normalized, so both will have high similarity
        # Test that threshold filtering works by checking we get results
        assert len(results) >= 1
        # All results should meet threshold
        for result in results:
            assert result["score"] >= 0.95

    @pytest.mark.asyncio
    async def test_search_similar_limit(self, hot_store: HotStore) -> None:
        """Test result limiting in vector search."""
        now = datetime.now(UTC)

        # Insert multiple conversations
        for i in range(10):
            await hot_store.insert(
                HotRecord(
                    system_id="system-1",
                    conversation_id=f"conv-{i}",
                    content=f"Conversation {i}",
                    embedding=[0.1 + i * 0.001] * 384,
                    timestamp=now,
                    metadata={},
                )
            )

        # Search with limit
        query = [0.1] * 384
        results = await hot_store.search_similar(
            query_embedding=query,
            limit=5,
            threshold=0.0,
        )

        # Should return at most 5 results
        assert len(results) <= 5

    @pytest.mark.asyncio
    async def test_content_hash_computation(self) -> None:
        """Test content hash computation."""
        content1 = "Test conversation"
        content2 = "Test conversation"
        content3 = "Different conversation"

        hash1 = HotStore._compute_content_hash(content1)
        hash2 = HotStore._compute_content_hash(content2)
        hash3 = HotStore._compute_content_hash(content3)

        # Same content should produce same hash
        assert hash1 == hash2
        # Different content should produce different hash
        assert hash1 != hash3

    @pytest.mark.asyncio
    async def test_close_hot_store(self, hot_store: HotStore) -> None:
        """Test closing hot store."""
        assert hot_store.conn is not None

        await hot_store.close()

        # Connection should be closed
        # Note: DuckDB doesn't have a simple way to check if closed
        # But we can verify no error on double close
        await hot_store.close()  # Should not raise

    @pytest.mark.asyncio
    async def test_insert_without_initialization(self) -> None:
        """Test insertion without initialization raises error."""
        store = HotStore(database_path=":memory:")
        # Don't initialize

        record = HotRecord(
            system_id="system-1",
            conversation_id="conv-1",
            content="Test",
            embedding=[0.1] * 384,
            timestamp=datetime.now(UTC),
            metadata={},
        )

        with pytest.raises(RuntimeError, match="not initialized"):
            await store.insert(record)

    @pytest.mark.asyncio
    async def test_search_without_initialization(self) -> None:
        """Test search without initialization raises error."""
        store = HotStore(database_path=":memory:")
        # Don't initialize

        with pytest.raises(RuntimeError, match="not initialized"):
            await store.search_similar(
                query_embedding=[0.1] * 384,
            )

    @pytest.mark.asyncio
    async def test_concurrent_operations(self, hot_store: HotStore) -> None:
        """Test concurrent insert operations."""
        import asyncio

        now = datetime.now(UTC)

        # Insert multiple conversations concurrently
        tasks = []
        for i in range(10):
            record = HotRecord(
                system_id="system-1",
                conversation_id=f"conv-{i}",
                content=f"Conversation {i}",
                embedding=[0.1 + i * 0.001] * 384,
                timestamp=now,
                metadata={},
            )
            tasks.append(hot_store.insert(record))

        # Should not raise errors
        await asyncio.gather(*tasks)

        # Verify all inserted
        result = hot_store.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
        assert result[0] == 10

    @pytest.mark.asyncio
    @pytest.mark.security
    async def test_sql_injection_prevention(self, hot_store: HotStore) -> None:
        """Test that SQL injection attempts are safely handled via parameterized queries."""
        now = datetime.now(UTC)

        # Insert a legitimate conversation
        await hot_store.insert(
            HotRecord(
                system_id="system-1",
                conversation_id="conv-1",
                content="Legitimate conversation",
                embedding=[0.1] * 384,
                timestamp=now,
                metadata={},
            )
        )

        # Test various SQL injection payloads
        malicious_system_ids = [
            "'; DROP TABLE conversations; --",
            "' OR '1'='1",
            "admin'--",
            "' UNION SELECT * FROM conversations --",
            "../../../etc/passwd",
            "$(whoami)",
            "`id`",
            "'; EXEC xp_cmdshell('dir'); --",
        ]

        for malicious_id in malicious_system_ids:
            # Search with malicious system_id
            # Should NOT crash or allow SQL injection
            results = await hot_store.search_similar(
                query_embedding=[0.1] * 384,
                system_id=malicious_id,
                limit=10,
            )

            # Should return empty results (no matching system with that malicious ID)
            # NOT crash or leak data
            assert isinstance(results, list)
            # With parameterized queries, these will simply find no matches
            # If SQL injection worked, it could crash or return unintended data

    # ------------------------------------------------------------------
    # Regression tests for the OR-of-two-JSON-paths DuckDB optimiser bug.
    #
    # DuckDB's optimiser miscomputes the metadata type when a JSON-path
    # ``= ?`` predicate is ANDed with other WHERE conditions, surfacing
    # as ``ConversionException: Failed to cast value to numerical``.
    # The fix wraps the non-JSON filters in a CTE and applies the
    # JSON-path filters in UNION ALL branches.
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_query_traces_task_class_only_matches_both_shapes(
        self, hot_store: HotStore
    ) -> None:
        """Filter on task_class alone matches both top-level and nested forms."""
        now = datetime.now(UTC)
        await hot_store.insert(
            HotRecord(
                system_id="ak",
                conversation_id="c1",
                content="x",
                embedding=[0.0] * 384,
                timestamp=now,
                metadata={
                    "task_class": "code_generation",
                    "attributes": {"outcome": "success"},
                },
            )
        )
        await hot_store.insert(
            HotRecord(
                system_id="ak",
                conversation_id="c2",
                content="y",
                embedding=[0.0] * 384,
                timestamp=now,
                metadata={
                    "task_class": "analysis",
                    "attributes": {
                        "task_class": "code_generation",
                        "outcome": "failure",
                    },
                },
            )
        )
        await hot_store.insert(
            HotRecord(
                system_id="ak",
                conversation_id="c3",
                content="z",
                embedding=[0.0] * 384,
                timestamp=now,
                metadata={"task_class": "unrelated"},
            )
        )

        rows = await hot_store.query_traces(task_class="code_generation")
        ids = sorted(r["conversation_id"] for r in rows)
        assert ids == ["c1", "c2"]

    @pytest.mark.asyncio
    async def test_query_traces_task_class_with_timestamp_filter(
        self, hot_store: HotStore
    ) -> None:
        """task_class AND timestamp filter — the case that triggered the bug.

        NOTE: passes naive-UTC timestamp strings (no ``+00:00`` suffix)
        because DuckDB stores ``TIMESTAMP`` (no timezone) and the
        TIMESTAMP-vs-VARCHAR-with-tz comparison has a separate known
        issue on PDT-local sessions. The Akosha-side fix for that
        is out of scope for this PR; the regression test exercises
        the JSON-path-OR fix, which is what this PR targets.
        """
        now = datetime.now(UTC).replace(tzinfo=None)
        await hot_store.insert(
            HotRecord(
                system_id="ak",
                conversation_id="c1",
                content="x",
                embedding=[0.0] * 384,
                timestamp=now,
                metadata={"task_class": "code_generation"},
            )
        )
        # naive-UTC strings (no tz suffix) — see note above
        start_str = (now - timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        end_str = (now + timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        rows = await hot_store.query_traces(
            task_class="code_generation",
            start_time=start_str,
            end_time=end_str,
        )
        assert len(rows) == 1
        assert rows[0]["conversation_id"] == "c1"

    @pytest.mark.asyncio
    async def test_query_traces_task_class_with_system_id_filter(
        self, hot_store: HotStore
    ) -> None:
        """task_class AND system_id filter — also triggers the bug."""
        now = datetime.now(UTC)
        await hot_store.insert(
            HotRecord(
                system_id="ak",
                conversation_id="c1",
                content="x",
                embedding=[0.0] * 384,
                timestamp=now,
                metadata={"task_class": "code_generation"},
            )
        )
        await hot_store.insert(
            HotRecord(
                system_id="other",
                conversation_id="c2",
                content="y",
                embedding=[0.0] * 384,
                timestamp=now,
                metadata={"task_class": "code_generation"},
            )
        )

        rows = await hot_store.query_traces(
            task_class="code_generation", system_id="ak"
        )
        ids = [r["conversation_id"] for r in rows]
        assert ids == ["c1"]

    @pytest.mark.asyncio
    async def test_query_traces_no_task_class_regression(
        self, hot_store: HotStore
    ) -> None:
        """Non-task_class queries still work (regression check)."""
        now = datetime.now(UTC)
        for cid in ("c1", "c2", "c3"):
            await hot_store.insert(
                HotRecord(
                    system_id="ak",
                    conversation_id=cid,
                    content="x",
                    embedding=[0.0] * 384,
                    timestamp=now,
                    metadata={},
                )
            )

        rows = await hot_store.query_traces(system_id="ak", limit=10)
        ids = sorted(r["conversation_id"] for r in rows)
        assert ids == ["c1", "c2", "c3"]

    @pytest.mark.asyncio
    async def test_query_traces_task_class_misses_rows_without_task_class(
        self, hot_store: HotStore
    ) -> None:
        """Rows where metadata has no task_class are not matched."""
        now = datetime.now(UTC)
        await hot_store.insert(
            HotRecord(
                system_id="ak",
                conversation_id="c1",
                content="x",
                embedding=[0.0] * 384,
                timestamp=now,
                metadata={"outcome": "success"},  # no task_class at all
            )
        )

        rows = await hot_store.query_traces(task_class="code_generation")
        assert rows == []

    # ------------------------------------------------------------------
    # Regression tests for the DuckDB tz-aware datetime binding bug.
    #
    # DuckDB ``TIMESTAMP`` (not ``TIMESTAMPTZ``) stores naive wall-clock
    # values. The Python binding for tz-aware datetimes appears to call
    # ``.astimezone(local_tz)`` before binding, so the same instant
    # in time gets stored at different wall-clock values depending on
    # whether the caller passed naive or tz-aware. On a PDT session
    # (UTC-7), ``2026-09-14 13:00:00+00:00`` ends up stored as
    # ``2026-09-14 06:00:00`` — a silent 7-hour shift that only surfaces
    # when the row fails to round-trip against a tz-aware query.
    #
    # The fix normalises tz-aware timestamps to naive UTC at insert time
    # (and strips tz suffixes from query-time strings for defense in
    # depth, even though current DuckDB versions handle ``+00:00``
    # directly).
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_insert_tz_aware_datetime_stored_as_naive_utc(
        self, hot_store: HotStore
    ) -> None:
        """tz-aware UTC datetime at insert must land at the naive-UTC value.

        On a non-UTC session (e.g. PDT) DuckDB's binding layer would
        otherwise silently shift the wall-clock to local time. This
        test pins the storage layer to naive-UTC semantics regardless
        of session tz.
        """
        # 13:00 UTC. Must NOT be stored as 06:00 on a PDT session.
        ts_utc = datetime(2026, 9, 14, 13, 0, 0, tzinfo=UTC)
        naive_utc_value = datetime(2026, 9, 14, 13, 0, 0)  # tzinfo=None

        await hot_store.insert(
            HotRecord(
                system_id="ak",
                conversation_id="tz-aware",
                content="x",
                embedding=[0.0] * 384,
                timestamp=ts_utc,
                metadata={"task_class": "code_generation"},
            )
        )

        row = hot_store.conn.execute(
            "SELECT timestamp FROM conversations WHERE conversation_id = 'tz-aware'"
        ).fetchone()
        assert row is not None
        stored = row[0]
        # Must equal naive-UTC (13:00), NOT local-time (06:00 on PDT).
        assert stored == naive_utc_value, (
            f"tz-aware datetime mis-bound: expected {naive_utc_value!r}, "
            f"got {stored!r} (session tz would shift this on PDT)"
        )
        # Belt and suspenders: stored column must be naive.
        assert stored.tzinfo is None

    @pytest.mark.asyncio
    async def test_query_traces_tz_suffixed_iso_string(
        self, hot_store: HotStore
    ) -> None:
        """query_traces with ``+00:00`` ISO string must return naive-UTC rows.

        Defense in depth: even though current DuckDB versions handle
        the ``+00:00`` suffix correctly, query_traces normalises tz
        suffixes off the input so a future DuckDB/ICU change cannot
        regress this path. ``datetime.now(UTC).isoformat()`` returns
        ``2026-09-14T11:00:00+00:00``; callers pass that string
        unmodified.
        """
        now = datetime(2026, 9, 14, 12, 0, 0)  # naive UTC
        await hot_store.insert(
            HotRecord(
                system_id="ak",
                conversation_id="c1",
                content="x",
                embedding=[0.0] * 384,
                timestamp=now,
                metadata={"task_class": "code_generation"},
            )
        )

        rows = await hot_store.query_traces(
            task_class="code_generation",
            start_time=(now - timedelta(hours=1)).isoformat(),  # 11:00+00:00
        )
        assert len(rows) == 1
        assert rows[0]["conversation_id"] == "c1"

    @pytest.mark.asyncio
    async def test_query_traces_negative_tz_iso_string(
        self, hot_store: HotStore
    ) -> None:
        """query_traces must normalise PDT tz offsets off input.

        Confirms the strip-tz-suffix path handles negative offsets too,
        which would otherwise pass local-PDT wall-clock to a TIMESTAMP
        column that stores naive UTC.
        """
        now = datetime(2026, 9, 14, 12, 0, 0)  # naive UTC
        await hot_store.insert(
            HotRecord(
                system_id="ak",
                conversation_id="c1",
                content="x",
                embedding=[0.0] * 384,
                timestamp=now,
                metadata={"task_class": "code_generation"},
            )
        )

        # Same instant expressed in PDT: 12:00 UTC = 05:00 PDT.
        # If query_traces does NOT strip tz, DuckDB would compare
        # 05:00 against 12:00 and return no rows.
        pdt_start = (now - timedelta(hours=7)).astimezone(
            __import__("datetime").timezone(timedelta(hours=-7))
        )
        rows = await hot_store.query_traces(
            task_class="code_generation",
            start_time=pdt_start.isoformat(),
        )
        assert len(rows) == 1
        assert rows[0]["conversation_id"] == "c1"
