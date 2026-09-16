"""AkoSHA HotStore — extends Oneiric DuckdbHotStore with code-graph overlay.

Post-Phase 5 follow-up (spec §Phase 5 follow-up). The
conversations-table surface is inherited from Oneiric's substrate;
AkoSHA retains only the code-graph overlay + ``query_traces`` (an
AkoSHA-specific JSON-path CTE query). Conversations-table DDL lives
in ``oneiric/adapters/vector/duckdb_hot_store.py`` — substrate is the
single source of truth. See commits ``93f60cd`` + ``198564e`` in
oneiric for the substrate lift.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import duckdb

from oneiric.adapters.vector.duckdb_hot_store import DuckdbHotStore

from akosha.processing.embedding_dim import resolve_embedding_dim

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def _strip_tz_suffix(s: str) -> str:
    """Strip ISO-8601 tz suffix off a timestamp string (``+HH:MM`` / ``-HH:MM`` / ``Z``).

    DuckDB currently accepts the ``+00:00`` suffix against a
    ``TIMESTAMP`` column without complaint, but stripping it is
    defense in depth — a DuckDB/ICU upgrade could regress this
    path, and query_traces is on the hot read-side of every Bodai
    component. The space-separator ``YYYY-MM-DD HH:MM:SS`` form
    (also accepted by DuckDB) is unchanged.
    """
    if "T" not in s:
        return s  # already space-separated, not ISO; leave as-is
    # Trailing "Z" → drop it.
    if s.endswith("Z"):
        return s[:-1]
    # Trailing "+HH:MM" or "-HH:MM" → drop it (only if it's a tz offset,
    # not a date-parsed value; ISO dates don't carry these suffixes).
    for sep in ("+", "-"):
        idx = s.rfind(sep)
        if idx > 10:  # past the date portion (YYYY-MM-DD)
            tail = s[idx:]
            if ":" in tail and len(tail) == 6:  # +HH:MM or -HH:MM form
                return s[:idx]
    return s


class HotStore(DuckdbHotStore):
    """AkoSHA's HotStore: Oneiric DuckdbHotStore + code-graph overlay.

    The conversations-table surface (``__init__``, ``initialize``,
    ``insert``, ``search_similar``, ``close``,
    ``_compute_content_hash``) is inherited from substrate; AkoSHA
    adds the code-graph methods. Constructor delegates to
    ``DuckdbHotStore.__init__`` after resolving AkoSHA's
    :func:`resolve_embedding_dim` (queries the embedding service
    at runtime), so the schema dim matches the active backend.

    **Development/test backend only** — in-memory DuckDB loses
    rows on restart; file-backed DuckDB is not safe on ephemeral
    filesystems. Use :class:`akosha.storage.PgvectorHotStore` for
    production persistence.
    """

    def __init__(
        self,
        database_path: str | Path = ":memory:",
        embedding_dim: int | None = None,
    ) -> None:
        # Resolve AkoSHA's smarter embedding dim before delegating so
        # the schema dim matches the active backend.
        if embedding_dim is None:
            embedding_dim = resolve_embedding_dim()
        super().__init__(
            database_path=database_path,
            embedding_dim=embedding_dim,
        )

    async def search_similar(
        self,
        query_embedding: list[float],
        system_id: str | None = None,
        limit: int = 10,
        threshold: float = 0.7,
    ) -> list[dict[str, Any]]:
        """Search for similar conversations; returns ``similarity`` key.

        AkoSHA preserves its pre-substrate public API by mapping the
        substrate's ``score`` key to ``similarity``. Substrate's
        ``DuckdbHotStore.search_similar`` returns ``score`` (more
        generic — could be any metric); AkoSHA's consumers expect
        ``similarity`` (the metric this codebase actually computes).
        Delegates everything else to substrate.

        Vector similarity uses ``array_cosine_similarity(...)`` —
        no index required (HNSW skipped; see ``initialize``).
        """
        results = await super().search_similar(
            query_embedding=query_embedding,
            system_id=system_id,
            limit=limit,
            threshold=threshold,
        )
        return [{**r, "similarity": r["score"]} for r in results]

    async def initialize(self) -> None:
        """Initialize conversations table (inherited) + code_graphs table (overlay).

        Preserves AkoSHA's intentional HNSW skip. Substrate's
        ``DuckdbHotStore.initialize()`` attempts HNSW index creation
        (always fails on DuckDB without the ``vss`` extension); the
        pre-Phase-5 akosha code documented this as 5×/cycle operator-UX
        noise with no useful index. We silence ONLY the HNSW message
        via a targeted filter, drop the failed index, and surface the
        akosha intent. Vector search uses brute-force
        ``array_cosine_similarity`` — no index required.
        """

        class _HnswFilter(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                return "HNSW index creation failed" not in record.getMessage()

        substrate_logger = logging.getLogger(
            "oneiric.adapters.vector.duckdb_hot_store"
        )
        hnsw_filter = _HnswFilter()
        substrate_logger.addFilter(hnsw_filter)
        try:
            await super().initialize()
        finally:
            substrate_logger.removeFilter(hnsw_filter)
        if self.conn is not None:
            try:
                self.conn.execute("DROP INDEX IF EXISTS embedding_hnsw_index")
            except Exception:
                pass
        logger.info(
            "HotStore: skipping HNSW index — DuckDB has no native HNSW; "
            "vector search uses array_cosine_similarity (brute-force). "
            "For ANN acceleration, install the `vss` extension."
        )
        await self.initialize_code_graphs_table()

    async def query_traces(
        self,
        system_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        task_class: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query traces using SQL WHERE on metadata JSON attributes.

        AkoSHA-specific extension — DuckdbHotStore has no equivalent.
        Pushes attribute filters (task_class, time range) into the SQL
        WHERE clause rather than fetching all traces and filtering
        in Python. The HNSW index is NOT used for this query.
        """
        async with self._lock:
            if not self.conn:
                raise RuntimeError("Hot store not initialized")

            common_conditions: list[str] = []
            params: list[Any] = []

            if system_id:
                common_conditions.append("system_id = ?")
                params.append(system_id)

            if start_time:
                # Strip tz offset so TIMESTAMP comparison is purely
                # naive-UTC-vs-naive-UTC; ``_strip_tz_suffix`` is a
                # no-op for already-naive strings.
                common_conditions.append("timestamp >= ?")
                params.append(_strip_tz_suffix(start_time))

            if end_time:
                common_conditions.append("timestamp <= ?")
                params.append(_strip_tz_suffix(end_time))

            select_cols = (
                "system_id, conversation_id, content, timestamp, metadata"
            )

            if task_class:
                # Filter on metadata JSON: attributes.task_class OR top-level
                # task_class. DuckDB's optimiser miscomputes the metadata
                # type when a JSON-path ``= ?`` predicate is ANDed with
                # other WHERE conditions (e.g. timestamp, system_id),
                # surfacing ``ConversionException: Failed to cast value
                # to numerical``. A CTE (non-JSON filters) + UNION ALL of
                # two complete queries (one per JSON path) avoids the
                # optimiser path that triggers the cast.
                cte_where = (
                    "WHERE " + " AND ".join(common_conditions)
                    if common_conditions
                    else ""
                )

                inner = (
                    f"SELECT {select_cols} FROM filtered "
                    f"WHERE metadata->>'task_class' = ? "
                    f"UNION ALL "
                    f"SELECT {select_cols} FROM filtered "
                    f"WHERE metadata->'attributes'->>'task_class' = ?"
                )
                query = (
                    f"WITH filtered AS ("
                    f"SELECT {select_cols} FROM conversations {cte_where}"
                    f") "
                    f"SELECT * FROM ({inner}) "
                    f"ORDER BY timestamp DESC LIMIT ?"
                )
                params.extend([task_class, task_class, limit])
            else:
                where_clause = (
                    " AND ".join(common_conditions)
                    if common_conditions
                    else "1=1"
                )
                query = (
                    f"SELECT {select_cols} FROM conversations "
                    f"WHERE {where_clause} "
                    f"ORDER BY timestamp DESC LIMIT ?"
                )
                params.append(limit)

            rows = self.conn.execute(query, params).fetchall()

            return [
                {
                    "system_id": r[0],
                    "conversation_id": r[1],
                    "content": r[2],
                    "timestamp": r[3],
                    "metadata": r[4],
                }
                for r in rows
            ]

    async def initialize_code_graphs_table(self) -> None:
        """Initialize code_graphs table for cross-repo pattern analysis."""
        async with self._lock:
            if not self.conn:
                raise RuntimeError("Hot store not initialized")
            self._create_code_graphs_schema(self.conn)

    def _create_code_graphs_schema(
        self, conn: duckdb.DuckDBPyConnection
    ) -> None:
        """Idempotent CREATE TABLE + indexes for the code_graphs table."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS code_graphs (
                repo_path VARCHAR,
                commit_hash VARCHAR,
                nodes_count INTEGER,
                graph_data JSON,
                metadata JSON,
                ingested_at TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (repo_path, commit_hash)
            )""")
        for name, ddl in (
            (
                "code_graphs repo_path",
                "CREATE INDEX IF NOT EXISTS code_graphs_repo_index "
                "ON code_graphs (repo_path)",
            ),
            (
                "code_graphs nodes_count",
                "CREATE INDEX IF NOT EXISTS code_graphs_nodes_index "
                "ON code_graphs (nodes_count DESC)",
            ),
            (
                "code_graphs ingested_at",
                "CREATE INDEX IF NOT EXISTS code_graphs_ingested_index "
                "ON code_graphs (ingested_at DESC)",
            ),
        ):
            try:
                conn.execute(ddl)
                logger.info(f"Created {name} index")
            except Exception as e:
                logger.warning(f"{name} index creation failed: {e}")

    async def store_code_graph(
        self,
        repo_path: str,
        commit_hash: str,
        nodes_count: int,
        graph_data: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None:
        """Insert or replace a code-graph snapshot."""
        async with self._lock:
            if not self.conn:
                raise RuntimeError("Hot store not initialized")
            import json

            self.conn.execute(
                """INSERT OR REPLACE INTO code_graphs
                       (repo_path, commit_hash, nodes_count, graph_data, metadata, ingested_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    repo_path,
                    commit_hash,
                    nodes_count,
                    json.dumps(graph_data),
                    json.dumps(metadata),
                    datetime.now(UTC),
                ],
            )

    async def get_code_graph(
        self,
        repo_path: str,
        commit_hash: str,
    ) -> dict[str, Any] | None:
        """Fetch a code-graph snapshot, or None if missing."""
        import json

        async with self._lock:
            if not self.conn:
                raise RuntimeError("Hot store not initialized")
            row = self.conn.execute(
                """SELECT repo_path, commit_hash, nodes_count, graph_data, metadata, ingested_at
                  FROM code_graphs
                  WHERE repo_path = ? AND commit_hash = ?""",
                [repo_path, commit_hash],
            ).fetchone()
            if not row or len(row) < 6:
                return None
            graph_data = json.loads(row[3]) if row[3] else {}
            metadata = json.loads(row[4]) if row[4] else {}
            return {
                "repo_path": row[0],
                "commit_hash": row[1],
                "nodes_count": row[2],
                "graph_data": graph_data,
                "metadata": metadata,
                "ingested_at": row[5],
            }

    async def list_code_graphs(
        self,
        repo_path: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List code-graph snapshots, optionally filtered by repo_path."""
        async with self._lock:
            if not self.conn:
                raise RuntimeError("Hot store not initialized")
            if repo_path is not None:
                rows = self.conn.execute(
                    """SELECT repo_path, commit_hash, nodes_count, ingested_at
                      FROM code_graphs WHERE repo_path = ?
                      ORDER BY ingested_at DESC LIMIT ?""",
                    [repo_path, limit],
                ).fetchall()
            else:
                rows = self.conn.execute(
                    """SELECT repo_path, commit_hash, nodes_count, ingested_at
                      FROM code_graphs
                      ORDER BY ingested_at DESC LIMIT ?""",
                    [limit],
                ).fetchall()
            return [
                {
                    "repo_path": r[0],
                    "commit_hash": r[1],
                    "nodes_count": r[2],
                    "ingested_at": r[3],
                }
                for r in rows
            ]
__all__ = ["HotStore"]

