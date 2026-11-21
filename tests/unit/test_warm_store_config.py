"""Unit tests for the warm-tier configuration resolution.

Covers ``akosha.storage.resolve_warm_store_config`` and
``akosha.storage.apply_warm_env_overrides`` — the two functions that
decide which warm store Akosha actually runs.

Why these exist
---------------
The warm tier is selected by reading the repo's ``settings/akosha.yaml``
DIRECTLY, not through the layered Oneiric loader: ``warm.backend`` and
``warm.pg_url`` are substrate-mapped and are stripped from the YAML
before ``AkoshaConfig`` is built. The XDG overlay at
``~/.config/akosha/local.yaml`` therefore never reached the warm store.

Two bugs hid behind that:

* The repo root was resolved as ``Path(__file__).resolve().parents[2]``,
  which points one directory ABOVE the repo, so the settings file it
  looked for never existed and every call silently fell back to the
  ``duckdb-ssd`` defaults — with no warning at all.
* There was no way for an operator to select the pgvector warm tier
  without editing a committed file. The env vars are that lever now.

The tests below pin both: the file is actually found, and the env vars
win over it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from akosha.storage import (
    WARM_BACKEND_ENV,
    WARM_PG_URL_ENV,
    apply_warm_env_overrides,
    resolve_warm_store_config,
)

_TEST_PG_URL = "postgresql://postgres@127.0.0.1:6432/akosha_pgvector"

if TYPE_CHECKING:
    import pytest


class TestApplyWarmEnvOverrides:
    """The env vars override whatever the caller resolved."""

    def test_no_env_vars_leaves_values_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(WARM_BACKEND_ENV, raising=False)
        monkeypatch.delenv(WARM_PG_URL_ENV, raising=False)
        assert apply_warm_env_overrides("duckdb-ssd", "") == ("duckdb-ssd", "")

    def test_backend_env_var_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(WARM_BACKEND_ENV, "pgvector")
        monkeypatch.delenv(WARM_PG_URL_ENV, raising=False)
        backend, pg_url = apply_warm_env_overrides("duckdb-ssd", "")
        assert backend == "pgvector"
        assert pg_url == ""

    def test_pg_url_env_var_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(WARM_BACKEND_ENV, raising=False)
        monkeypatch.setenv(WARM_PG_URL_ENV, _TEST_PG_URL)
        backend, pg_url = apply_warm_env_overrides("duckdb-ssd", "postgresql://old")
        assert backend == "duckdb-ssd"
        assert pg_url == _TEST_PG_URL

    def test_both_env_vars_apply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(WARM_BACKEND_ENV, "pgvector")
        monkeypatch.setenv(WARM_PG_URL_ENV, _TEST_PG_URL)
        assert apply_warm_env_overrides("duckdb-ssd", "") == ("pgvector", _TEST_PG_URL)

    def test_blank_env_var_does_not_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A whitespace-only env var is ignored, not treated as a value."""
        monkeypatch.setenv(WARM_BACKEND_ENV, "   ")
        monkeypatch.delenv(WARM_PG_URL_ENV, raising=False)
        assert apply_warm_env_overrides("duckdb-ssd", "") == ("duckdb-ssd", "")


class TestResolveWarmStoreConfig:
    """The resolver reads the repo settings file and applies the env overrides."""

    def test_finds_the_repo_settings_file(self) -> None:
        """The repo root is resolved correctly — the settings file IS read.

        This is the regression pin for the ``parents[2]`` off-by-one that
        made every lookup miss and fall back to the defaults silently.
        """
        from akosha import storage

        settings_path = storage._REPO_ROOT / "settings" / "akosha.yaml"
        assert settings_path.exists(), f"resolver cannot find {settings_path}"
        assert settings_path.parent.name == "settings"

    def test_reads_the_warm_block_from_the_repo_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The committed ``warm:`` block is the base default."""
        monkeypatch.delenv(WARM_BACKEND_ENV, raising=False)
        monkeypatch.delenv(WARM_PG_URL_ENV, raising=False)

        resolved = resolve_warm_store_config()

        assert resolved["backend"] == "duckdb-ssd"
        assert resolved["pg_url"] == ""
        assert set(resolved) == {"backend", "pg_url", "db_path"}

    def test_env_vars_win_over_the_repo_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An operator can select pgvector without editing a committed file."""
        monkeypatch.setenv(WARM_BACKEND_ENV, "pgvector")
        monkeypatch.setenv(WARM_PG_URL_ENV, _TEST_PG_URL)

        resolved = resolve_warm_store_config()

        assert resolved["backend"] == "pgvector"
        assert resolved["pg_url"] == _TEST_PG_URL

    def test_missing_settings_file_falls_back_to_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing settings file degrades to the safe default, not a crash."""
        import akosha.storage as storage

        monkeypatch.delenv(WARM_BACKEND_ENV, raising=False)
        monkeypatch.delenv(WARM_PG_URL_ENV, raising=False)
        monkeypatch.setattr(storage, "_REPO_ROOT", Path("/nonexistent-repo-root"))

        resolved = storage.resolve_warm_store_config()

        assert resolved["backend"] == "duckdb-ssd"
        assert resolved["pg_url"] == ""
        assert resolved["db_path"] is None

    def test_malformed_settings_file_falls_back_to_defaults(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A YAML syntax error must DEGRADE to the default, not disable the tier.

        ``yaml.YAMLError`` is not a ``ValueError``, so a malformed file used
        to escape this function entirely and take the whole warm tier down
        with it — the opposite of the fallback this function promises.
        """
        import akosha.storage as storage

        monkeypatch.delenv(WARM_BACKEND_ENV, raising=False)
        monkeypatch.delenv(WARM_PG_URL_ENV, raising=False)
        settings_dir = tmp_path / "settings"
        settings_dir.mkdir()
        (settings_dir / "akosha.yaml").write_text(
            'warm:\n  backend: pgvector\n   pg_url: "x"\n'
        )
        monkeypatch.setattr(storage, "_REPO_ROOT", tmp_path)

        resolved = storage.resolve_warm_store_config()

        assert resolved["backend"] == "duckdb-ssd"
        assert resolved["pg_url"] == ""
        assert resolved["db_path"] is None

    def test_non_mapping_settings_root_falls_back_to_defaults(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A settings file whose root is a list is unreadable, not fatal."""
        import akosha.storage as storage

        monkeypatch.delenv(WARM_BACKEND_ENV, raising=False)
        monkeypatch.delenv(WARM_PG_URL_ENV, raising=False)
        settings_dir = tmp_path / "settings"
        settings_dir.mkdir()
        (settings_dir / "akosha.yaml").write_text("- a\n- b\n")
        monkeypatch.setattr(storage, "_REPO_ROOT", tmp_path)

        resolved = storage.resolve_warm_store_config()

        assert resolved["backend"] == "duckdb-ssd"


class TestCreateWarmStoreHonoursItsArguments:
    """The constructor honours what it is given — the env override is a
    RESOLUTION concern, not a constructor concern.

    The MCP server's aging service calls ``create_warm_store(backend=
    "duckdb-ssd")`` deliberately: ``AgingService`` calls
    ``insert_batch()`` and reads ``.conn``, and ``PgvectorWarmStore``
    implements neither. If the constructor re-applied the machine-local
    backend override, a machine that selected pgvector would hand the
    aging service a pgvector store and break the batch migration path.
    """

    def test_explicit_duckdb_backend_survives_the_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit ``duckdb-ssd`` is NOT rewritten to pgvector."""
        from akosha.storage import create_warm_store
        from akosha.storage.warm_store import WarmStore

        monkeypatch.setenv(WARM_BACKEND_ENV, "pgvector")
        monkeypatch.setenv(WARM_PG_URL_ENV, _TEST_PG_URL)

        store = create_warm_store(backend="duckdb-ssd")

        assert isinstance(store, WarmStore)

    def test_explicit_pgvector_backend_is_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An explicit ``pgvector`` stays pgvector."""
        from akosha.storage import create_warm_store
        from akosha.storage.pgvector_warm_store import PgvectorWarmStore

        monkeypatch.delenv(WARM_BACKEND_ENV, raising=False)

        store = create_warm_store(backend="pgvector", pg_url=_TEST_PG_URL)

        assert isinstance(store, PgvectorWarmStore)

    def test_pgvector_without_a_dsn_falls_back_to_duckdb(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pgvector with no DSN and no env DSN degrades to DuckDB, loudly."""
        from akosha.storage import create_warm_store
        from akosha.storage.warm_store import WarmStore

        monkeypatch.delenv(WARM_BACKEND_ENV, raising=False)
        monkeypatch.delenv(WARM_PG_URL_ENV, raising=False)

        store = create_warm_store(backend="pgvector", pg_url="")

        assert isinstance(store, WarmStore)


class TestEffectiveWarmBackend:
    """The startup log must name the backend that is RUNNING.

    ``create_warm_store`` fail-softs to DuckDB when pgvector is
    requested without a DSN, so the requested selector and the running
    one can differ. The log field named ``backend`` used to hold the
    CONSTRUCTED CLASS NAME (``WarmStore`` / ``PgvectorWarmStore``),
    which answers "what was constructed" and not "which backend is
    running" -- and cannot be grepped against the configured value.
    """

    def test_pgvector_store_reports_pgvector(self) -> None:
        from akosha.storage import effective_warm_backend
        from akosha.storage.pgvector_warm_store import PgvectorWarmStore

        store = PgvectorWarmStore(pg_url="postgresql://postgres@127.0.0.1:6432/x")

        assert effective_warm_backend(store) == "pgvector"

    def test_duckdb_store_reports_duckdb_ssd(self) -> None:
        from akosha.storage import effective_warm_backend
        from akosha.storage.warm_store import WarmStore

        store = WarmStore(database_path=Path("/tmp/akosha-warm-test/warm.db"))

        assert effective_warm_backend(store) == "duckdb-ssd"

    def test_an_in_memory_duckdb_store_reports_duckdb_memory(self) -> None:
        from akosha.storage import effective_warm_backend
        from akosha.storage.warm_store import WarmStore

        store = WarmStore(database_path=Path(":memory:"))

        assert effective_warm_backend(store) == "duckdb-memory"

    def test_no_store_reports_disabled(self) -> None:
        from akosha.storage import effective_warm_backend

        assert effective_warm_backend(None) == "disabled"
