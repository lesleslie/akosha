"""Akosha storage layer."""

from __future__ import annotations

import logging
import os
import typing as t
from pathlib import Path

from akosha.storage.aging import AgingService, MigrationStats
from akosha.storage.cold_store import ColdStore
from akosha.storage.hot_store import HotStore
from akosha.storage.models import (
    CodeGraphMetadata,
    ColdRecord,
    ConversationMetadata,
    HotRecord,
    IngestionStats,
    SystemMemoryUpload,
    WarmRecord,
)
from akosha.storage.path_resolver import (
    StoragePathResolver,
    get_config_dir,
    get_default_resolver,
    get_warm_store_path,
)
from akosha.storage.pgvector_warm_store import PgvectorWarmStore
from akosha.storage.warm_store import WarmStore

logger = logging.getLogger(__name__)

#: Machine-local override for the warm-tier backend selector.
#:
#: The committed ``settings/akosha.yaml`` is the LAST-RESORT default for
#: the warm tier. It is NOT read through the layered Oneiric loader: the
#: warm tier is selected by reading that file directly (see
#: :func:`resolve_warm_store_config`), because ``warm.backend`` and
#: ``warm.pg_url`` are also substrate-mapped and would otherwise be
#: stripped from the YAML before ``AkoshaConfig`` is built. An operator
#: therefore needs an env var to change the backend.
WARM_BACKEND_ENV = "AKOSHA__STORAGE__WARM__BACKEND"

#: Machine-local override for the warm-tier PostgreSQL DSN. See
#: :data:`WARM_BACKEND_ENV` for why the env var is the operator's lever.
WARM_PG_URL_ENV = "AKOSHA__STORAGE__WARM__PG_URL"

#: Repository root, resolved from this module's location
#: (``<repo>/akosha/storage/__init__.py`` -> ``parents[2]`` is the repo
#: root). Kept module-level so the resolution is done once.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# NOTE (2026-09-27 audit): ``PgvectorHotStore`` was the hot-tier pgvector
# backend. pgvector moved to the warm tier as part of the storage-allowlist
# cleanup (the hot tier's read-only-in-memory contract and pgvector's
# network-attached cost made pgvector a poor fit for hot). Use
# ``PgvectorWarmStore`` (one collection per shard, see
# ``akosha/storage/pgvector_warm_store.py``) for the warm-tier pgvector
# backend. The class itself is retained at
# ``akosha/storage/pgvector_hot_store.py`` only as a deprecation stub so
# external imports keep resolving; remove after 2026-Q4.

__all__ = [
    "WARM_BACKEND_ENV",
    "WARM_PG_URL_ENV",
    "AgingService",
    "CodeGraphMetadata",
    "ColdRecord",
    "ColdStore",
    "ConversationMetadata",
    "HotRecord",
    "HotStore",
    "IngestionStats",
    "MigrationStats",
    "PgvectorWarmStore",
    "StoragePathResolver",
    "SystemMemoryUpload",
    "WarmRecord",
    "WarmStore",
    "apply_warm_env_overrides",
    "create_hot_store",
    "create_warm_store",
    "effective_warm_backend",
    "get_config_dir",
    "get_default_resolver",
    "get_warm_store_path",
    "resolve_warm_store_config",
]


def create_hot_store(
    backend: str = "duckdb-memory",  # noqa: ARG001  # parallel API with create_warm_store; hot tier is DuckDB-only and the value is not consumed here
    embedding_dim: int | None = None,
    database_path: str = ":memory:",
) -> HotStore:
    """Create a hot store instance.

    The hot tier accepts only DuckDB-backed implementations (``duckdb-memory``
    for the zero-dep default, ``duckdb-ssd`` for on-disk). pgvector was a
    historical hot-tier backend and was removed 2026-09-27 — see the
    ``HotStorageConfig`` docstring for the migration path.

    Args:
        backend: Storage backend selector. ``"duckdb-memory"`` (default) or
            ``"duckdb-ssd"``. ``AkoshaApplication.start()`` reads this from
            the ``hot.backend`` block in ``settings/akosha.yaml`` and
            passes it through; tests should pass it explicitly.
        embedding_dim: Optional embedding vector dimension to thread
            through to the underlying store. ``None`` lets the store
            resolve via the embedding-dim contract (defaulting to 384).
            Pass an explicit ``int`` when the embedding backend's dim is
            already known (e.g. from ``AkoshaApplication.start``).
        database_path: DuckDB database path. Defaults to ``":memory:"``
            so tests and local dev never accidentally write to disk.

    Returns:
        ``HotStore`` (DuckDB-backed).
    """
    return HotStore(database_path=database_path, embedding_dim=embedding_dim)


def _coerce_db_path(path: os.PathLike[str] | None) -> Path:
    """Coerce ``PathLike``/``None`` to ``Path``.

    WarmStore.__init__'s ``database_path`` is typed ``Path`` (required).
    :func:`create_warm_store` accepts ``PathLike | None`` so callers can
    pass raw config-dict values; we narrow + default here.

    When ``path is None``, fall through to the StoragePathResolver's
    resolved warm-store path (``<base>/warm/warm.db``). The previous
    default of ``Path()`` (= cwd) silently fed DuckDB the current
    working directory, which surfaces as ``IO Error: ... Is a directory``
    the first time ``WarmStore.__init__`` calls ``duckdb.connect``. The
    failure cascaded to ``AgingService init failed`` -> ``aging_feed``
    cycles_total=0 / ingester_running=False -> ``/health`` 503 in
    standard mode. Fixed 2026-10-05 during restart verification.
    """
    if path is not None:
        return Path(path)
    # Local import to avoid a circular import: ``path_resolver`` is a
    # sibling module, but the canonical resolver is keyed off
    # ``akosha.config.config.warm.path`` which itself uses this module.
    from akosha.storage.path_resolver import get_warm_store_path

    return get_warm_store_path()


def apply_warm_env_overrides(backend: str, pg_url: str) -> tuple[str, str]:
    """Apply the operator's machine-local env overrides to a warm-tier selection.

    ``AKOSHA__STORAGE__WARM__BACKEND`` overrides the backend and
    ``AKOSHA__STORAGE__WARM__PG_URL`` overrides the DSN. Both are
    machine-local; the committed ``settings/akosha.yaml`` stays the
    last-resort default.

    Args:
        backend: The backend the caller resolved.
        pg_url: The DSN the caller resolved.

    Returns:
        The ``(backend, pg_url)`` pair after the env overrides.
    """
    env_backend = os.getenv(WARM_BACKEND_ENV, "").strip()
    env_pg_url = os.getenv(WARM_PG_URL_ENV, "").strip()
    if env_backend:
        backend = env_backend
    if env_pg_url:
        pg_url = env_pg_url
    return backend, pg_url


def resolve_warm_store_config() -> dict[str, t.Any]:
    """Resolve the warm-tier selection for every Akosha process.

    Precedence, highest wins:

    1. ``AKOSHA__STORAGE__WARM__BACKEND`` / ``AKOSHA__STORAGE__WARM__PG_URL``
       — the operator's machine-local override.
    2. the ``warm:`` block of the repo's ``settings/akosha.yaml`` — the
       last-resort default.

    The repo file is read directly rather than through the layered Oneiric
    loader because ``warm.backend`` and ``warm.pg_url`` are
    substrate-mapped (``akosha.config._SUBSTRATE_MAPPED_NESTED``) and are
    stripped from the YAML before ``AkoshaConfig`` is built. Reading the
    file directly is what makes the committed value meaningful again.

    Both warm-store construction sites do NOT call this yet. The MCP
    server's aging service (``akosha/mcp/server.py``) constructs its store
    as ``create_warm_store(backend="duckdb-ssd")`` and never consults the
    resolver; ``AkoshaApplication`` in ``akosha/main.py`` does consult it,
    but is never started in production. Wiring the MCP path through the
    resolver is Phase 2 of
    ``docs/plans/2026-11-17-unified-pgvector-warm-tier.md``.

    Two readers of the warm backend currently disagree, and this function
    is the one that is wrong for the canonical knob. ``akosha/config.py``
    documents ``ONEIRIC__SUBSTRATE__WARM__BACKEND`` as the environment
    mechanism for the warm tier and deliberately strips ``warm.backend``
    from the YAML so the substrate wins — but this function reads the repo
    file raw, so the substrate value never reaches it and the legacy
    ``AKOSHA__STORAGE__WARM__*`` variable is honoured instead. Reconciling
    the two is also Phase 2 work.

    Returns:
        A dict with ``backend``, ``pg_url`` and ``db_path``. ``db_path``
        is ``None`` when the DuckDB warm path is not configured.
    """
    defaults: dict[str, t.Any] = {
        "backend": "duckdb-ssd",
        "pg_url": "",
        "db_path": None,
    }

    settings_path = _REPO_ROOT / "settings" / "akosha.yaml"
    file_backend = ""
    file_pg_url = ""
    file_db_path: str | None = None
    if settings_path.exists():
        yaml_module: t.Any = None
        try:
            import yaml as yaml_module
        except ImportError:
            yaml_module = None
        if yaml_module is None:
            logger.warning(
                "akosha.warm_store.settings_unreadable: %s (PyYAML is not "
                "installed); falling back to the duckdb-ssd default",
                settings_path,
            )
        else:
            try:
                with settings_path.open() as handle:
                    cfg = yaml_module.safe_load(handle) or {}
                if not isinstance(cfg, dict):
                    raise ValueError("settings root is not a mapping")
                warm = cfg.get("warm") or {}
                if not isinstance(warm, dict):
                    warm = {}
                file_backend = str(warm.get("backend", "") or "")
                file_pg_url = str(warm.get("pg_url", "") or "")
                raw_db_path = warm.get("path")
                file_db_path = str(raw_db_path) if raw_db_path else None
            except (OSError, ValueError, yaml_module.YAMLError) as exc:
                # A malformed settings file must DEGRADE to the default,
                # not disable the tier. ``yaml.YAMLError`` is not a
                # ``ValueError``, so it has to be named explicitly or a
                # syntax error escapes this function and takes the whole
                # warm tier down with it.
                logger.warning(
                    "akosha.warm_store.settings_unreadable: %s (%s); "
                    "falling back to the duckdb-ssd default",
                    settings_path,
                    exc,
                )
    else:
        logger.warning(
            "akosha.warm_store.settings_missing: %s does not exist; "
            "falling back to the duckdb-ssd default",
            settings_path,
        )

    backend, pg_url = apply_warm_env_overrides(
        file_backend or defaults["backend"],
        file_pg_url or defaults["pg_url"],
    )

    db_path = Path(file_db_path) if file_db_path else None
    return {
        "backend": backend,
        "pg_url": pg_url,
        "db_path": db_path,
    }


def effective_warm_backend(warm_store: WarmStore | PgvectorWarmStore | None) -> str:
    """The backend selector the constructed warm store actually IS.

    ``create_warm_store`` fail-softs to the DuckDB store when pgvector
    is requested without a DSN, so the REQUESTED selector and the
    RUNNING one can differ. This reports the running one, as a selector
    an operator can compare against the configuration
    (``duckdb-ssd``, ``duckdb-memory``, ``pgvector``).

    A class name answers "what was constructed", not "which backend is
    running", and a log field named ``backend`` that holds a class name
    cannot be grepped against the configured value.

    Args:
        warm_store: The constructed store, or ``None`` if none was built.

    Returns:
        The effective backend selector.
    """
    if warm_store is None:
        return "disabled"
    if isinstance(warm_store, PgvectorWarmStore):
        return "pgvector"
    path = str(getattr(warm_store, "db_path", ""))
    if path == ":memory:":
        return "duckdb-memory"
    return "duckdb-ssd"


def create_warm_store(
    backend: str = "duckdb-ssd",
    pg_url: str = "",
    shard_count: int | None = None,
    database_path: os.PathLike[str] | None = None,
) -> WarmStore | PgvectorWarmStore:
    """Create a warm store instance based on the resolved backend.

    The arguments are honoured EXACTLY as passed. The machine-local
    ``AKOSHA__STORAGE__WARM_*`` overrides are applied by
    :func:`resolve_warm_store_config`, which is the resolution step —
    they are not re-applied here. A constructor that silently overrides
    its own arguments cannot be called with a deliberate choice: the
    MCP server's aging service needs the DuckDB warm store regardless of
    what the machine selected, because ``AgingService`` calls
    ``insert_batch()`` and reads ``.conn``, neither of which
    ``PgvectorWarmStore`` implements.

    Args:
        backend: Storage backend selector. One of ``"duckdb-ssd"`` (default),
            ``"duckdb-hdd"``, or ``"pgvector"``.
        pg_url: PostgreSQL connection string for the pgvector backend.
            Required when ``backend="pgvector"``. Falls back to
            ``AKOSHA__STORAGE__WARM__PG_URL`` when empty — the DSN is a
            credential-ish value, not a selector, so the fallback is safe
            for any caller that asked for the pgvector backend.
        shard_count: Number of shards for the pgvector warm store.
            ``None`` reads ``akosha.config.config.shard_count``. Ignored
            when ``backend != "pgvector"``.
        database_path: Filesystem path for the DuckDB warm store. ``None``
            resolves to ``akosha.config.config.warm.path``. Ignored when
            ``backend == "pgvector"``.

    Returns:
        ``PgvectorWarmStore`` when ``backend == "pgvector"`` and a pg_url
        is available. Otherwise ``WarmStore`` (DuckDB on-disk).
        When ``backend="pgvector"`` is requested but no pg_url is set,
        logs a WARNING and falls back to the on-disk ``WarmStore`` —
        fail-soft so a missing config never silently breaks ``start()``.
    """

    if backend == "pgvector":
        pg_url = pg_url or os.getenv(WARM_PG_URL_ENV, "")
        if not pg_url:
            logger.warning(
                "akosha.warm_store.pg_url_missing: "
                "backend=pgvector requested but no pg_url provided "
                "(pass via create_warm_store(pg_url=...) or "
                "%s); falling back to duckdb-ssd",
                WARM_PG_URL_ENV,
            )
            return WarmStore(database_path=_coerce_db_path(database_path))
        return PgvectorWarmStore(
            pg_url=pg_url,
            shard_count=shard_count,
        )

    return WarmStore(database_path=_coerce_db_path(database_path))
