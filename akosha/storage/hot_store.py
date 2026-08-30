"""Hot store: DuckDB in-memory for recent data."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import duckdb

from akosha.processing.embedding_dim import resolve_embedding_dim

if TYPE_CHECKING:
    from pathlib import Path

    from akosha.models import HotRecord

logger = logging.getLogger(__name__)


class HotStore:
    """Hot store with DuckDB in-memory storage.

    .. warning::

        **This is the development / test backend.** In-memory DuckDB
        loses all indexed rows on restart. **File-backed DuckDB is NOT
        recommended** for serverless or ephemeral-filesystem deployments:
        the filesystem does not survive container restarts, so the file
        is silently lost. For production deployments that require
        persistence, use :class:`akosha.storage.PgvectorHotStore` —
        pgvector-backed storage on Postgres survives restarts and is
        the recommended production default.

        Plan: docs/plans/2026-08-29-pgvector-default.md Phase 3.

    Embedding dim is configurable via the ``embedding_dim`` constructor
    argument; ``None`` resolves via
    :func:`akosha.processing.embedding_dim.resolve_embedding_dim` which
    defaults to the active embedding backend's dim at startup, falling
    back to 384 when no service is initialized. The resolved dim is
    baked into the DuckDB schema (``FLOAT[N]``) at ``initialize()`` time,
    so callers MUST set ``embedding_dim`` to the value their embedding
    service will produce *before* the first ``initialize()`` call.
    """

    def __init__(
        self,
        database_path: str | Path = ":memory:",
        embedding_dim: int | None = None,
    ) -> None:
        """Initialize hot store.

        Args:
            database_path: DuckDB database path (":memory:" for in-memory)
            embedding_dim: Embedding vector dimension. ``None`` resolves
                via :func:`resolve_embedding_dim` so the schema dim
                matches the active backend; pass an explicit ``int`` to
                pin it (e.g. in tests that never call
                ``get_embedding_service().initialize()``).
        """
        self.db_path = database_path
        self.conn: duckdb.DuckDBPyConnection | None = None
        self._lock = asyncio.Lock()
        # Schema dim is baked into the CREATE TABLE DDL at initialize()
        # time — capture it now so the SQL can interpolate
        # ``FLOAT[<resolved>]`` as a literal. ``int(self._embedding_dim)``
        # is guaranteed safe because ``resolve_embedding_dim`` always
        # returns an ``int``.
        self._embedding_dim: int = (
            int(embedding_dim) if embedding_dim is not None else int(resolve_embedding_dim())
        )

    async def initialize(self) -> None:
        """Initialize database schema."""
        async with self._lock:
            self.conn = duckdb.connect(str(self.db_path))

            # Create conversations table with HNSW index support.
            # ``embedding FLOAT[N]`` is interpolated at __init__ time so
            # the schema dim matches the active backend; this MUST match
            # the dim the embedding service produces or insert() raises.
            self.conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS conversations (
                    system_id VARCHAR,
                    conversation_id VARCHAR PRIMARY KEY,
                    content TEXT,
                    embedding FLOAT[{int(self._embedding_dim)}],
                    timestamp TIMESTAMP,
                    metadata JSON,
                    content_hash VARCHAR,
                    uploaded_at TIMESTAMP DEFAULT NOW()
                )
            """
            )

            # Create HNSW index for vector search
            try:
                self.conn.execute("""
                    CREATE INDEX IF NOT EXISTS embedding_hnsw_index
                    ON conversations USING HNSW (embedding)
                    WITH (m = 16, ef_construction = 200)
                """)
            except Exception as e:
                logger.warning(f"HNSW index creation failed: {e}")

            # Create indexes for filtered queries (performance optimization)
            try:
                # Index on system_id for fast filtering
                self.conn.execute("""
                    CREATE INDEX IF NOT EXISTS system_id_index
                    ON conversations (system_id)
                """)
                logger.info("Created system_id index")
            except Exception as e:
                logger.warning(f"system_id index creation failed: {e}")

            try:
                # Index on timestamp for aging queries
                self.conn.execute("""
                    CREATE INDEX IF NOT EXISTS timestamp_index
                    ON conversations (timestamp)
                """)
                logger.info("Created timestamp index")
            except Exception as e:
                logger.warning(f"timestamp index creation failed: {e}")

            try:
                # Composite index for system_id + timestamp (common query pattern)
                self.conn.execute("""
                    CREATE INDEX IF NOT EXISTS system_timestamp_index
                    ON conversations (system_id, timestamp)
                """)
                logger.info("Created composite system_id+timestamp index")
            except Exception as e:
                logger.warning(f"Composite index creation failed: {e}")

            # Create the code_graphs table in the same initialization pass so
            # ``search_code_patterns`` / ``find_function_usage`` don't fail with
            # ``Catalog Error: Table with name code_graphs does not exist!``
            # when the tool runs against a freshly-initialised store. The
            # SQL is run inline (without re-acquiring ``_lock``) — we are
            # already inside the lock here, and ``asyncio.Lock`` is NOT
            # reentrant, so calling ``initialize_code_graphs_table`` would
            # deadlock. ``_create_code_graphs_schema`` is the lock-free
            # implementation shared with the public method.
            self._create_code_graphs_schema(self.conn)

            logger.info("Hot store initialized")

    async def insert(self, record: HotRecord) -> None:
        """Insert conversation into hot store.

        Args:
            record: Hot record to insert

        Raises:
            ValueError: If ``len(record.embedding) != self._embedding_dim``.
                This is the fail-loud contract — a dim mismatch indicates
                the embedding backend changed (or was misconfigured)
                since ``__init__`` baked the schema. Catching this at
                the subscriber layer keeps the fail-soft behaviour for
                per-row failures.
        """
        # Fail loud BEFORE acquiring the lock — the check is cheap and
        # the value error is the signal callers (e.g.
        # websocket_invocations_subscriber) hook into for fail-soft
        # logging.
        actual_dim = len(record.embedding)
        if actual_dim != self._embedding_dim:
            logger.warning(
                "akosha.hot_store.dim_mismatch",
                extra={
                    "expected": self._embedding_dim,
                    "actual": actual_dim,
                    "conversation_id": record.conversation_id,
                },
            )
            raise ValueError(
                f"HotStore.insert: embedding dim mismatch "
                f"(expected {self._embedding_dim}, got {actual_dim})"
            )

        async with self._lock:
            if not self.conn:
                raise RuntimeError("Hot store not initialized")

            self.conn.execute(
                """
                INSERT INTO conversations
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                [
                    record.system_id,
                    record.conversation_id,
                    record.content,
                    record.embedding,
                    record.timestamp,
                    record.metadata,
                    self._compute_content_hash(record.content),
                    datetime.now(UTC),
                ],
            )

    async def search_similar(
        self,
        query_embedding: list[float],
        system_id: str | None = None,
        limit: int = 10,
        threshold: float = 0.7,
    ) -> list[dict[str, Any]]:
        """Search for similar conversations using vector similarity.

        Args:
            query_embedding: Query vector (FLOAT[N], dim matches schema)
            system_id: Optional system filter
            limit: Maximum results to return
            threshold: Minimum similarity score (0-1)

        Returns:
            List of similar conversations with metadata

        Raises:
            ValueError: If ``len(query_embedding) != self._embedding_dim``.
                Fails fast before hitting DuckDB so callers see a clear
                dim-mismatch signal instead of an opaque CAST error.
        """
        query_dim = len(query_embedding)
        if query_dim != self._embedding_dim:
            logger.warning(
                "akosha.hot_store.dim_mismatch",
                extra={
                    "expected": self._embedding_dim,
                    "actual": query_dim,
                    "operation": "search_similar",
                },
            )
            raise ValueError(
                f"HotStore.search_similar: query dim mismatch "
                f"(expected {self._embedding_dim}, got {query_dim})"
            )

        async with self._lock:
            if not self.conn:
                raise RuntimeError("Hot store not initialized")

            # Set HNSW search parameters
            import contextlib

            with contextlib.suppress(Exception):
                self.conn.execute("SET hnsw_ef_search = 100")

            # Build query with parameterized WHERE clause (SQL injection prevention)
            # Note: We use separate queries for each case to ensure proper parameterization
            # Detect zero vector — cosine similarity is undefined for zero vectors,
            # so use timestamp ordering instead (HNSW index not applicable here).
            is_zero_vector = all(abs(x) < 1e-10 for x in query_embedding)

            if is_zero_vector and system_id:
                # Zero vector: order by timestamp descending (most recent first)
                query = """
                    SELECT
                        system_id,
                        conversation_id,
                        content,
                        timestamp,
                        metadata,
                        NULL as similarity
                    FROM conversations
                    WHERE system_id = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """
                results = self.conn.execute(query, [system_id, limit]).fetchall()
            elif is_zero_vector:
                # Zero vector, no system_id filter: order by timestamp
                query = """
                    SELECT
                        system_id,
                        conversation_id,
                        content,
                        timestamp,
                        metadata,
                        NULL as similarity
                    FROM conversations
                    ORDER BY timestamp DESC
                    LIMIT ?
                """
                results = self.conn.execute(query, [limit]).fetchall()
            elif system_id:
                query = f"""
                    SELECT
                        system_id,
                        conversation_id,
                        content,
                        timestamp,
                        metadata,
                        array_cosine_similarity(embedding, ?::FLOAT[{int(self._embedding_dim)}]) as similarity
                    FROM conversations
                    WHERE system_id = ?
                    ORDER BY similarity DESC
                    LIMIT ?
                """
                results = self.conn.execute(query, [query_embedding, system_id, limit]).fetchall()
            else:
                query = f"""
                    SELECT
                        system_id,
                        conversation_id,
                        content,
                        timestamp,
                        metadata,
                        array_cosine_similarity(embedding, ?::FLOAT[{int(self._embedding_dim)}]) as similarity
                    FROM conversations
                    ORDER BY similarity DESC
                    LIMIT ?
                """
                results = self.conn.execute(query, [query_embedding, limit]).fetchall()

            # Filter by threshold (NULL similarity from zero-vector path always passes)
            return [
                {
                    "system_id": r[0],
                    "conversation_id": r[1],
                    "content": r[2],
                    "timestamp": r[3],
                    "metadata": r[4],
                    "similarity": r[5],
                }
                for r in results
                if r[5] is None or r[5] >= threshold
            ]

    @staticmethod
    def _compute_content_hash(content: str) -> str:
        """Compute SHA-256 hash of content."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    async def close(self) -> None:
        """Close database connection."""
        async with self._lock:
            if self.conn:
                self.conn.close()
                logger.info("Hot store closed")

    async def query_traces(
        self,
        system_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        task_class: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query traces using SQL WHERE on metadata JSON attributes.

        This method pushes attribute filters (task_class, time range) into the SQL
        WHERE clause rather than fetching all traces and filtering in Python.
        The HNSW index is NOT used for this query.

        Args:
            system_id: Optional system_id filter
            start_time: ISO8601 start time (inclusive)
            end_time: ISO8601 end time (inclusive)
            task_class: Filter traces where metadata.attributes.task_class matches
            limit: Maximum results to return

        Returns:
            List of trace records with conversation_id, content, timestamp, metadata
        """
        async with self._lock:
            if not self.conn:
                raise RuntimeError("Hot store not initialized")

            # Build parameterized WHERE clause
            conditions: list[str] = []
            params: list[Any] = []

            if system_id:
                conditions.append("system_id = ?")
                params.append(system_id)

            if start_time:
                conditions.append("timestamp >= ?")
                params.append(start_time)

            if end_time:
                conditions.append("timestamp <= ?")
                params.append(end_time)

            if task_class:
                # Filter on metadata JSON: attributes.task_class or top-level task_class
                conditions.append(
                    "(metadata->>'task_class' = ? OR metadata->'attributes'->>'task_class' = ?)"
                )
                params.extend([task_class, task_class])

            where_clause = " AND ".join(conditions) if conditions else "1=1"

            query = f"""
                SELECT
                    system_id,
                    conversation_id,
                    content,
                    timestamp,
                    metadata
                FROM conversations
                WHERE {where_clause}
                ORDER BY timestamp DESC
                LIMIT ?
            """
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
            logger.info("Code graphs table initialized")

    def _create_code_graphs_schema(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Create the code_graphs table + indexes. Lock-free helper.

        Shared by ``initialize`` (which already holds ``_lock``) and the
        public ``initialize_code_graphs_table`` (which acquires it).
        ``asyncio.Lock`` is NOT reentrant, so this method MUST NOT acquire
        the lock — callers are responsible for serialisation.
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS code_graphs (
                repo_path VARCHAR,
                commit_hash VARCHAR,
                nodes_count INTEGER,
                graph_data JSON,
                metadata JSON,
                ingested_at TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (repo_path, commit_hash)
            )
        """)

        # Create indexes for common queries
        try:
            conn.execute("""
                CREATE INDEX IF NOT EXISTS code_graphs_repo_index
                ON code_graphs (repo_path)
            """)
            logger.info("Created code_graphs repo_path index")
        except Exception as e:
            logger.warning(f"code_graphs repo_path index creation failed: {e}")

        try:
            conn.execute("""
                CREATE INDEX IF NOT EXISTS code_graphs_nodes_index
                ON code_graphs (nodes_count DESC)
            """)
            logger.info("Created code_graphs nodes_count index")
        except Exception as e:
            logger.warning(f"code_graphs nodes_count index creation failed: {e}")

        try:
            conn.execute("""
                CREATE INDEX IF NOT EXISTS code_graphs_ingested_index
                ON code_graphs (ingested_at DESC)
            """)
            logger.info("Created code_graphs ingested_at index")
        except Exception as e:
            logger.warning(f"code_graphs ingested_at index creation failed: {e}")

    async def store_code_graph(
        self,
        repo_path: str,
        commit_hash: str,
        nodes_count: int,
        graph_data: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None:
        """Store a code graph in the hot store.

        Args:
            repo_path: Path to the repository
            commit_hash: Git commit hash
            nodes_count: Number of nodes in the code graph
            graph_data: Complete code graph data (nodes, edges, etc.)
            metadata: Optional metadata dictionary
        """
        async with self._lock:
            if not self.conn:
                raise RuntimeError("Hot store not initialized")

            import json

            self.conn.execute(
                """
                INSERT OR REPLACE INTO code_graphs
                (repo_path, commit_hash, nodes_count, graph_data, metadata, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
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
        """Get a code graph by repo path and commit hash.

        Args:
            repo_path: Path to the repository
            commit_hash: Git commit hash

        Returns:
            Code graph data or None if not found
        """
        async with self._lock:
            if not self.conn:
                raise RuntimeError("Hot store not initialized")

            result = self.conn.execute(
                """
                SELECT repo_path, commit_hash, nodes_count, graph_data, metadata, ingested_at
                FROM code_graphs
                WHERE repo_path = ? AND commit_hash = ?
                """,
                [repo_path, commit_hash],
            ).fetchone()

            if not result or len(result) < 6:
                return None

            graph_data = json.loads(result[3]) if result[3] else {}  # type: ignore[unreachable]
            metadata = json.loads(result[4]) if result[4] else {}  # type: ignore[unreachable]

            return {
                "repo_path": result[0],
                "commit_hash": result[1],
                "nodes_count": result[2],
                "graph_data": graph_data,
                "metadata": metadata,
                "ingested_at": result[5],
            }

    async def list_code_graphs(
        self,
        repo_path: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List code graphs with optional filtering.

        Args:
            repo_path: Optional repo path filter
            limit: Maximum number of results

        Returns:
            List of code graph summaries
        """
        async with self._lock:
            if not self.conn:
                raise RuntimeError("Hot store not initialized")

            if repo_path:
                results = self.conn.execute(
                    """
                    SELECT repo_path, commit_hash, nodes_count, ingested_at
                    FROM code_graphs
                    WHERE repo_path = ?
                    ORDER BY ingested_at DESC
                    LIMIT ?
                    """,
                    [repo_path, limit],
                ).fetchall()
            else:
                results = self.conn.execute(
                    """
                    SELECT repo_path, commit_hash, nodes_count, ingested_at
                    FROM code_graphs
                    ORDER BY ingested_at DESC
                    LIMIT ?
                    """,
                    [limit],
                ).fetchall()

            return [
                {
                    "repo_path": r[0],
                    "commit_hash": r[1],
                    "nodes_count": r[2],
                    "ingested_at": r[3],
                }
                for r in results
            ]
