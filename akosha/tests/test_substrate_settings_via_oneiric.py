"""Plan 2 Phase E2 Task 1: substrate settings flow through akosha.config.

Per ``docs/plans/2026-09-27-shared-bodai-substrate-phase-e2.md`` §6.6:
``akosha/config.py`` adopts ``OneiricSettings.substrate`` for
cache/warm/cold/embedding/pool/retry/auth settings.

Atomic per-file rule (same commit must delete the local field + adopt the
substrate reference). These tests pin the substrate flow so a half-migrated
state (one field reading substrate, sibling still reading ``AKOSHA_*`` env)
fails CI.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_substrate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip substrate env vars so each test starts from a clean slate.

    The substrate settings object is a process-singleton Pydantic model;
    without this scrub, prior tests' env vars leak across tests.
    """
    for key in list(__import__("os").environ):
        if key.startswith("ONEIRIC__SUBSTRATE__"):
            monkeypatch.delenv(key, raising=False)


def test_cache_max_entries_via_substrate(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ONEIRIC__SUBSTRATE__CACHE__MAX_ENTRIES`` flows to ``cfg.cache.max_entries``.

    Regression guard for Phase E2 atomic migration. Prior to migration the
    field did not exist (RED). Post-migration it must read from substrate.
    """
    monkeypatch.setenv("ONEIRIC__SUBSTRATE__CACHE__MAX_ENTRIES", "42")
    from akosha.config import get_config

    cfg = get_config()
    assert cfg.cache.max_entries == 42


def test_warm_pg_url_via_substrate(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ONEIRIC__SUBSTRATE__WARM__PG_URL`` flows to ``cfg.warm.pg_url``."""
    monkeypatch.setenv(
        "ONEIRIC__SUBSTRATE__WARM__PG_URL",
        "postgresql+psycopg://ak_test/akosha",
    )
    from akosha.config import get_config

    cfg = get_config()
    assert "ak_test" in cfg.warm.pg_url


def test_auth_enabled_via_substrate(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ONEIRIC__SUBSTRATE__AUTH__ENABLED`` flows to ``cfg.auth_enabled``.

    Per ``feedback-bodai-localhost-no-auth.md`` the auth gate must
    short-circuit when auth is disabled; this test pins both halves of
    the substrate flow.
    """
    monkeypatch.setenv("ONEIRIC__SUBSTRATE__AUTH__ENABLED", "true")
    from akosha.config import get_config

    cfg = get_config()
    assert cfg.auth_enabled


def test_auth_loopback_trusted_via_substrate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ONEIRIC__SUBSTRATE__AUTH__LOOPBACK_TRUSTED`` flows through substrate."""
    monkeypatch.setenv("ONEIRIC__SUBSTRATE__AUTH__LOOPBACK_TRUSTED", "false")
    from akosha.config import get_config

    cfg = get_config()
    assert not cfg.auth_loopback_trusted


def test_cold_bucket_via_substrate(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ONEIRIC__SUBSTRATE__COLD__BUCKET`` flows to ``cfg.cold.bucket``."""
    monkeypatch.setenv(
        "ONEIRIC__SUBSTRATE__COLD__BUCKET",
        "akosha-cold-test-bucket",
    )
    from akosha.config import get_config

    cfg = get_config()
    assert cfg.cold.bucket == "akosha-cold-test-bucket"


def test_pool_workers_per_instance_via_substrate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ONEIRIC__SUBSTRATE__POOL__WORKERS_PER_INSTANCE`` flows to ``cfg.pool_workers_per_instance``."""
    monkeypatch.setenv("ONEIRIC__SUBSTRATE__POOL__WORKERS_PER_INSTANCE", "7")
    from akosha.config import get_config

    cfg = get_config()
    assert cfg.pool_workers_per_instance == 7
