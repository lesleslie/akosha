"""Akosha configuration management using Oneiric MCPBaseSettings pattern.

This module provides centralized configuration management following the
Oneiric pattern with layered configuration loading:
- Defaults in field definitions
- settings/akosha.yaml (committed)
- settings/local.yaml (gitignored)
- Environment variables AKOSHA_*
- **Substrate fields via ``ONEIRIC__SUBSTRATE__*``** (Plan 2 Phase E2).
  Cache/warm/cold/auth/pool settings adopt the shared Bodai substrate
  (see ``oneiric.core.substrate_settings.SubstrateSettings``). Field
  names use the canonical double-underscore convention
  (``cache__max_entries``, ``warm__pg_url``, ``auth__enabled``, etc.)
  matching the env-var form ``ONEIRIC__SUBSTRATE__CACHE__MAX_ENTRIES``.

Configuration loading order (later overrides earlier):
1. Default values in field definitions (substrate defaults for substrate
   fields)
2. settings/akosha.yaml (committed, for production defaults)
3. settings/local.yaml (gitignored, for development)
4. Environment variables ``ONEIRIC__SUBSTRATE__*`` for substrate fields
5. Environment variables ``AKOSHA_{FIELD}`` for Akosha-specific fields

For nested storage configs (HotStorageConfig, etc.), environment variables
use AKOSHA_{FIELD}__{SUBFIELD} format. Substrate fields use
``ONEIRIC__SUBSTRATE__<DOUBLE_UNDERSCORE_FIELD>``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, model_validator

if TYPE_CHECKING:
    # Type-checker only: import the real base class so ``class AkoshaConfig``
    # sees a single concrete base (no MRO ambiguity).
    from oneiric.core.config import OneiricMCPConfig
else:
    try:
        from oneiric.core.config import OneiricMCPConfig  # type: ignore[assignment]
    except ImportError:
        # Fallback if oneiric is not installed. Defining an empty subclass of
        # BaseModel keeps ``OneiricMCPConfig`` a single class for the type checker,
        # instead of a ``OneiricMCPConfig | BaseModel`` union which would break
        # ``class AkoshaConfig(OneiricMCPConfig)``. The ``TYPE_CHECKING`` branch
        # above shadows this fallback for the type checker.
        class OneiricMCPConfig(BaseModel):  # type: ignore[no-redef]
            """Fallback base class when ``oneiric`` is unavailable."""


from akosha.storage.path_resolver import StoragePathResolver

logger = logging.getLogger(__name__)

#: Single source of truth for the Akosha network port. The Bodai ecosystem
#: reserves 8682 for the Akosha seer; the previous 3002 default predates the
#: port convention. Override via AKOSHA_MCP_PORT / MCP_PORT env vars or CLI
#: flag, never by editing this constant.
DEFAULT_MCP_PORT: int = 8682


def _substrate_field(field_name: str) -> Any:
    """Lazy accessor for ``OneiricSettings.substrate.<field_name>``.

    Plan 2 Phase E2 atomic per-file adoption. Reads fresh at instance
    creation time so env vars set between module import and config
    construction are honored (avoids module-load snapshot drift).

    ``load_settings`` is invoked lazily so the oneiric import is not
    forced at module-load (preserves the existing fallback path when
    oneiric is unavailable — the ``OneiricMCPConfig`` shim above).
    """
    from oneiric.core.config import load_settings as _oneiric_load

    return getattr(_oneiric_load().substrate, field_name)


#: Plan 2 Phase E2 — the canonical list of AkoshaConfig fields that have
#: been adopted from ``OneiricSettings.substrate``. The ``get_config``
#: merger strips these from YAML-loaded data so per-class
#: ``default_factory`` lambdas (which read from substrate) take effect.
_SUBSTRATE_MAPPED_TOP_LEVEL: frozenset[str] = frozenset(
    {
        "auth_enabled",
        "auth_loopback_trusted",
        "pool_workers_per_instance",
    },
)
_SUBSTRATE_MAPPED_NESTED: dict[str, frozenset[str]] = {
    "cache": frozenset({"backend", "max_entries", "default_ttl_seconds"}),
    "warm": frozenset({"backend", "pg_url", "pg_pool_size"}),
    "cold": frozenset({"backend", "bucket", "prefix", "compression"}),
}


def _strip_substrate_mapped(data: dict[str, Any]) -> dict[str, Any]:
    """Remove substrate-mapped fields from a config-init dict.

    The substrate settings object is the canonical source of truth for
    these fields (Plan 2 Phase E2). Without this strip, YAML-loaded
    values (e.g. ``warm.pg_url: ""`` from ``settings/akosha.yaml``)
    would silently win over ``ONEIRIC__SUBSTRATE__WARM__PG_URL``
    env-var overrides — breaking the atomic per-file rule that says
    substrate wins.

    The strip is shallow + per-nested-config: top-level substrate fields
    are popped from the dict, and nested substrate fields are popped
    from their respective nested dicts.
    """
    out: dict[str, Any] = {k: v for k, v in data.items() if k not in _SUBSTRATE_MAPPED_TOP_LEVEL}
    for prefix, fields in _SUBSTRATE_MAPPED_NESTED.items():
        nested = out.get(prefix)
        if isinstance(nested, dict):
            out[prefix] = {k: v for k, v in nested.items() if k not in fields}
    return out


class HotStorageConfig(BaseModel):
    """Hot storage configuration.

    Attributes:
        backend: Storage backend type (duckdb-memory or duckdb-ssd)
        path: Path to hot storage (usually ":memory:" for in-memory)
        write_ahead_log: Enable write-ahead logging
        wal_path: Path to WAL directory

    Configuration can be set via:
    1. settings/akosha.yaml under hot
    2. settings/local.yaml
    3. Environment variable: AKOSHA__STORAGE__HOT__BACKEND

    NOTE (2026-09-27 audit): ``pgvector`` was a valid hot backend; it was
    removed because pgvector is now a warm-tier-only backend. The DuckDB
    in-memory store is the only hot-tier implementation; the pg_url field
    and AKOSHA__STORAGE__HOT__PG_URL env var were removed in the same pass.
    Migrate any ``backend=pgvector`` hot-tier configs to warm-tier:
    ``AKOSHA__STORAGE__WARM__BACKEND=pgvector`` and
    ``AKOSHA__STORAGE__WARM__PG_URL=<your_dsn>``.
    """

    backend: str = Field(
        default="duckdb-memory",
        description=(
            "Storage backend: 'duckdb-memory' or 'duckdb-ssd'. "
            "Set via AKOSHA__STORAGE__HOT__BACKEND"
        ),
    )
    path: str = Field(
        default=":memory:",
        description="DuckDB database path for OTel ingester (':memory:' for in-memory)",
    )
    write_ahead_log: bool = Field(default=True)
    wal_path: Path | None = None  # Will be resolved by model_validator

    def __init__(self, **data: Any) -> None:
        # Honor AKOSHA__STORAGE__HOT__BACKEND
        _env_backend = os.getenv("AKOSHA__STORAGE__HOT__BACKEND", "")
        if _env_backend and "backend" not in data:
            data["backend"] = _env_backend
        super().__init__(**data)

    @model_validator(mode="after")
    def resolve_paths(self) -> HotStorageConfig:
        """Resolve WAL path using StoragePathResolver."""
        if self.wal_path is None:
            resolver = StoragePathResolver()
            self.wal_path = resolver.get_hot_store_wal_path()
        return self


class WarmStorageConfig(BaseModel):
    """Warm storage configuration.

    Attributes:
        backend: Storage backend. Adopted from substrate (``warm__backend``)
            per Plan 2 Phase E2 — substrate defaults to ``pgvector``.
        path: Path to warm storage directory
        pg_url: PostgreSQL connection string for pgvector backend.
            Adopted from substrate (``warm__pg_url``).
        num_partitions: Number of shards for distributed queries
        pg_pool_size: SQLAlchemy connection pool size. Adopted from substrate
            (``warm__pg_pool_size``).

    Configuration can be set via:
    1. settings/akosha.yaml under warm
    2. settings/local.yaml
    3. Environment variables: ONEIRIC__SUBSTRATE__WARM__BACKEND,
       ONEIRIC__SUBSTRATE__WARM__PG_URL, ONEIRIC__SUBSTRATE__WARM__PG_POOL_SIZE.
       (Legacy AKOSHA__STORAGE__WARM__* still honored via the ``__init__``
       fallback for one release.)
    """

    backend: str = Field(
        default_factory=lambda: _substrate_field("warm__backend"),
        description=(
            "Storage backend. Adopted from substrate; default 'pgvector'. "
            "Override via ONEIRIC__SUBSTRATE__WARM__BACKEND."
        ),
    )
    path: Path | None = None  # Will be resolved by model_validator
    pg_url: str = Field(
        default_factory=lambda: _substrate_field("warm__pg_url"),
        description=(
            "PostgreSQL connection string for pgvector-backed warm storage. "
            "Required when backend='pgvector'. "
            "Override via ONEIRIC__SUBSTRATE__WARM__PG_URL."
        ),
    )
    pg_pool_size: int = Field(
        default_factory=lambda: _substrate_field("warm__pg_pool_size"),
        description="SQLAlchemy connection pool size (from substrate).",
    )
    num_partitions: int = 256

    def __init__(self, **data: Any) -> None:
        # Legacy ``AKOSHA__STORAGE__WARM__*`` env vars remain honored as a
        # one-release fallback. New code should use ONEIRIC__SUBSTRATE__*
        # (Plan 2 Phase E2).
        _env_backend = os.getenv("AKOSHA__STORAGE__WARM__BACKEND", "")
        _env_pg_url = os.getenv("AKOSHA__STORAGE__WARM__PG_URL", "")
        if _env_backend and "backend" not in data:
            data["backend"] = _env_backend
        if _env_pg_url and "pg_url" not in data:
            data["pg_url"] = _env_pg_url
        super().__init__(**data)

    @model_validator(mode="after")
    def resolve_paths(self) -> WarmStorageConfig:
        """Resolve warm storage path using StoragePathResolver."""
        if self.path is None:
            resolver = StoragePathResolver()
            self.path = resolver.get_warm_store_path()
        return self


class ColdStorageConfig(BaseModel):
    """Cold storage configuration.

    Attributes:
        backend: Storage backend type. Adopted from substrate
            (``cold__backend``) per Plan 2 Phase E2 — substrate defaults to
            ``r2``.
        bucket: Bucket name for cloud storage. Adopted from substrate
            (``cold__bucket``).
        prefix: Object-key prefix. Adopted from substrate
            (``cold__prefix``).
        compression: Compression codec at write time. Adopted from substrate
            (``cold__compression``).
        format: File format (parquet)
        region: Cloud region (S3/R2: 'auto' for R2)
        endpoint_url: Override the API endpoint (fake-gcs-server in dev, R2 in prod)
        project: GCP project ID (gcs backend only; ignored on s3)
        anonymous_credentials: Use anonymous GCS credentials (fake-gcs-server only)
        access_key_id: S3 access key ID (Cloudflare R2 etc.)
        secret_access_key: S3 secret access key

    Configuration can be set via:
    1. settings/akosha.yaml under cold
    2. settings/local.yaml (gitignored)
    3. Environment variables (substrate, primary):
       ONEIRIC__SUBSTRATE__COLD__BACKEND, ONEIRIC__SUBSTRATE__COLD__BUCKET,
       ONEIRIC__SUBSTRATE__COLD__PREFIX, ONEIRIC__SUBSTRATE__COLD__COMPRESSION
    4. Environment variables (legacy, one-release fallback):
       AKOSHA__STORAGE__COLD__ENDPOINT_URL,
       AKOSHA__STORAGE__COLD__PROJECT,
       AKOSHA__STORAGE__COLD__ANONYMOUS_CREDENTIALS,
       AKOSHA__STORAGE__COLD__ACCESS_KEY_ID,
       AKOSHA__STORAGE__COLD__SECRET_ACCESS_KEY,
       AKOSHA__STORAGE__COLD__REGION,
       AKOSHA_COLD_BACKEND, AKOSHA_COLD_BUCKET, AKOSHA_COLD_ENDPOINT,
       AKOSHA_COLD_REGION.
    """

    backend: str = Field(default_factory=lambda: _substrate_field("cold__backend"))
    bucket: str = Field(default_factory=lambda: _substrate_field("cold__bucket"))
    prefix: str = Field(default_factory=lambda: _substrate_field("cold__prefix"))
    compression: str = Field(default_factory=lambda: _substrate_field("cold__compression"))
    format: str = "parquet"
    region: str | None = Field(default_factory=lambda: os.getenv("AKOSHA_COLD_REGION", "us-west-2"))
    endpoint_url: str | None = None
    project: str | None = None
    anonymous_credentials: bool = False
    access_key_id: str | None = None
    secret_access_key: str | None = None

    def __init__(self, **data: Any) -> None:
        # Honor nested env vars (preferred). Only fill fields not already
        # explicitly passed via ``data``.
        _env_endpoint_url = os.getenv("AKOSHA__STORAGE__COLD__ENDPOINT_URL", "")
        _env_project = os.getenv("AKOSHA__STORAGE__COLD__PROJECT", "")
        _env_anon = os.getenv("AKOSHA__STORAGE__COLD__ANONYMOUS_CREDENTIALS", "")
        _env_access_key = os.getenv("AKOSHA__STORAGE__COLD__ACCESS_KEY_ID", "")
        _env_secret_key = os.getenv("AKOSHA__STORAGE__COLD__SECRET_ACCESS_KEY", "")
        _env_region = os.getenv("AKOSHA__STORAGE__COLD__REGION", "")
        if _env_endpoint_url and "endpoint_url" not in data:
            data["endpoint_url"] = _env_endpoint_url
        if _env_project and "project" not in data:
            data["project"] = _env_project
        if _env_anon and "anonymous_credentials" not in data:
            data["anonymous_credentials"] = _env_anon.lower() in ("true", "1", "yes")
        if _env_access_key and "access_key_id" not in data:
            data["access_key_id"] = _env_access_key
        if _env_secret_key and "secret_access_key" not in data:
            data["secret_access_key"] = _env_secret_key
        if _env_region and "region" not in data:
            data["region"] = _env_region

        # Flat legacy env vars (fallback only when nested form unset).
        # Matches the AKOSHA_COLD_* convention used in QUICKSTART.md and
        # older settings/local.yaml files.
        if "endpoint_url" not in data:
            _legacy_endpoint = os.getenv("AKOSHA_COLD_ENDPOINT", "")
            if _legacy_endpoint:
                data["endpoint_url"] = _legacy_endpoint
        if "region" not in data:
            _legacy_region = os.getenv("AKOSHA_COLD_REGION", "")
            if _legacy_region:
                data["region"] = _legacy_region

        super().__init__(**data)


class CacheConfig(BaseModel):
    """Cache configuration.

    Attributes:
        backend: Cache backend (redis, memory, multitier, persistent_kv).
            Adopted from substrate (``cache__backend``) per Plan 2 Phase E2.
        max_entries: Maximum entries held by the L1 cache.
            Adopted from substrate (``cache__max_entries``).
        default_ttl_seconds: Default TTL applied to cache entries.
            Adopted from substrate (``cache__default_ttl_seconds``).
        host: Redis hostname
        port: Redis port
        db: Redis database number
        local_ttl_seconds: TTL for in-memory cache
        redis_ttl_seconds: TTL for Redis cache
    """

    backend: str = Field(default_factory=lambda: _substrate_field("cache__backend"))
    max_entries: int | None = Field(
        default_factory=lambda: _substrate_field("cache__max_entries"),
    )
    default_ttl_seconds: float | None = Field(
        default_factory=lambda: _substrate_field("cache__default_ttl_seconds"),
    )
    host: str = Field(default_factory=lambda: os.getenv("AKOSHA_REDIS_HOST", "localhost"))
    port: int = Field(default_factory=lambda: int(os.getenv("AKOSHA_REDIS_PORT", "6379")))
    db: int = 0
    local_ttl_seconds: int = 60
    redis_ttl_seconds: int = 3600


class EventBridgeConfig(BaseModel):
    """EventBridge publisher configuration.

    Attributes:
        enabled: Master toggle for the Akosha-side EventBridge publisher.
        default_topic: Default topic suffix when callers omit one.
        default_source: Producer identifier (``akosha``).
        endpoint: Optional external EventBridge ingestion URL.
        max_concurrency: Max concurrent publishes (reserved).
        timeout_seconds: Per-publish HTTP timeout (reserved).
        dry_run: When True, envelopes are logged but not transmitted.

    Configuration can be set via:
    1. settings/akosha.yaml under eventbridge
    2. settings/local.yaml
    3. Environment variables: AKOSHA_EVENTBRIDGE_ENABLED,
       AKOSHA_EVENTBRIDGE_DRY_RUN, AKOSHA_EVENTBRIDGE_ENDPOINT.
    """

    enabled: bool = Field(
        default=False,
        description=(
            "Master toggle for the EventBridge publisher. Set to True to "
            "begin emitting analytics events to the Bodai EventBridge. "
            "Set via AKOSHA_EVENTBRIDGE_ENABLED."
        ),
    )
    default_topic: str = Field(default="analytics.default")
    default_source: str = Field(default="akosha")
    endpoint: str | None = Field(
        default=None,
        description=(
            "Optional external EventBridge ingestion URL. Reserved for "
            "future AWS PutEvents integration; not consumed by the "
            "current Oneiric dispatcher transport."
        ),
    )
    max_concurrency: int = Field(default=5, ge=1, le=100)
    timeout_seconds: float = Field(default=5.0, gt=0.0)
    dry_run: bool = Field(
        default=True,
        description=(
            "When True, envelopes are logged but not transmitted. Operators "
            "must explicitly set dry_run=False (or AKOSHA_EVENTBRIDGE_DRY_RUN=false) "
            "to actually emit events."
        ),
    )

    def __init__(self, **data: Any) -> None:
        # MCPBaseSettings does not auto-bind nested env vars; mirror the
        # HotStorageConfig pattern (above). Only fills fields not already
        # explicitly passed via ``data``.
        _env_enabled = os.getenv("AKOSHA_EVENTBRIDGE_ENABLED", "")
        _env_dry_run = os.getenv("AKOSHA_EVENTBRIDGE_DRY_RUN", "")
        _env_endpoint = os.getenv("AKOSHA_EVENTBRIDGE_ENDPOINT", "")
        if _env_enabled and "enabled" not in data:
            data["enabled"] = _env_enabled.lower() in ("true", "1", "yes")
        if _env_dry_run and "dry_run" not in data:
            data["dry_run"] = _env_dry_run.lower() in ("true", "1", "yes")
        if _env_endpoint and "endpoint" not in data:
            data["endpoint"] = _env_endpoint
        super().__init__(**data)


class SpansSubstrateConfig(BaseModel):
    """Settings for the OTel span substrate and its receiver.

    v7 §6.4. The port defaults are the canonical OTLP ports, which the
    substrate OWNS per docs/decisions/2026-10-21-otel-substrate-port-ownership.md
    — Grafana Alloy moves to :14317/:14318 at cutover. These exist as real
    fields rather than a defensive ``settings.get(...)`` lookup because
    ``get_config`` strips any key not declared on ``AkoshaConfig``: an
    undeclared key is dropped silently, so the operator sets it, the value
    vanishes, and the receiver binds the canonical ports anyway with nothing
    in the logs to say why.
    """

    receiver_grpc_port: int = Field(
        default=4317,
        description="OTLP/gRPC listen port. Owned by the substrate, not Alloy.",
    )
    receiver_http_port: int = Field(
        default=4318,
        description="OTLP/HTTP listen port. Owned by the substrate, not Alloy.",
    )
    retention_days: int = Field(
        default=30,
        description="Days a span is retained before the §8.1 sweep removes it.",
    )
    retention_sweep_interval_hours: int = Field(
        default=6,
        description="Hours between retention sweeps. PR-A3 ships the task; PR-A4 the body.",
    )


class AkoshaConfig(OneiricMCPConfig):  # type: ignore[reportUntypedBaseClass]
    """Main Akosha configuration using Oneiric MCPBaseSettings pattern.

    Configuration loading order (later overrides earlier):
    1. Default values in field definitions
    2. settings/akosha.yaml (committed, for production defaults)
    3. settings/local.yaml (gitignored, for development)
    4. Environment variables AKOSHA_{FIELD}

    Attributes:
        server_name: Display name for the MCP server
        mode: Operational mode (lite, standard)
        hot: Hot storage configuration
        warm: Warm storage configuration
        cold: Cold storage configuration
        cache: Cache configuration
        api_port: API server port
        mcp_port: MCP server port
        debug: Enable debug mode
        ingestion_workers: Number of ingestion workers
        max_concurrent_ingests: Maximum concurrent ingestions
        shard_count: Number of shards for distributed queries
    """

    server_name: str = Field(
        default="Akosha Seer",
        description="Display name for Akosha server",
    )
    server_description: str = Field(
        default="Cross-system intelligence and embeddings for the Bodai ecosystem",
        description="Brief description of server functionality",
    )

    # Mode
    mode: str = Field(default_factory=lambda: os.getenv("AKOSHA_MODE", "lite"))

    # Storage
    hot: HotStorageConfig = Field(default_factory=HotStorageConfig)
    warm: WarmStorageConfig = Field(default_factory=WarmStorageConfig)
    cold: ColdStorageConfig = Field(default_factory=ColdStorageConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    eventbridge: EventBridgeConfig = Field(default_factory=EventBridgeConfig)
    spans_substrate: SpansSubstrateConfig = Field(
        default_factory=SpansSubstrateConfig
    )

    # API
    api_port: int = Field(
        default_factory=lambda: int(os.getenv("AKOSHA_API_PORT", str(DEFAULT_MCP_PORT)))
    )
    mcp_port: int = Field(
        default_factory=lambda: int(os.getenv("AKOSHA_MCP_PORT", str(DEFAULT_MCP_PORT)))
    )
    debug: bool = False
    log_level: str = Field(default_factory=lambda: os.getenv("AKOSHA_LOG_LEVEL", "INFO"))

    # Processing
    ingestion_workers: int = Field(
        default_factory=lambda: int(os.getenv("AKOSHA_INGESTION_WORKERS", "3"))
    )
    max_concurrent_ingests: int = 100
    shard_count: int = 256

    # Monitoring
    metrics_enabled: bool = True
    prometheus_port: int = Field(
        default_factory=lambda: int(os.getenv("AKOSHA_PROMETHEUS_PORT", "9090"))
    )

    # Environment
    environment: str = Field(default_factory=lambda: os.getenv("AKOSHA_ENVIRONMENT", "development"))

    # ---- Plan 2 Phase E2 substrate fields --------------------------
    # Atomic per-file adoption (Phase E2 §6.6). These fields read from
    # ``OneiricSettings.substrate`` rather than local ``AKOSHA_*`` env vars.
    # Defaults via ``default_factory`` so the read happens at instance
    # creation time (avoids module-load snapshot drift).
    #
    # Auth short-circuit preserved per ``feedback-bodai-localhost-no-auth.md``:
    # ``auth__enabled=False`` keeps the gate off; ``auth__loopback_trusted=True``
    # permits loopback unauthenticated requests.
    auth_enabled: bool = Field(
        default_factory=lambda: _substrate_field("auth__enabled"),
        description=(
            "Master auth gate (from substrate). When False, all auth "
            "gates short-circuit. Override via "
            "ONEIRIC__SUBSTRATE__AUTH__ENABLED."
        ),
    )
    auth_loopback_trusted: bool = Field(
        default_factory=lambda: _substrate_field("auth__loopback_trusted"),
        description=(
            "Permit loopback unauthenticated when True (from substrate). "
            "Override via ONEIRIC__SUBSTRATE__AUTH__LOOPBACK_TRUSTED."
        ),
    )
    pool_workers_per_instance: int = Field(
        default_factory=lambda: _substrate_field("pool__workers_per_instance"),
        description=(
            "Worker count per substrate-touching instance (from substrate). "
            "Override via ONEIRIC__SUBSTRATE__POOL__WORKERS_PER_INSTANCE."
        ),
    )

    model_config = {"arbitrary_types_allowed": True}


def load_config_from_file(config_path: str) -> dict[str, Any]:
    """Load configuration from YAML file.

    Args:
        config_path: Path to YAML configuration file

    Returns:
        Configuration dictionary
    """
    import yaml

    path = Path(config_path).expanduser()
    if not path.exists():
        logger.warning(f"Config file not found: {config_path}")
        return {}

    try:
        with path.open() as f:
            config: dict[str, Any] = yaml.safe_load(f) or {}  # type: ignore[assignment]
        logger.info(f"Loaded configuration from {config_path}")
        return config
    except Exception as e:
        logger.error(f"Failed to load config from {config_path}: {e}")
        return {}


def validate_storage_config(config: AkoshaConfig) -> dict[str, bool]:
    """Validate that storage paths are accessible.

    Args:
        config: Akosha configuration instance

    Returns:
        Dictionary mapping storage tier to validity status
    """
    results: dict[str, bool] = {}

    # Validate warm path. ``config.warm.path`` is typed as ``Path | None``; the
    # walrus + ``is None`` check narrows the type for the rest of the branch.
    if (warm_path := config.warm.path) is None or not warm_path.exists():
        # Either path is missing or doesn't exist on disk — try to create it.
        if warm_path is not None:
            try:
                warm_path.mkdir(parents=True, exist_ok=True)
                results["warm"] = True
                logger.info(f"Created warm storage directory: {warm_path}")
            except Exception as e:
                results["warm"] = False
                logger.error(f"Cannot create warm storage directory {warm_path}: {e}")
        else:
            results["warm"] = False
            logger.error("Warm storage path is not configured")
    else:
        results["warm"] = True

    # Validate WAL path (if enabled)
    if config.hot.write_ahead_log:
        wal_path = config.hot.wal_path
        if wal_path is None or not wal_path.exists():
            if wal_path is not None:
                try:
                    wal_path.mkdir(parents=True, exist_ok=True)
                    results["wal"] = True
                    logger.info(f"Created WAL directory: {wal_path}")
                except Exception as e:
                    results["wal"] = False
                    logger.error(f"Cannot create WAL directory {wal_path}: {e}")
            else:
                results["wal"] = False
                logger.error("WAL path is not configured")
        else:
            results["wal"] = True

    # Cold storage is external, just validate config
    results["cold"] = bool(config.cold.bucket)

    # Hot store is in-memory, always valid
    results["hot"] = True

    return results


def get_config(config_path: str | None = None) -> AkoshaConfig:
    """Get configuration instance.

    Migrated from MCPBaseSettings.load() to oneiric.core.config.load_settings().
    Reads ``settings/akosha.yaml`` via oneiric's layered-config machinery
    and constructs an ``AkoshaConfig`` from the relevant fields.

    Args:
        config_path: Optional path to YAML config file (for backward
            compatibility). When provided, this file's contents override
            the oneiric-loaded defaults with the highest priority
            (after env vars).

    Returns:
        AkoshaConfig instance
    """
    from oneiric.core.config import load_settings as _oneiric_load

    # Read the explicit file ourselves so unknown-to-OneiricSettings
    # fields (like ``mode``) survive the round-trip. oneiric's
    # ``load_settings`` validates through OneiricSettings, which
    # silently drops any field it doesn't declare (Pydantic v2 default
    # with no ``extra="allow"``). For Akosha-specific fields like
    # ``mode``, that drop means explicit overrides are silently lost.
    explicit_data: dict[str, Any] = {}
    if config_path:
        file = Path(config_path)
        if file.exists():
            import yaml

            explicit_data = yaml.safe_load(file.read_text()) or {}

    # Anchor at the package install location so the lookup is
    # CWD-independent. oneiric's load_settings accepts ``project_root=``
    # to anchor Layer 6-7 (``settings/{project_name}.yaml`` and
    # ``settings/local.yaml``) at the package directory, while still
    # honoring Layer 1-5 (explicit path, env vars, XDG). Do NOT use
    # ``path=`` here — that short-circuits the explicit-config layer
    # and disables XDG lookup at ``~/.config/{project_name}/*.yaml``.
    project_root = Path(__file__).resolve().parent.parent
    settings_obj = _oneiric_load(
        project_name="akosha",
        project_root=project_root,
    )

    # Strip values that don't match an AkoshaConfig field. ``OneiricSettings``
    # includes inherited fields (``http_port``, ``cache_dir``, etc.) from
    # ``OneiricMCPConfig`` that AkoshaConfig also inherits -- but
    # OneiricSettings may populate them with ``None`` (Pydantic v2 default
    # for required fields). Filter to (a) AkoshaConfig fields and
    # (b) non-None values so we don't trip the type validator.
    merged: dict[str, Any] = {
        **settings_obj.model_dump(),
        **explicit_data,
    }
    relevant_data = {
        k: v for k, v in merged.items() if k in AkoshaConfig.model_fields and v is not None
    }

    # Plan 2 Phase E2: strip substrate-mapped fields from the loaded
    # data so the per-class ``default_factory`` lambdas (which read from
    # ``OneiricSettings.substrate``) take effect. Without this strip,
    # YAML-loaded values (e.g. ``warm.pg_url: ""`` from settings/akosha.yaml)
    # would override substrate env-var overrides. See the field
    # definitions above for the canonical substrate-mapping list.
    relevant_data = _strip_substrate_mapped(relevant_data)
    return AkoshaConfig(**relevant_data)


# Global configuration instance
config = get_config()
