"""Substrate adoption contract (Plan 2 Phase E2, Task 2 mirror).

These tests pin the atomic per-file migration of akosha substrate
fields to ``OneiricSettings.substrate.*``. The atomic-per-file rule
forbids partial state — for every migrated key, the local declaration
is replaced with a ``default_factory`` reading from substrate.

The substrate-mapped fields exposed on akosha config (per
``akosha/config.py`` Plan 2 Phase E2):

- top-level: ``auth_enabled``, ``auth_loopback_trusted``,
  ``pool_workers_per_instance``
- nested: ``warm.{backend, pg_url, pg_pool_size}``,
  ``cold.{backend, bucket, prefix, compression}``,
  ``cache.{backend, max_entries, default_ttl_seconds}``

Auth short-circuit (Phase E2 spec §2): when
``ONEIRIC__SUBSTRATE__AUTH__ENABLED`` is ``true``, ``A koshaConfig.auth_enabled``
exposes ``True``; when ``false`` (the substrate default), it exposes
``False``. ``auth_loopback_trusted`` mirrors
``ONEIRIC__SUBSTRATE__AUTH__LOOPBACK_TRUSTED``.

Test isolation: each test sets the substrate env var via
``monkeypatch.setenv`` then instantiates the relevant config model.
Pydantic-settings ``BaseSettings.__init__`` re-reads env vars on every
construction; the substrate lazy accessor (``akosha.config._substrate_field``)
calls ``oneiric.core.config.load_settings()`` fresh per field resolution,
so env-var overrides propagate without a cache reset.
"""

from __future__ import annotations

import pytest


class TestSubstrateSettingsViaOneiric:
    """A kosha substrate fields read from ``OneiricSettings.substrate``."""

    def test_auth_enabled_via_substrate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``A koshaConfig.auth_enabled`` defaults from ``substrate.auth__enabled``.

        The setting is loopback-default false (``ONEIRIC__SUBSTRATE__AUTH__
        ENABLED=False``). Auth short-circuit MUST continue to bypass
        middleware + decorator gates when the substrate says
        ``auth__enabled=False`` (per ``feedback-bodai-localhost-no-auth.md``).
        """
        from akosha.config import AkoshaConfig

        monkeypatch.setenv("ONEIRIC__SUBSTRATE__AUTH__ENABLED", "true")
        cfg = AkoshaConfig()

        assert cfg.auth_enabled is True

    def test_auth_loopback_trusted_via_substrate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``auth_loopback_trusted`` mirrors substrate; required for the
        localhost-no-auth escape hatch."""
        from akosha.config import AkoshaConfig

        monkeypatch.setenv(
            "ONEIRIC__SUBSTRATE__AUTH__LOOPBACK_TRUSTED", "false"
        )
        cfg = AkoshaConfig()

        assert cfg.auth_loopback_trusted is False

    def test_pool_workers_per_instance_via_substrate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``pool_workers_per_instance`` defaults from
        ``substrate.pool__workers_per_instance``."""
        from akosha.config import AkoshaConfig

        monkeypatch.setenv(
            "ONEIRIC__SUBSTRATE__POOL__WORKERS_PER_INSTANCE", "7"
        )
        cfg = AkoshaConfig()

        assert cfg.pool_workers_per_instance == 7

    def test_warm_backend_via_substrate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``WarmStorageConfig.backend`` defaults from
        ``substrate.warm__backend`` (substrate default: ``pgvector`` per
        Phase E1 contract — only pgvector ships as a warm-tier backend)."""
        from akosha.config import WarmStorageConfig

        monkeypatch.setenv("ONEIRIC__SUBSTRATE__WARM__BACKEND", "pgvector")
        cfg = WarmStorageConfig()

        assert cfg.backend == "pgvector"

    def test_warm_pg_url_via_substrate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``WarmStorageConfig.pg_url`` defaults from
        ``substrate.warm__pg_url``."""
        from akosha.config import WarmStorageConfig

        monkeypatch.setenv(
            "ONEIRIC__SUBSTRATE__WARM__PG_URL",
            "postgresql+psycopg://substrate/test",
        )
        cfg = WarmStorageConfig()

        assert cfg.pg_url == "postgresql+psycopg://substrate/test"

    def test_cold_backend_via_substrate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``ColdStorageConfig.backend`` defaults from
        ``substrate.cold__backend`` (substrate default: ``r2``)."""
        from akosha.config import ColdStorageConfig

        monkeypatch.setenv("ONEIRIC__SUBSTRATE__COLD__BACKEND", "s3")
        cfg = ColdStorageConfig()

        assert cfg.backend == "s3"

    def test_cold_bucket_via_substrate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``ColdStorageConfig.bucket`` defaults from
        ``substrate.cold__bucket`` (substrate default:
        ``oneiric-substrate-shared``)."""
        from akosha.config import ColdStorageConfig

        monkeypatch.setenv(
            "ONEIRIC__SUBSTRATE__COLD__BUCKET", "my-substrate-bucket"
        )
        cfg = ColdStorageConfig()

        assert cfg.bucket == "my-substrate-bucket"

    def test_cache_backend_via_substrate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``CacheConfig.backend`` defaults from
        ``substrate.cache__backend`` (substrate default: ``memory``)."""
        from akosha.config import CacheConfig

        monkeypatch.setenv("ONEIRIC__SUBSTRATE__CACHE__BACKEND", "redis")
        cfg = CacheConfig()

        assert cfg.backend == "redis"

    def test_substrate_defaults_flow_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no substrate env override is set, the substrate defaults
        flow through — i.e. ``warm.backend`` defaults to ``pgvector``,
        ``cold.backend`` defaults to ``r2``, and ``cache.backend``
        defaults to ``memory``."""
        from akosha.config import (
            CacheConfig,
            ColdStorageConfig,
            WarmStorageConfig,
        )

        for env in (
            "ONEIRIC__SUBSTRATE__WARM__BACKEND",
            "ONEIRIC__SUBSTRATE__WARM__PG_URL",
            "ONEIRIC__SUBSTRATE__COLD__BACKEND",
            "ONEIRIC__SUBSTRATE__COLD__BUCKET",
            "ONEIRIC__SUBSTRATE__CACHE__BACKEND",
        ):
            monkeypatch.delenv(env, raising=False)

        warm = WarmStorageConfig()
        cold = ColdStorageConfig()
        cache = CacheConfig()
        assert warm.backend == "pgvector"
        assert cold.backend == "r2"
        assert cold.bucket == "oneiric-substrate-shared"
        assert cache.backend == "memory"

    def test_get_config_returns_akosha_config(self) -> None:
        """Public-surface contract: ``get_config()`` returns an
        ``A koshaConfig`` instance — never ``OneiricSettings``
        directly. Existing callers (CLI, mcp/server.py) keep working
        without changes."""
        from akosha.config import AkoshaConfig, get_config

        cfg = get_config()

        assert isinstance(cfg, AkoshaConfig)