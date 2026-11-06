"""Standard mode: Full production configuration with Redis and optional cold storage."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from akosha.modes.base import BaseMode, ModeConfig

logger = logging.getLogger(__name__)


def _resolve_cold_settings(fallback_config: dict[str, Any]) -> dict[str, Any]:
    """Resolve the cold-tier settings from the nested ``cold:`` config block.

    The authoritative source is :class:`akosha.config.AkoshaConfig`'s
    ``cold`` field, which the Oneiric layered loader populates from:

    1. the substrate env vars (``ONEIRIC__SUBSTRATE__COLD__*``),
    2. the operator's XDG overlay (``~/.config/akosha/local.yaml``),
    3. the committed repo defaults (``settings/akosha.yaml``).

    ``fallback_config`` (the CLI's explicit ``--config`` dict) is honoured
    for the legacy FLAT key names so an explicit config file keeps
    working. A flat key that the caller actually supplied WINS over the
    layered block: ``fallback_config`` is the more specific layer (a
    ``--config <file>`` on the command line, or a direct construction in
    a test), while the layered block is the machine-wide default. With
    the layered block winning instead, ``StandardMode(config=...)`` would
    ignore its own argument and the result would depend on the operator's
    environment rather than on the call.
    """
    from akosha.config import get_config

    try:
        cold = get_config().cold
    except Exception as exc:
        logger.warning("Could not load the cold-storage config (%s)", exc)
        cold = None

    resolved: dict[str, Any] = {
        "backend": getattr(cold, "backend", "s3") if cold is not None else "s3",
        "bucket": getattr(cold, "bucket", "") if cold is not None else "",
        "prefix": getattr(cold, "prefix", "conversations/")
        if cold is not None
        else "conversations/",
        "region": getattr(cold, "region", None) if cold is not None else None,
        "endpoint_url": getattr(cold, "endpoint_url", None) if cold is not None else None,
        "access_key_id": getattr(cold, "access_key_id", None) if cold is not None else None,
        "secret_access_key": (
            getattr(cold, "secret_access_key", None) if cold is not None else None
        ),
    }

    # Legacy flat keys: an explicitly supplied value overrides the
    # ambient layered block. A key that is absent, or present but empty,
    # leaves the layered value in place.
    flat_map = {
        "backend": "cold_storage_backend",
        "bucket": "cold_bucket",
        "prefix": "cold_prefix",
        "region": "cold_region",
        "endpoint_url": "cold_endpoint_url",
        "access_key_id": "cold_access_key_id",
        "secret_access_key": "cold_secret_access_key",
    }
    for key, flat_key in flat_map.items():
        if flat_key not in fallback_config:
            continue
        value = fallback_config[flat_key]
        if value not in (None, ""):
            resolved[key] = value
    return resolved


class StandardMode(BaseMode):
    """Standard mode with full production features.

    Standard mode characteristics:
    - Redis cache for improved performance
    - Optional cold storage for derived data and exports
    - Production-ready scalability
    - Persistent storage when configured
    - May require external services depending on storage backend

    Graceful degradation:
    - Falls back to in-memory cache if Redis unavailable
    - Falls back to local storage if cloud storage unavailable
    - Logs warnings but continues operation

    Examples:
        >>> mode = StandardMode(config={"redis_host": "localhost"})
        >>> mode.mode_config.name
        'standard'
        >>> mode.requires_external_services
        True
    """

    def get_mode_config(self) -> ModeConfig:
        """Get standard mode configuration.

        Returns:
            ModeConfig with standard mode settings
        """
        return ModeConfig(
            name="standard",
            description="Standard mode: Full production configuration with Redis and optional cold storage",
            redis_enabled=True,
            cold_storage_enabled=True,
            cache_backend="redis",
        )

    async def initialize_cache(self) -> Any:
        """Initialize Redis cache with graceful fallback.

        Returns:
            Redis client instance if available, None for in-memory fallback

        Note:
            If Redis is unavailable, falls back to in-memory cache
            and logs a warning. Application continues normally.
        """
        try:
            import redis

            redis_host = self.config.get("redis_host", "localhost")
            redis_port = self.config.get("redis_port", 6379)
            redis_db = self.config.get("redis_db", 0)

            logger.info(f"Standard mode: Connecting to Redis at {redis_host}:{redis_port}")

            redis_client = redis.Redis(
                host=redis_host,
                port=redis_port,
                db=redis_db,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )

            # Test connection
            redis_client.ping()

            logger.info("Standard mode: Redis cache initialized successfully")
            return redis_client

        except ImportError:
            logger.warning(
                "Standard mode: redis package not installed, using in-memory cache. "
                "Install with: pip install redis"
            )
            return None

        except Exception as e:
            logger.warning(
                f"Standard mode: Redis unavailable ({e}), using in-memory cache. "
                "Start Redis with: docker run -d -p 6379:6379 redis"
            )
            return None

    async def initialize_cold_storage(self) -> Any:
        """Initialize the cold store with graceful fallback.

        Returns:
            A :class:`akosha.storage.cold_store.ColdStore` instance if
            available, None if disabled.

        Note:
            If cloud storage is unavailable, logs a warning and
            continues without cold storage functionality.

        The cold-tier settings come from the NESTED ``cold:`` block of
        :class:`akosha.config.AkoshaConfig` — the Oneiric layered loader
        (repo ``settings/akosha.yaml``, overlaid by the operator's
        ``~/.config/akosha/local.yaml`` and the substrate env vars).

        This used to read FLAT keys (``cold_storage_backend``,
        ``cold_bucket``, ``cold_region``, ...) out of ``self.config``.
        No YAML in the ecosystem defines flat keys — every one uses the
        nested ``cold:`` block — and ``self.config`` was ``{}`` by
        default anyway (the CLI only loads an explicit ``--config`` file).
        So the cold tier always resolved to ``backend="local"``,
        ``bucket="akosha-cold-data"``, ``local_path="./data/cold"`` and
        never touched R2.

        It used to return a bare oneiric storage ADAPTER and build the
        adapter settings here, duplicating the construction that
        :meth:`akosha.storage.cold_store.ColdStore.initialize` already
        owns (and covering only two of the four backends). It now
        returns the ``ColdStore`` itself: the prefix, the bucket and the
        credentials then all reach the one object that assembles the
        object key, so the configured prefix is actually applied to
        every cold object.
        """
        try:
            from akosha.storage.cold_store import ColdStore, resolve_storage_backend

            cold = _resolve_cold_settings(self.config)
            # "r2" is the substrate's declared cold backend, but no
            # adapter implements that name — R2 is the S3 API with a
            # custom endpoint and region="auto". Translate at the
            # adapter boundary.
            configured_backend = cold["backend"]
            try:
                backend = resolve_storage_backend(configured_backend)
            except ValueError as exc:
                # An unrecognised backend name is an operator typo.
                # Fall back to local storage with a warning that names
                # the bad value, rather than letting the generic
                # handler below swallow it and leave the cold tier
                # unconfigured with no explanation.
                logger.warning(
                    "Standard mode: Unsupported cold storage backend %r (%s), "
                    "falling back to local storage",
                    configured_backend,
                    exc,
                )
                backend = "local"
            bucket = cold["bucket"]
            prefix = cold["prefix"]

            logger.info(
                "Standard mode: Initializing %s cold storage (bucket: %s, prefix: %s)",
                backend,
                bucket,
                prefix,
            )

            store = ColdStore(
                bucket=bucket,
                prefix=prefix,
                storage_backend=backend,
                local_dir=self._cold_local_dir(),
                region=cold["region"],
                endpoint_url=cold["endpoint_url"],
                access_key_id=cold["access_key_id"],
                secret_access_key=cold["secret_access_key"],
            )
            await store.initialize()

            logger.info("Standard mode: Cold storage initialized successfully (%s)", backend)
            return store

        except ImportError as e:
            logger.warning(
                f"Standard mode: Oneiric storage adapter not available ({e}), cold storage disabled. "
                "Install Oneiric with: pip install oneiric"
            )
            return None

        except Exception as e:
            logger.warning(
                f"Standard mode: Cold storage unavailable ({e}), continuing without cold storage. "
                "Configure storage credentials or use local backend."
            )
            return None

    def _cold_local_dir(self) -> Path:
        """Resolve the local backend's root directory.

        An explicit ``local_storage_path`` in the caller's config wins;
        otherwise the storage path resolver's cold-cache path is used, so
        the local backend writes to the same place the rest of the
        storage tier writes to instead of a CWD-relative ``./data/cold``.
        """
        configured = self.config.get("local_storage_path")
        if configured:
            return Path(configured)
        from akosha.storage.path_resolver import StoragePathResolver

        return StoragePathResolver().get_cold_store_cache_path()

    @property
    def requires_external_services(self) -> bool:
        """Standard mode requires external services.

        Returns:
            True (Redis and cloud storage required for full functionality)
        """
        return True

    def __repr__(self) -> str:
        """String representation of standard mode."""
        return "StandardMode(services_required=True, cache=redis, cold_storage=cloud)"
