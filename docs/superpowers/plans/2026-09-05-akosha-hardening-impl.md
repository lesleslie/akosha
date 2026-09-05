---
status: draft
role: implementation
date: 2026-09-05
last_reviewed: 2026-09-05
superseded_by: null
blocks_on:
  - docs/superpowers/specs/2026-09-05-akosha-hardening-design.md
topic: convergence-control-plane
---

# Akosha Comprehensive Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Address every finding from the 2026-09-05 Akosha audit: 10 production bugs, coverage gate failure (84.79% vs 87.62%), 9 zero-coverage modules, 5 broken tests, 382 empty no-assert tests, and an empty live MCP backend. Land in 5 sequential waves, each independently shippable to local main (no PRs per `bodai-pre-1.0-merge-policy`).

**Architecture:** Five sequential waves, each ending with a wave-end commit + feature-tracking entry. Wave 1 fixes P0 critical bugs. Wave 2 fixes P1 high bugs and writes tests for the 9 zero-coverage modules. Wave 3 fixes P2 medium bugs and raises the coverage gate from 87.62% to 90.0% (next ratchet milestone). Wave 4 rewrites 382 empty tests in-place with `pytest.raises` / failure-path probes and adds a self-guard. Wave 5 wires the live MCP backend data feeds (knowledge graph population, OTel ingester, code indexer) and bumps version to 0.15.0. Each task follows strict TDD: failing test → minimal implementation → passing test → commit.

**Tech Stack:** Python 3.14 (project target), `pytest`, `pytest-asyncio` (asyncio_mode = "auto" per `pyproject.toml:78`), `coverage.py` (branch coverage enabled), `datasketch>=0.6.0` (new for MinHash dedup), `aiohttp>=3.12.14` (new — currently undeclared), `oneiric>=0.20` (existing, used for storage adapter), `crackerjack` (project quality gate).

**Spec:** `/Users/les/Projects/akosha/docs/superpowers/specs/2026-09-05-akosha-hardening-design.md` — read it alongside this plan; the plan argues from the spec.

## Global Constraints

- Akosha project conventions per `CLAUDE.md` Crackerjack-Compliant Code: `from __future__ import annotations` first non-comment line; imports sorted within each section (stdlib → third-party → first-party with `force-sort-within-sections = true` and `known-first-party = ["akosha"]`); modern syntax `X | None` (not `Optional[X]`); function args with default `None` typed `X | None = None`; no `assert` in production code (`akosha/**`); `logger.exception` (not `logger.error(..., exc_info=True)`) in `except` blocks; oneiric logger (`oneiric.logging`) preferred over stdlib `logging`.
- Hard limits: line length 100, max 10 function args, max 15 branches, max 6 returns, max 55 statements per function.
- Async tests don't need `@pytest.mark.asyncio` (asyncio_mode = "auto").
- Coverage thresholds: **start at 87.62%** (`pyproject.toml:99`), **target 90.0%** by Wave 3 commit. New code must maintain 90.0% from Wave 3 onward.
- No PRs — each wave-end commit goes directly to local main (per `bodai-pre-1.0-merge-policy`).
- Each wave ends with: (a) wave-end commit, (b) `docs/feature-tracking/2026-09-05-akosha-hardening.md` entry marked with the wave's `built` date.
- Use `git -c user.email='les@wedgwoodwebworks.com' -c user.name='les'` for all commits (per `git-author-email-correct-domain.md` memory).

---

## File Structure

| Path | Purpose |
|---|---|
| `akosha/storage/cold_store.py` | Modified. Real `_upload_to_storage` via Oneiric storage adapter; add `storage_backend`, `initialize()`, `close()`. |
| `akosha/mcp/server.py` | Modified. Real `/health` and `/healthz` handlers; add `_check_dependency_health`. |
| `akosha/shell/adapter.py` | Modified. Replace five `TODO: Implement` shell commands with stub-status responses. |
| `akosha/api/middleware.py` | Verified. `aiohttp` import intentional; pin in `pyproject.toml`. |
| `akosha/cli.py` | Modified. Narrow `except` to `PackageNotFoundError`; export `_health_probe`. |
| `akosha/ingestion/orchestrator.py` | Modified. Replace `last_heartbeat` self-attestation with `DependencyConfig` ping. |
| `akosha/mcp/tools/code_graph_tools.py` | Modified. Restructure `_compute_graph_similarity` error handling. |
| `akosha/processing/deduplication.py` | Modified. Replace SHA-256 placeholder with MinHash; add `HasherFallback`. |
| `akosha/storage/aging.py` | Modified. Rewrite `_quantize_embedding` with scale persistence. |
| `akosha/main.py` | Modified. Add boot hooks for knowledge-graph population, OTel ingester, code indexer. |
| `akosha/__init__.py` | Modified. Bump `__version__` to `0.15.0` at Wave 5. |
| `pyproject.toml` | Modified. Add `aiohttp>=3.12.14`, `datasketch>=0.6.0`; bump `--cov-fail-under` to 90.0 at Wave 3; bump version to 0.15.0 at Wave 5. |
| `.coverage-ratchet.json` | Modified. Record 90% milestone at Wave 3. |
| `tests/unit/test_dependencies.py` | New. `test_aiohttp_declared`, `test_datasketch_declared`. |
| `tests/unit/test_cold_store.py` | New. ≥10 tests including `test_export_batch_writes_to_local`. |
| `tests/unit/test_main_entrypoints.py` | New. ≥4 smoke tests via subprocess. |
| `tests/unit/mcp/test_client.py` | New. ≥8 tests. |
| `tests/unit/cli/commands/test_migrate.py` | New. ≥8 tests. |
| `tests/unit/mcp/tools/test_fitness_tools.py` | New. ≥6 tests. |
| `tests/unit/processing/test_deduplication.py` | New. ≥8 tests. |
| `tests/unit/mcp/tools/test_tool_registry.py` | New. ≥6 tests. |
| `tests/unit/observability/test_eventbridge_adapter.py` | New. ≥4 tests with real assertions. |
| `tests/unit/storage/test_aging.py` | New. ≥6 tests including `test_quantize_roundtrip_within_eps`. |
| `tests/unit/test_test_quality.py` | New. `test_no_empty_tests` guard. |
| 382 in-place rewrites of empty tests | Across ~12 test files (Wave 4). |
| `docs/feature-tracking/2026-09-05-akosha-hardening.md` | New. Built/wired/adopted lifecycle entries per wave. |

Files changing together: `pyproject.toml` and `akosha/__init__.py:__version__` both define the version (pinned in sync); `akosha/main.py` boot hooks depend on `akosha/storage/cold_store.py:initialize()` existing; `akosha/cli.py:_health_probe` is the single source of truth consumed by both CLI and HTTP `/health` route.

---

# Wave 1 — P0 Critical Bugs + Broken Tests + Dependency Hygiene

## Task 1.1: Verify oneiric storage adapter availability

**Files:**

- Read-only: `akosha/storage/cold_store.py` (line 36)

**Step 1.1.1: Confirm `oneiric.storage` is importable**

Run: `cd /Users/les/Projects/akosha && .venv/bin/python -c "from oneiric.storage import S3StorageAdapter; print(S3StorageAdapter)"`
Expected: prints `<class 'oneiric.storage.S3StorageAdapter'>`. If the import fails, STOP — Task 1.2 falls back to `boto3` per the spec's risk mitigation.

**Step 1.1.2: Confirm installed oneiric version**

Run: `cd /Users/les/Projects/akosha && .venv/bin/python -c "import importlib.metadata; print(importlib.metadata.version('oneiric'))"`
Expected: `0.20.0` or higher (per `pyproject.toml:dependencies`).

**Commit:** No commit — verification step only.

## Task 1.2: ColdStore real upload (C1)

**Files:**

- Modify: `akosha/storage/cold_store.py:36, 71-205, 207, 212`
- Test: `tests/unit/test_cold_store.py` (new)

**Interfaces:**

- Consumes: `oneiric.storage.S3StorageAdapter` (verified in Task 1.1).
- Produces:
  ```python
  class ColdStore:
      def __init__(self, *, storage_backend: Literal["memory", "s3", "r2"] = "memory",
                   bucket: str | None = None, endpoint_url: str | None = None,
                   local_dir: Path | None = None) -> None: ...
      async def initialize(self) -> None: ...
      async def close(self) -> None: ...
      async def export_batch(self, records: list[dict[str, Any]], object_key: str) -> dict[str, Any]: ...
  ```

- [ ] **Step 1: Write failing tests in `tests/unit/test_cold_store.py`**

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from akosha.storage.cold_store import ColdStore


@pytest.fixture
def tmp_local_dir(tmp_path: Path) -> Path:
    return tmp_path / "cold"


@pytest.mark.asyncio
async def test_export_batch_writes_to_local(tmp_local_dir: Path) -> None:
    """Memory backend writes Parquet to local_dir."""
    store = ColdStore(storage_backend="memory", local_dir=tmp_local_dir)
    await store.initialize()
    try:
        result = await store.export_batch(
            [{"id": 1, "value": "foo"}, {"id": 2, "value": "bar"}],
            object_key="test/2026/01/01.parquet",
        )
        assert result["status"] == "success"
        assert result["bytes_written"] > 0
        assert (tmp_local_dir / "test" / "2026" / "01" / "01.parquet").exists()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_initialize_constructs_s3_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """S3 backend constructs oneiric S3StorageAdapter."""
    fake_adapter = AsyncMock()
    fake_adapter.upload = AsyncMock(return_value={"bytes_written": 100})
    monkeypatch.setattr("akosha.storage.cold_store.S3StorageAdapter", lambda **kw: fake_adapter)
    store = ColdStore(storage_backend="s3", bucket="my-bucket")
    await store.initialize()
    assert store._adapter is fake_adapter  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_close_calls_adapter_close(tmp_local_dir: Path) -> None:
    """close() forwards to underlying adapter."""
    store = ColdStore(storage_backend="memory", local_dir=tmp_local_dir)
    await store.initialize()
    store._adapter.close = AsyncMock()  # type: ignore[attr-defined]
    await store.close()
    store._adapter.close.assert_awaited_once()  # type: ignore[attr-defined]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/test_cold_store.py -v`
Expected: 3 failures with "ColdStore.__init__ got unexpected keyword argument 'storage_backend'" or "no attribute 'storage_backend'".

- [ ] **Step 3: Implement `ColdStore.__init__` and supporting state**

In `akosha/storage/cold_store.py`, replace line 36 (`self._storage_adapter: Any | None = None`) with:

```python
from typing import Literal

from oneiric.storage import S3StorageAdapter  # noqa: F401  (verified in Task 1.1)


class ColdStore:
    def __init__(
        self,
        *,
        storage_backend: Literal["memory", "s3", "r2"] = "memory",
        bucket: str | None = None,
        endpoint_url: str | None = None,
        local_dir: Path | None = None,
    ) -> None:
        self._storage_backend = storage_backend
        self._bucket = bucket
        self._endpoint_url = endpoint_url
        self._local_dir = local_dir or Path.home() / ".akosha" / "cold-store"
        self._adapter: Any = None
```

- [ ] **Step 4: Implement `ColdStore.initialize()` and `ColdStore.close()`**

Replace lines 207-212 in `akosha/storage/cold_store.py`:

```python
    async def initialize(self) -> None:
        """Construct the underlying storage adapter. Idempotent."""
        if self._adapter is not None:
            return
        if self._storage_backend == "memory":
            self._local_dir.mkdir(parents=True, exist_ok=True)
            self._adapter = _LocalDirAdapter(self._local_dir)
        elif self._storage_backend in {"s3", "r2"}:
            if not self._bucket:
                raise ValueError(f"bucket required for {self._storage_backend} backend")
            self._adapter = S3StorageAdapter(
                bucket=self._bucket,
                endpoint_url=self._endpoint_url,
            )
            await self._adapter.initialize()
        else:
            raise ValueError(f"Unknown storage_backend: {self._storage_backend}")

    async def close(self) -> None:
        """Close the underlying adapter. Idempotent."""
        if self._adapter is not None:
            await self._adapter.close()
            self._adapter = None
```

- [ ] **Step 5: Implement `_LocalDirAdapter` helper class (memory backend)**

Append to `akosha/storage/cold_store.py`:

```python
class _LocalDirAdapter:
    """In-memory-ish backend: writes Parquet files to local_dir."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def initialize(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)

    async def upload(self, *, temp_path: Path, object_key: str) -> dict[str, Any]:
        target = self._root / object_key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(temp_path.read_bytes())
        temp_path.unlink(missing_ok=True)
        return {"bytes_written": target.stat().st_size}

    async def close(self) -> None:
        return None
```

- [ ] **Step 6: Replace `_upload_to_storage()` (lines 183-205)**

```python
    async def _upload_to_storage(self, temp_path: Path, object_key: str) -> None:
        if self._adapter is None:
            raise RuntimeError("ColdStore.initialize() not called")
        try:
            result = await self._adapter.upload(temp_path=temp_path, object_key=object_key)
            logger.info(
                "ColdStore uploaded object_key=%s bytes=%s",
                object_key,
                result.get("bytes_written", 0),
            )
        except Exception:
            logger.exception("ColdStore upload failed for object_key=%s", object_key)
            raise
```

- [ ] **Step 7: Update `export_batch()` to call `initialize()` and `close()` (line 71 area)**

In `ColdStore.export_batch()`, before the existing Parquet write logic, add:

```python
        await self.initialize()
        try:
            # ... existing export logic, ends with _upload_to_storage call ...
        finally:
            await self.close()
```

(Read lines 50-110 of the current file to splice correctly; preserve all existing Parquet-write code.)

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/test_cold_store.py -v`
Expected: 3 passed.

- [ ] **Step 9: Run full suite to verify no regression**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest -q -m "not slow"`
Expected: ≥2,020 passed (current count) plus the 3 new tests. No regressions in existing tests.

- [ ] **Step 10: Commit**

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add akosha/storage/cold_store.py tests/unit/test_cold_store.py
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "fix(akosha): ColdStore.export_batch now actually uploads (was silent no-op)

audit C1: _upload_to_storage logged 'Would upload ...' and deleted the temp
file, causing silent data loss for any caller routing Parquet exports to
S3/R2. Add storage_backend (memory/s3/r2) + initialize()/close() lifecycle,
LocalDirAdapter for dev, oneiric.storage.S3StorageAdapter for cloud. Tests
in test_cold_store.py cover local write + adapter construction + close."
```

## Task 1.3: `/health` real probe (C2)

**Files:**

- Modify: `akosha/mcp/server.py:419-432`
- Create: `akosha/cli.py` modification to export `_health_probe` as `health_probe` (public alias)
- Test: `tests/unit/test_mcp_health.py` (new) + extension to existing `tests/unit/cli/test_health.py`

**Interfaces:**

- Consumes: `AkoshaApplication._check_dependency_health()` (new method on existing class).
- Produces:
  ```python
  # akosha/mcp/server.py
  async def health_check(request: Any) -> JSONResponse: ...  # 200 if healthy, 503 if degraded
  async def healthz_check(request: Any) -> JSONResponse: ...  # 200 only if AkoshaApplication.start() completed

  # akosha/cli.py
  async def health_probe(app: AkoshaApplication) -> dict[str, Any]: ...
  ```

- [ ] **Step 1: Write failing test for HTTP `/health` returning 503 on degraded state**

Create `tests/unit/test_mcp_health.py`:

```python
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from akosha.mcp.server import build_app


@pytest.fixture
def mock_app() -> MagicMock:
    app = MagicMock()
    app._check_dependency_health = AsyncMock(return_value={
        "hot_store": {"ok": True},
        "dhara": {"ok": False, "error": "connection refused"},
        "websocket": {"ok": True},
    })
    return app


def test_health_returns_503_when_dhara_unreachable(mock_app: MagicMock) -> None:
    """C2: /health must NOT lie — return 503 when any dep is down."""
    server = build_app(mock_app)
    client = TestClient(server)
    response = client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["dhara"]["ok"] is False


def test_health_returns_200_when_all_deps_ok(mock_app: MagicMock) -> None:
    mock_app._check_dependency_health = AsyncMock(return_value={
        "hot_store": {"ok": True},
        "dhara": {"ok": True},
        "websocket": {"ok": True},
    })
    server = build_app(mock_app)
    client = TestClient(server)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/test_mcp_health.py -v`
Expected: 2 failures with "build_app signature mismatch" or "/health always returns 200".

- [ ] **Step 3: Add `_check_dependency_health` method to `AkoshaApplication` in `akosha/main.py`**

Find the `AkoshaApplication` class and add after `close()`:

```python
    async def _check_dependency_health(self) -> dict[str, dict[str, Any]]:
        """Probe HotStore, Dhara, WebSocket subscriber. Returns per-dep status dict."""
        checks: dict[str, dict[str, Any]] = {}
        checks["hot_store"] = await self._check_hot_store()
        if self._dhara_client is not None:
            checks["dhara"] = await self._check_dhara()
        if self._ws_subscriber is not None:
            checks["websocket"] = {"ok": self._ws_subscriber.is_running()}
        return checks

    async def _check_hot_store(self) -> dict[str, Any]:
        if self._hot_store is None:
            return {"ok": False, "error": "not initialized"}
        try:
            await self._hot_store.ping()
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def _check_dhara(self) -> dict[str, Any]:
        try:
            await self._dhara_client.ping()
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
```

(Adapt attribute names to match the actual `AkoshaApplication` fields; read `akosha/main.py` first.)

- [ ] **Step 4: Replace `/health` and `/healthz` handlers in `akosha/mcp/server.py:419-432`**

```python
@app.custom_route("/health", methods=["GET"])
async def health_check(request: Any) -> JSONResponse:
    app_instance = request.app.state.akosha_app  # type: ignore[attr-defined]
    checks = await app_instance._check_dependency_health()
    all_ok = all(c.get("ok", False) for c in checks.values())
    body = {"status": "ok" if all_ok else "degraded", "checks": checks}
    return JSONResponse(body, status_code=200 if all_ok else 503)


@app.custom_route("/healthz", methods=["GET"])
async def healthz_check(request: Any) -> JSONResponse:
    return JSONResponse({"status": "ok"})
```

(Verify `request.app.state.akosha_app` is set in `AkoshaApplication.start()`; if not, add the assignment there.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/test_mcp_health.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add akosha/mcp/server.py akosha/main.py tests/unit/test_mcp_health.py
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "fix(akosha): /health now probes dependencies (was hardcoded OK)

audit C2: /health and /healthz returned {'status': 'ok'} regardless of
HotStore/Dhara/WebSocket state, defeating Kubernetes liveness probes.
Replace with _check_dependency_health() that returns 200 only when all
deps pass, 503 + structured body otherwise. /healthz stays as process-
liveness-only."
```

## Task 1.4: IPython shell stub surface (C3)

**Files:**

- Modify: `akosha/shell/adapter.py:115-261`
- Test: `tests/unit/test_shell_adapter.py` (new)

**Interfaces:**

- Produces: every `AkoshaShell._X` method now returns `{"status": "stub", "command": "X", "message": "..."}` instead of `{"status": "success", "count": 0, ...}`.

- [ ] **Step 1: Write failing test in `tests/unit/test_shell_adapter.py`**

```python
from __future__ import annotations

from akosha.shell.adapter import AkoshaShell


def test_aggregate_returns_stub_status() -> None:
    shell = AkoshaShell()
    result = shell._aggregate({"query": "foo"})
    assert result["status"] == "stub"
    assert result["command"] == "aggregate"


def test_search_returns_stub_status() -> None:
    shell = AkoshaShell()
    result = shell._search({"query": "foo"})
    assert result["status"] == "stub"


def test_detect_returns_stub_status() -> None:
    shell = AkoshaShell()
    result = shell._detect({"query": "foo"})
    assert result["status"] == "stub"


def test_graph_returns_stub_status() -> None:
    shell = AkoshaShell()
    result = shell._graph({"query": "foo"})
    assert result["status"] == "stub"


def test_trends_returns_stub_status() -> None:
    shell = AkoshaShell()
    result = shell._trends({"query": "foo"})
    assert result["status"] == "stub"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/test_shell_adapter.py -v`
Expected: 5 failures, each asserting `status == "success"` (the current fake value).

- [ ] **Step 3: Replace the five stub method bodies in `akosha/shell/adapter.py`**

For each of `_aggregate`, `_search`, `_detect`, `_graph`, `_trends` at lines 115-261, replace the body:

```python
    def _aggregate(self, args: dict) -> dict:
        """Aggregate query across systems. Stub until Wave 5 wires data feeds."""
        return {
            "status": "stub",
            "command": "aggregate",
            "message": "Not yet implemented; tracked in docs/feature-tracking/2026-09-05-akosha-hardening.md",
            "args": args,
        }
```

(Repeat for the other four commands with `command: "search"`, `"detect"`, `"graph"`, `"trends"`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/test_shell_adapter.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add akosha/shell/adapter.py tests/unit/test_shell_adapter.py
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "fix(akosha): IPython shell commands surface 'stub' instead of fake success

audit C3: Five shell commands (_aggregate, _search, _detect, _graph,
_trends) returned {'status': 'success', 'count': 0} with TODO comments.
Operators couldn't distinguish empty results from unwired implementations.
Replace with explicit 'stub' status + feature-tracking pointer."
```

## Task 1.5: Version-sync test fix + aiohttp dep declaration (M1)

**Files:**

- Modify: `akosha/__init__.py:__version__` OR `tests/unit/test_version_sync.py` (whichever is stale — read first)
- Modify: `pyproject.toml:11-40`
- Create: `tests/unit/test_dependencies.py`
- Modify: `uv.lock`

**Interfaces:**

- Consumes: current `pyproject.toml [project] version`, current `akosha/__init__.py:__version__`.
- Produces: tests pass; `pyproject.toml` declares `aiohttp>=3.12.14`.

- [ ] **Step 1: Diagnose version drift**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/test_version_sync.py -v 2>&1 | tail -30`
Expected: 5 failures showing the asserted value vs the current value. **Read the test first** to understand which side is canonical.

- [ ] **Step 2: Reconcile**

If `akosha/__init__.py:__version__` is the source of truth (most likely), update the test constant to match. If the test is enforcing a constraint (e.g. "version must be 0.14.x"), update the source. Add a regression test pinning them in sync:

Append to `tests/unit/test_version_sync.py`:

```python
from akosha import __version__
import tomllib
from pathlib import Path


def test_pyproject_version_matches_package_version() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    assert pyproject["project"]["version"] == __version__
```

- [ ] **Step 3: Run regression test**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/test_version_sync.py -v`
Expected: 6 passed (5 original + 1 new regression).

- [ ] **Step 4: Declare `aiohttp>=3.12.14` in `pyproject.toml`**

Find `[project]` `dependencies = [...]` block. Add:

```toml
    "aiohttp>=3.12.14",
```

Run: `cd /Users/les/Projects/akosha && uv lock`
Expected: lockfile updated with `aiohttp>=3.12.14`.

- [ ] **Step 5: Write guard test in `tests/unit/test_dependencies.py`**

```python
from __future__ import annotations

import re
from pathlib import Path


PYPROJECT = Path("pyproject.toml")


def test_aiohttp_declared_in_dependencies() -> None:
    """audit M1: aiohttp is imported but was previously undeclared."""
    content = PYPROJECT.read_text()
    match = re.search(r'aiohttp[><=~]+\s*[\d.]+', content)
    assert match is not None, "aiohttp must be declared in pyproject.toml [project.dependencies]"


def test_aiohttp_import_sites_have_pin() -> None:
    """Every aiohttp import must be in a file that imports after the dep pin."""
    akosha_dir = Path("akosha")
    imports_found = list(akosha_dir.rglob("*.py"))
    for f in imports_found:
        if "import aiohttp" in f.read_text():
            assert PYPROJECT.exists(), "pyproject.toml must declare aiohttp"
```

- [ ] **Step 6: Run guard tests**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/test_dependencies.py -v`
Expected: 2 passed.

- [ ] **Step 7: Commit**

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add akosha/__init__.py tests/unit/test_version_sync.py tests/unit/test_dependencies.py pyproject.toml uv.lock
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "fix(akosha): version-sync test + declare aiohttp>=3.12.14 in pyproject

audit M1 + version drift: aiohttp was imported in
akosha/api/middleware.py:116 but never declared in
[project.dependencies]. Add pin + guard test. Reconcile version
drift surfaced by 5 broken test_version_sync tests; add regression
test pinning pyproject.toml and akosha.__init__.__version__ in sync."
```

## Task 1.6: Wave 1 feature-tracking entry + audit-orphans run

**Files:**

- Create: `docs/feature-tracking/2026-09-05-akosha-hardening.md`
- Modify: `pyproject.toml` (bump version 0.14.3 → 0.14.4 for Wave 1)
- Modify: `akosha/__init__.py:__version__`

- [ ] **Step 1: Run audit-orphans.py**

Run: `cd /Users/les/Projects/akosha && .venv/bin/python scripts/audit_orphans.py 2>&1 | tail -30`
Expected: zero reports of Wave 1 symbols being unused (ColdStore.initialize/close, _check_dependency_health, stub shell commands, _check_dependency_health on AkoshaApplication).

- [ ] **Step 2: Bump version to 0.14.4**

In `pyproject.toml` `[project]` section: `"version" = "0.14.4"`. In `akosha/__init__.py:__version__ = "0.14.4"`. Run `uv lock`.

- [ ] **Step 3: Write feature-tracking entry**

Create `docs/feature-tracking/2026-09-05-akosha-hardening.md`:

```markdown
---
built: 2026-09-05
wired: 2026-09-05
adopted: 2026-09-05
phase: convergence-control-plane
topic: akosha-hardening
---

# Akosha Comprehensive Hardening — Wave 1 (P0)

## What

Three P0 critical bugs fixed and one M1 dependency hygiene item:

1. **C1**: `ColdStore.export_batch()` no longer silent-data-loss — real upload via `storage_backend: "memory"|"s3"|"r2"` lifecycle.
2. **C2**: `/health` HTTP route now probes HotStore/Dhara/WebSocket — returns 503 when degraded.
3. **C3**: IPython admin shell five commands now return `{"status": "stub", ...}` instead of fake success.
4. **M1**: `aiohttp>=3.12.14` declared in `pyproject.toml`.

Plus: 5 broken `tests/unit/test_version_sync.py::*` tests reconciled; regression test added pinning pyproject version and `akosha.__version__` in sync.

## Why

Audit on 2026-09-05 found production data-loss path (ColdStore), misleading health endpoints defeating K8s liveness probes, and operators getting fake "success" from shell commands. Each was a single root-cause change but represented material reliability gaps.

## Test coverage

New test files:
- `tests/unit/test_cold_store.py` — 3 tests (local write, adapter construction, close)
- `tests/unit/test_mcp_health.py` — 2 tests (200 OK, 503 degraded)
- `tests/unit/test_shell_adapter.py` — 5 tests (one per stubbed command)
- `tests/unit/test_dependencies.py` — 2 tests (aiohttp declared, import sites covered)
- `tests/unit/test_version_sync.py` — 5 reconciled + 1 regression test

Total: 17 new test cases across 4 new test files + 1 regression.

## Followups

- [ ] Wave 2: P1 high bugs + zero-coverage module tests (similarity, dedup, quantization, 9 modules).
- [ ] Wave 3: P2 medium bugs + coverage to 90%.
- [ ] Wave 4: 382 empty-test rewrites.
- [ ] Wave 5: Live MCP backend wiring + 0.15.0 bump.
```

- [ ] **Step 4: Commit**

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add docs/feature-tracking/2026-09-05-akosha-hardening.md pyproject.toml akosha/__init__.py uv.lock
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "chore(akosha): bump to 0.14.4 + wave-1 feature-tracking entry"
```

---

# Wave 2 — P1 High Bugs + Zero-Coverage Module Tests

## Task 2.1: Similarity error propagation (H1)

**Files:**

- Modify: `akosha/mcp/tools/code_graph_tools.py:274-308`
- Test: extension to `tests/unit/mcp/tools/test_code_graph_tools.py` (existing file)

**Interfaces:**

- Produces: `_compute_graph_similarity(graph1, graph2) -> float` raises `RuntimeError` on unexpected errors; `0.0` only when both graphs are empty.

- [ ] **Step 1: Write failing test**

Append to `tests/unit/mcp/tools/test_code_graph_tools.py`:

```python
import pytest

from akosha.mcp.tools.code_graph_tools import _compute_graph_similarity


@pytest.mark.asyncio
async def test_similarity_propagates_runtime_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """audit H1: similarity must propagate errors instead of swallowing to 0.0."""
    def explode(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr("akosha.mcp.tools.code_graph_tools._compute_graph_features", explode)
    graph1 = {"types": ["Foo"]}
    graph2 = {"types": ["Bar"]}
    with pytest.raises(RuntimeError, match="boom"):
        await _compute_graph_similarity(graph1, graph2)


@pytest.mark.asyncio
async def test_similarity_zero_on_empty_types() -> None:
    """Empty types produce 0.0 — the only path that legitimately returns 0."""
    result = await _compute_graph_similarity({"types": []}, {"types": []})
    assert result == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/mcp/tools/test_code_graph_tools.py -v`
Expected: `test_similarity_propagates_runtime_errors` fails because current code returns 0.0 on the `RuntimeError`.

- [ ] **Step 3: Refactor `_compute_graph_similarity`**

In `akosha/mcp/tools/code_graph_tools.py`, replace lines 274-308:

```python
async def _compute_graph_similarity(graph1: dict, graph2: dict) -> float:
    """Cosine similarity over graph node types. Empty → 0.0; errors propagate."""
    types1 = set(graph1.get("types", []))
    types2 = set(graph2.get("types", []))
    if not types1 or not types2:
        return 0.0
    # Build feature vectors (assumes both graphs use the same vocabulary)
    vocab = sorted(types1 | types2)
    vec1 = [1.0 if t in types1 else 0.0 for t in vocab]
    vec2 = [1.0 if t in types2 else 0.0 for t in vocab]
    dot_product = sum(a * b for a, b in zip(vec1, vec2, strict=True))
    norm1 = sum(a * a for a in vec1) ** 0.5
    norm2 = sum(b * b for b in vec2) ** 0.5
    if norm1 == 0.0 or norm2 == 0.0:
        return 0.0
    return float(dot_product / (norm1 * norm2))
```

(No try/except wrapper. Errors propagate naturally.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/mcp/tools/test_code_graph_tools.py -v`
Expected: both new tests pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add akosha/mcp/tools/code_graph_tools.py tests/unit/mcp/tools/test_code_graph_tools.py
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "fix(akosha): _compute_graph_similarity propagates errors (was zero-on-error)

audit H1: bare 'except Exception: return 0.0' made every error path
indistinguishable from 'two graphs with zero overlap'. Refactor:
explicit empty-types check returns 0.0; all other errors propagate.
Removes the silent-suppression of refactor-cluster signals."
```

## Task 2.2: Real deduplication with MinHash (H2)

**Files:**

- Modify: `akosha/processing/deduplication.py:62-84` (replace `compute_fingerprint` and `find_similar`)
- Test: `tests/unit/processing/test_deduplication.py` (new)

**Interfaces:**

- Produces:
  ```python
  class DeduplicationService:
      def __init__(self, *, backend: Literal["minhash", "sha256"] = "minhash",
                   num_perm: int = 128, threshold: float = 0.5) -> None: ...
      def compute_fingerprint(self, content: str) -> bytes: ...
      def find_similar(self, fingerprint: bytes, candidates: list[bytes]) -> list[tuple[int, float]]: ...
  ```

- [ ] **Step 1: Write failing tests in `tests/unit/processing/test_deduplication.py`**

```python
from __future__ import annotations

import pytest

from akosha.processing.deduplication import DeduplicationService


def test_compute_fingerprint_returns_deterministic_bytes() -> None:
    service = DeduplicationService()
    fp1 = service.compute_fingerprint("hello world")
    fp2 = service.compute_fingerprint("hello world")
    assert fp1 == fp2
    assert len(fp1) > 0


def test_find_similar_returns_matches_above_threshold() -> None:
    service = DeduplicationService(threshold=0.5)
    fp1 = service.compute_fingerprint("the quick brown fox jumps over the lazy dog")
    fp2 = service.compute_fingerprint("the quick brown fox jumps over the lazy dog")  # identical
    fp3 = service.compute_fingerprint("completely different text about nothing related")
    matches = service.find_similar(fp1, [fp2, fp3])
    assert len(matches) >= 1
    assert matches[0][0] == 0  # fp2 is the first candidate
    assert matches[0][1] >= 0.99  # near-identical


def test_sha256_backend_fallback_works() -> None:
    """HasherFallback path: SHA-256 + exact-match similarity."""
    service = DeduplicationService(backend="sha256", threshold=0.5)
    fp1 = service.compute_fingerprint("hello")
    fp2 = service.compute_fingerprint("hello")
    fp3 = service.compute_fingerprint("world")
    matches = service.find_similar(fp1, [fp2, fp3])
    assert (0, 1.0) in matches or any(m[0] == 0 and m[1] >= 0.99 for m in matches)
```

- [ ] **Step 2: Add `datasketch` to pyproject.toml**

In `[project.dependencies]`:

```toml
    "datasketch>=0.6.0",
```

Run: `cd /Users/les/Projects/akosha && uv lock`

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/processing/test_deduplication.py -v`
Expected: 3 failures — current `find_similar` always returns `[]`.

- [ ] **Step 4: Implement `DeduplicationService` in `akosha/processing/deduplication.py`**

Replace lines 62-84 (the `compute_fingerprint` and `find_similar` methods) with:

```python
from __future__ import annotations

import hashlib
from typing import Literal

from datasketch import MinHash


class DeduplicationService:
    """Real deduplication via MinHash (default) or SHA-256 + exact match (fallback)."""

    def __init__(
        self,
        *,
        backend: Literal["minhash", "sha256"] = "minhash",
        num_perm: int = 128,
        threshold: float = 0.5,
    ) -> None:
        self._backend = backend
        self._num_perm = num_perm
        self._threshold = threshold

    def compute_fingerprint(self, content: str) -> bytes:
        if self._backend == "minhash":
            m = MinHash(num_perm=self._num_perm)
            for word in content.split():
                m.update(word.encode("utf-8"))
            return m.hashbytes
        return hashlib.sha256(content.encode("utf-8")).digest()

    def find_similar(
        self, fingerprint: bytes, candidates: list[bytes]
    ) -> list[tuple[int, float]]:
        matches: list[tuple[int, float]] = []
        if self._backend == "minhash":
            m = MinHash(num_perm=self._num_perm, hashbytes=fingerprint)
            for idx, cand_bytes in enumerate(candidates):
                cand = MinHash(num_perm=self._num_perm, hashbytes=cand_bytes)
                jaccard = m.jaccard(cand)
                if jaccard >= self._threshold:
                    matches.append((idx, jaccard))
        else:
            target = fingerprint
            for idx, cand in enumerate(candidates):
                if cand == target:
                    matches.append((idx, 1.0))
        return matches
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/processing/test_deduplication.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add akosha/processing/deduplication.py tests/unit/processing/test_deduplication.py pyproject.toml uv.lock
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "feat(akosha): real MinHash deduplication (was SHA-256 no-op)

audit H2: DeduplicationService.compute_fingerprint used SHA-256 as
placeholder; find_similar always returned []. Implement via datasketch
MinHash (default) with SHA-256 fallback. add datasketch>=0.6.0 dep."
```

## Task 2.3: INT8 quantization correctness (H3)

**Files:**

- Modify: `akosha/storage/aging.py:291-309`
- Modify: `akosha/storage/models.py` (add `scale: float` field if metadata struct lives here)
- Test: `tests/unit/storage/test_aging.py` (new)

**Interfaces:**

- Produces:
  ```python
  class QuantizedVector(NamedTuple):
    values: list[int]   # in [-127, 127]
    scale: float        # 127 / max(abs(original))

  def quantize_embedding(embedding: list[float]) -> QuantizedVector: ...
  def dequantize(q: QuantizedVector) -> list[float]: ...
  ```

- [ ] **Step 1: Read `akosha/storage/aging.py:280-310` to understand the existing quantize signature**

- [ ] **Step 2: Write failing tests in `tests/unit/storage/test_aging.py`**

```python
from __future__ import annotations

import math

from akosha.storage.aging import quantize_embedding, dequantize


def test_quantize_roundtrip_within_eps() -> None:
    """audit H3: quantization must be reversible within 1/127."""
    emb = [0.1, 0.5, -0.3, 0.8, -0.9]
    q = quantize_embedding(emb)
    restored = dequantize(q)
    for orig, rec in zip(emb, restored, strict=True):
        assert abs(orig - rec) <= 1 / 127 + 1e-9


def test_quantize_clamps_to_int8_range() -> None:
    """Values outside [-1, 1] (after scaling) must clip to [-127, 127]."""
    emb = [2.0, -3.0, 0.5]  # scale = 127 / 3.0 ≈ 42.33
    q = quantize_embedding(emb)
    for v in q.values:
        assert -127 <= v <= 127


def test_quantize_preserves_zero() -> None:
    emb = [0.0, 0.0, 0.0]
    q = quantize_embedding(emb)
    # Scale undefined on all-zero; special-case to scale=1
    assert q.scale == 1.0
    assert all(v == 0 for v in q.values)


def test_quantize_scale_is_persisted() -> None:
    """Scale must be retrievable for dequantization."""
    emb = [0.5, -0.5]
    q = quantize_embedding(emb)
    assert q.scale > 0
    assert math.isclose(q.scale, 127 / max(abs(v) for v in emb), rel_tol=1e-6)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/storage/test_aging.py -v`
Expected: 4 failures — current `_quantize_embedding` returns `list[int]` with no scale persistence.

- [ ] **Step 4: Implement `quantize_embedding` and `dequantize` in `akosha/storage/aging.py`**

Replace lines 291-309:

```python
from typing import NamedTuple


class QuantizedVector(NamedTuple):
    values: list[int]
    scale: float


def quantize_embedding(embedding: list[float]) -> QuantizedVector:
    """Quantize float embedding to INT8 with clipping and scale persistence."""
    if not embedding:
        return QuantizedVector(values=[], scale=1.0)
    max_abs = max(abs(v) for v in embedding)
    if max_abs == 0.0:
        return QuantizedVector(values=[0] * len(embedding), scale=1.0)
    scale = 127.0 / max_abs
    values = [max(-127, min(127, round(v * scale))) for v in embedding]
    return QuantizedVector(values=values, scale=scale)


def dequantize(q: QuantizedVector) -> list[float]:
    """Reverse quantization using the persisted scale."""
    return [v / q.scale for v in q.values]


async def _quantize_embedding(self, float_embedding: list[float]) -> QuantizedVector:
    return quantize_embedding(float_embedding)
```

(Adapt `self._quantize_embedding` signature if callers expect `list[int]`; if so, add a deprecation shim returning `[v for v in q.values]` and document the migration in Wave 3.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/storage/test_aging.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add akosha/storage/aging.py akosha/storage/models.py tests/unit/storage/test_aging.py
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "fix(akosha): INT8 quantization with clipping + scale persistence

audit H3: int(v * 127) without clipping produced values outside INT8
range; missing scale factor made two semantically similar vectors
produce different scales. Implement quantize_embedding/dequantize
with max(abs) scale + [-127, 127] clipping. Round-trip error < 1/127."
```

## Task 2.4: Test files for 9 zero-coverage modules

**Files:**

- Create: 7 new test files (Tasks 2.4a–2.4g)

For each module listed in the audit's zero-coverage table, write a test file that exercises the public surface. TDD pattern: read the module, write failing tests against public classes/functions, run to confirm they fail, commit.

### Task 2.4a: `tests/unit/processing/test_fitness_analyzer.py`

**Files:**

- Create: `tests/unit/processing/test_fitness_analyzer.py`

- [ ] **Step 1: Read `akosha/processing/fitness_analyzer.py` to identify the public surface**

- [ ] **Step 2: Write ≥10 tests covering the per-(task_class, selector) signal computation**

Pattern (10 tests):

```python
from __future__ import annotations

import pytest

from akosha.processing.fitness_analyzer import FitnessAnalyzer


@pytest.fixture
def analyzer() -> FitnessAnalyzer:
    return FitnessAnalyzer()


@pytest.mark.asyncio
async def test_compute_failure_rate_with_no_traces(analyzer: FitnessAnalyzer) -> None:
    rate = await analyzer.compute_failure_rate(task_class="code_generation", selector="least_loaded")
    assert rate == 0.0


@pytest.mark.asyncio
async def test_compute_p99_latency_with_no_traces(analyzer: FitnessAnalyzer) -> None:
    latency = await analyzer.compute_p99_latency(task_class="code_generation", selector="least_loaded")
    assert latency == 0.0


# ... 8 more tests covering: trace ingestion, sliding window, percentile
# computation, anomaly threshold, persistence to Dhara, OTel hook,
# empty-corpus behavior, malformed trace handling ...
```

- [ ] **Step 3: Run tests**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/processing/test_fitness_analyzer.py -v`
Expected: ≥10 passed (some will fail if the module's public surface differs; adapt to match the actual API).

- [ ] **Step 4: Commit**

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add tests/unit/processing/test_fitness_analyzer.py
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "test(akosha): add fitness_analyzer tests (was zero coverage)"
```

(Repeat the same TDD pattern for Tasks 2.4b–2.4g.)

### Task 2.4b: `tests/unit/mcp/test_client.py` (≥8 tests)

### Task 2.4c: `tests/unit/cli/commands/test_migrate.py` (≥8 tests — distinct from existing `test_migrate_cli.py`)

### Task 2.4d: `tests/unit/mcp/tools/test_fitness_tools.py` (≥6 tests)

### Task 2.4e: `tests/unit/mcp/tools/test_tool_registry.py` (≥6 tests)

### Task 2.4f: `tests/unit/observability/test_eventbridge_adapter.py` (≥4 tests with real assertions, replacing the 1-test no-op)

### Task 2.4g: `tests/unit/test_main_entrypoints.py` (≥4 smoke tests via subprocess)

```python
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_akosha_main_module_imports() -> None:
    """python -m akosha must invoke without import errors."""
    result = subprocess.run(
        [sys.executable, "-m", "akosha", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    # --help exits 0; non-zero would indicate an import or argparse error
    assert result.returncode == 0, f"stderr: {result.stderr}"


def test_akosha_mcp_main_module_imports() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "akosha.mcp", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"


def test_akosha_main_module_version() -> None:
    """python -m akosha --version prints the package version."""
    result = subprocess.run(
        [sys.executable, "-m", "akosha", "--version"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "0." in result.stdout or "0." in result.stderr  # any 0.x.y version
```

- [ ] **Step 8: Run all Wave 2 new tests**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/processing/test_fitness_analyzer.py tests/unit/mcp/test_client.py tests/unit/cli/commands/test_migrate.py tests/unit/mcp/tools/test_fitness_tools.py tests/unit/mcp/tools/test_tool_registry.py tests/unit/observability/test_eventbridge_adapter.py tests/unit/test_main_entrypoints.py -v`
Expected: all passing.

- [ ] **Step 9: Run full suite with coverage**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest --cov=akosha --cov-report=term-missing -q -m "not slow"`
Expected: total coverage ≥85% (up from 84.79%); new modules at ≥60%.

- [ ] **Step 10: Wave 2 feature-tracking entry + commit**

Append to `docs/feature-tracking/2026-09-05-akosha-hardening.md`:

```markdown
## Wave 2 (P1) — built: 2026-09-05

3 P1 high bugs fixed + tests for 9 zero-coverage modules:

1. **H1**: `_compute_graph_similarity` propagates errors.
2. **H2**: `DeduplicationService` uses MinHash (was SHA-256 no-op).
3. **H3**: INT8 quantization has clipping + scale persistence.
4. Zero-coverage tests: fitness_analyzer, mcp/client, cli/commands/migrate, fitness_tools, tool_registry, eventbridge_adapter, main_entrypoints.
```

Bump version to 0.14.5. Run `uv lock`. Commit:

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add docs/feature-tracking/2026-09-05-akosha-hardening.md pyproject.toml akosha/__init__.py uv.lock
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "chore(akosha): wave-2 done — bump to 0.14.5 + feature-tracking update"
```

---

# Wave 3 — P2 Medium Bugs + Coverage to 90%

## Task 3.1: CLI version error specificity (M2)

**Files:**

- Modify: `akosha/cli.py:474-480`
- Test: `tests/unit/test_cli_version.py` (new)

**Interfaces:**

- Produces: `akosha version` raises a clean error (not silent "unknown") on broken install.

- [ ] **Step 1: Write failing test**

```python
from __future__ import annotations

from click.testing import CliRunner

from akosha.cli import cli


def test_version_surfaces_traceback_on_broken_install(monkeypatch) -> None:
    """audit M2: version command must surface errors, not print 'unknown'."""
    def explode(_pkg):
        raise RuntimeError("metadata broken")
    monkeypatch.setattr("importlib.metadata.version", explode)
    runner = CliRunner()
    result = runner.invoke(cli, ["version"])
    # Traceback appears in stderr; exit code non-zero
    assert result.exit_code != 0
    assert "metadata broken" in result.output or "metadata broken" in (result.stderr or "")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/test_cli_version.py -v`
Expected: current implementation suppresses all errors and exits 0 with "unknown".

- [ ] **Step 3: Narrow the except**

In `akosha/cli.py:474-480`, replace:

```python
import importlib.metadata

from typer import BadParameter


@app.command()
def version() -> None:
    """Print Akosha version."""
    try:
        ver = importlib.metadata.version("akosha")
    except importlib.metadata.PackageNotFoundError:
        raise BadParameter(
            "Akosha is not installed; cannot determine version. "
            "Reinstall with `uv pip install -e .`"
        ) from None
    typer.echo(f"Akosha version: {ver}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/test_cli_version.py -v`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add akosha/cli.py tests/unit/test_cli_version.py
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "fix(akosha): CLI version surfaces PackageNotFoundError as BadParameter (was silent 'unknown')

audit M2: 'except Exception: typer.echo(unknown)' hid install errors.
Now catches only PackageNotFoundError and raises BadParameter with a
remediation hint; other exceptions propagate naturally."
```

## Task 3.2: Orchestrator real health probe (M3)

**Files:**

- Modify: `akosha/ingestion/orchestrator.py:71-84`
- Test: `tests/unit/ingestion/test_orchestrator_health.py` (new)

**Interfaces:**

- Produces: `BootstrapOrchestrator.report_health()` returns dict with `last_actual_ping` field populated by an active ping (not just `last_heartbeat`).

- [ ] **Step 1: Write failing test**

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from akosha.ingestion.orchestrator import BootstrapOrchestrator


@pytest.mark.asyncio
async def test_report_health_pings_mahavishnu(monkeypatch: pytest.MonkeyPatch) -> None:
    """audit M3: report_health must do a real ping, not just report last heartbeat."""
    fake_client = MagicMock()
    fake_client.ping = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr("akosha.ingestion.orchestrator.create_mahavishnu_client", lambda: fake_client)
    orch = BootstrapOrchestrator()
    health = await orch.report_health()
    assert "last_actual_ping" in health
    assert health["last_actual_ping"] is not None
    fake_client.ping.assert_awaited()


@pytest.mark.asyncio
async def test_report_health_reports_degraded_on_ping_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = MagicMock()
    fake_client.ping = AsyncMock(side_effect=ConnectionError("unreachable"))
    monkeypatch.setattr("akosha.ingestion.orchestrator.create_mahavishnu_client", lambda: fake_client)
    orch = BootstrapOrchestrator()
    health = await orch.report_health()
    assert health["status"] == "degraded"
    assert "unreachable" in health["ping_error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/ingestion/test_orchestrator_health.py -v`
Expected: 2 failures — current `report_health` returns `last_heartbeat` without `last_actual_ping`.

- [ ] **Step 3: Implement real ping**

Replace `akosha/ingestion/orchestrator.py:71-84`:

```python
    async def report_health(self) -> dict[str, Any]:
        """Active health probe. Pings mahavishnu and returns real-time status."""
        result: dict[str, Any] = {
            "fallback_mode": self.fallback_mode,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        try:
            client = await self._get_mahavishnu_client()
            ping_result = await client.ping()
            result["status"] = "fallback" if self.fallback_mode else "normal"
            result["last_actual_ping"] = datetime.now(UTC).isoformat()
            result["ping_result"] = ping_result
        except Exception as exc:
            result["status"] = "degraded"
            result["ping_error"] = str(exc)
            result["last_actual_ping"] = None
        return result
```

(Adapt to actual orchestrator structure; read `akosha/ingestion/orchestrator.py` first.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/ingestion/test_orchestrator_health.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add akosha/ingestion/orchestrator.py tests/unit/ingestion/test_orchestrator_health.py
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "fix(akosha): BootstrapOrchestrator.report_health does real ping (was self-attestation)

audit M3: report_health returned last_heartbeat without verifying reachability.
Now actively pings the mahavishnu client and reports 'degraded' on failure."
```

## Task 3.3: Health probe consistency (M4)

**Files:**

- Modify: `akosha/mcp/server.py:419-432` (already uses `_check_dependency_health` after Task 1.3; verify both surfaces return identical shape)
- Test: `tests/unit/test_health_consistency.py` (new)

- [ ] **Step 1: Write parity test**

```python
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from akosha.cli import _health_probe
from akosha.mcp.server import build_app


@pytest.mark.asyncio
async def test_cli_and_http_health_return_same_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """audit M4: HTTP /health and CLI 'akosha health' must agree on shape."""
    fake_app = MagicMock()
    fake_app._check_dependency_health = AsyncMock(return_value={
        "hot_store": {"ok": True},
        "dhara": {"ok": True},
        "websocket": {"ok": False, "error": "subscriber not started"},
    })
    cli_result = await _health_probe(fake_app)
    assert cli_result["checks"].keys() == {"hot_store", "dhara", "websocket"}
    assert "status" in cli_result
```

- [ ] **Step 2: If `_health_probe` is private, promote it to a public alias**

In `akosha/cli.py`, add after the `_health_probe` definition:

```python
health_probe = _health_probe  # public alias for /health route to consume
```

- [ ] **Step 3: Update `akosha/mcp/server.py` HTTP handler to consume the same probe**

In `akosha/mcp/server.py`, import `health_probe`:

```python
from akosha.cli import health_probe
```

Replace the `/health` route:

```python
@app.custom_route("/health", methods=["GET"])
async def health_check(request: Any) -> JSONResponse:
    app_instance = request.app.state.akosha_app  # type: ignore[attr-defined]
    result = await health_probe(app_instance)
    return JSONResponse(result, status_code=200 if result["status"] == "ok" else 503)
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/test_health_consistency.py tests/unit/test_mcp_health.py tests/unit/cli/test_health.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add akosha/cli.py akosha/mcp/server.py tests/unit/test_health_consistency.py
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "fix(akosha): HTTP /health and CLI 'akosha health' use the same probe

audit M4: CLI reported 'degraded' on dependency failure but HTTP /health
returned 200 with hardcoded 'ok'. Promote _health_probe to public alias
and have the HTTP route consume it. Single source of truth."
```

## Task 3.4: Coverage gap closure to 90%

**Files:**

- Modify: `pyproject.toml:99` (raise `--cov-fail-under` to 90.0)
- Modify: `.coverage-ratchet.json`
- Create: 6-8 additional test files for the worst-covered modules

**Interfaces:**

- Produces: `pytest --cov-fail-under=90.0` exits 0; `.coverage-ratchet.json` shows 90% milestone.

- [ ] **Step 1: Identify uncovered lines in the bottom-15 modules**

Run: `cd /Users/les/Projects/akosha && .venv/bin/coverage report --skip-empty 2>&1 | sort -k7 | head -20`
Expected: bottom-15 modules with their missing line ranges.

- [ ] **Step 2: For each module below 80%, write tests targeting the missing lines**

Modules to focus on (from the audit):
- `akosha/main.py` (66%) — tests for `AkoshaApplication.start()` lifecycle paths
- `akosha/ingestion/bodai_event_subscriber.py` (68%) — reconnect/backoff paths
- `akosha/tools/mermaid_validator/renderer.py` (69%) — failure cases
- `akosha/ingestion/code_graph_ingester.py` (75%) — incremental update paths
- `akosha/mcp/tools/__init__.py` (77%) — registration edge cases
- `akosha/storage/dhara_http_client.py` (79%) — retry paths
- `akosha/cli.py` (81%) — uncovered command paths
- `akosha/storage/hot_store.py` (82%) — schema validation paths
- `akosha/processing/embedding_dim.py` (82%) — edge cases
- `akosha/config.py` (83%) — settings edge cases

For each, write tests following the TDD pattern (read module → write failing tests → run to verify → commit). Aim for ≥10 new test cases per module to close the gap.

- [ ] **Step 3: Run coverage to confirm ≥90%**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest --cov=akosha --cov-fail-under=90.0 --cov-report=term-missing -q -m "not slow"`
Expected: exit 0; total coverage ≥90%.

- [ ] **Step 4: Update `.coverage-ratchet.json`**

Edit the file:

```json
{
  "baseline": 87.61723329425557,
  "current_minimum": 90.0,
  "history": [
    {"commit": "baseline", "coverage": 87.61723329425557, "date": "2026-08-11T15:42:10.668605", "milestone": false},
    {"commit": "<wave-3-commit>", "coverage": 90.0, "date": "2026-09-05T...", "milestone": true}
  ],
  "last_updated": "2026-09-05T...",
  "milestones_achieved": [{"milestone": 90, "date": "2026-09-05T..."}],
  "next_milestone": 95,
  "target": 100.0
}
```

(Use the actual wave-3 commit hash and timestamp.)

- [ ] **Step 5: Update `pyproject.toml:99` `--cov-fail-under=90.0`**

- [ ] **Step 6: Commit wave 3**

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add tests/ pyproject.toml .coverage-ratchet.json
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "test(akosha): close coverage gap to 90% + raise --cov-fail-under

Wave 3 deliverable: --cov-fail-under raised from 87.62% to 90.0%
(next ratchet milestone). Bottom-15 modules each get new test cases
targeting uncovered lines. .coverage-ratchet.json records milestone."
```

- [ ] **Step 7: Wave 3 feature-tracking entry + commit**

Append wave-3 section to `docs/feature-tracking/2026-09-05-akosha-hardening.md` (M2, M3, M4 fixes + 90% coverage milestone). Bump version to 0.14.6.

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add docs/feature-tracking/2026-09-05-akosha-hardening.md pyproject.toml akosha/__init__.py uv.lock
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "chore(akosha): wave-3 done — bump to 0.14.6 + 90% coverage milestone"
```

---

# Wave 4 — Test Quality Refactor

## Task 4.1: Identify all empty tests via AST scan

**Files:**

- Create: `scripts/audit_empty_tests.py` (reusable in CI)
- Output: `tests/.empty_tests_inventory.json`

- [ ] **Step 1: Write the AST scanner**

```python
"""Identify test functions with no assertions or pytest.raises calls."""
from __future__ import annotations

import ast
import json
from pathlib import Path


def is_test_function(node: ast.FunctionDef) -> bool:
    return node.name.startswith("test_")


def has_assertion(body: list[ast.stmt]) -> bool:
    for stmt in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(stmt, ast.Assert):
            return True
        if isinstance(stmt, ast.Raise) and isinstance(stmt.exc, ast.Call):
            func = stmt.exc.func
            if isinstance(func, ast.Attribute) and func.attr == "raises":
                return True
            if isinstance(func, ast.Name) and func.id in {"raises", "fail"}:
                return True
    return False


def main() -> None:
    tests_dir = Path("tests")
    inventory: list[dict[str, str]] = []
    for test_file in tests_dir.rglob("*.py"):
        if test_file.name == "__init__.py":
            continue
        tree = ast.parse(test_file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and is_test_function(node):
                if not has_assertion(node.body):
                    inventory.append({"file": str(test_file), "test": node.name})
    Path("tests/.empty_tests_inventory.json").write_text(json.dumps(inventory, indent=2))
    print(f"Found {len(inventory)} empty tests across {len({i['file'] for i in inventory})} files")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run scanner**

Run: `cd /Users/les/Projects/akosha && .venv/bin/python scripts/audit_empty_tests.py`
Expected: prints count ≥382; writes `tests/.empty_tests_inventory.json`.

- [ ] **Step 3: Commit scanner**

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add scripts/audit_empty_tests.py tests/.empty_tests_inventory.json
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "chore(akosha): AST scanner for empty no-assert tests"
```

## Task 4.2: Rewrite empty tests in-place by domain

**Files:**

- Modify: ~12 test files identified in the inventory

Work in batches by domain. For each batch, apply the TDD pattern: read the original test, identify what failure path it should probe, write the rewritten test, run to verify it passes (or fails for the right reason), commit.

### Task 4.2a: `tests/test_security_coverage.py` — 18 empty tests → + 18 failure-path tests

Focus: auth-bypass, malformed JWT, expired token, role-mismatch, signature tampering, missing claim.

Pattern (one example, apply to each):

```python
def test_verify_token_rejects_expired_jwt() -> None:
    """Security: expired JWT must be rejected with 401."""
    expired = jwt.encode({"sub": "alice", "exp": int(time.time()) - 60}, "secret", algorithm="HS256")
    response = client.post("/api/protected", headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401
    assert "expired" in response.json()["error"].lower()
```

### Task 4.2b: `tests/test_prometheus_metrics_coverage.py` — 38 empty tests → + 38 failure-path tests

Focus: missing labels, NaN values, malformed exposition format, counter reset behavior, histogram bucket overflow.

### Task 4.2c: `tests/test_validation_coverage.py` — 35 empty tests → + 35 failure-path tests

Focus: schema rejection, missing required field, type mismatch, Pydantic v2 validator failure, custom validator edge cases.

### Task 4.2d: `tests/unit/test_models_schemas.py` — 33 empty tests → + 33 failure-path tests

Focus: invalid UUID, invalid timestamp, null required field, negative numeric, boundary overflow.

### Task 4.2e: `tests/unit/test_tracing.py` — 21 empty tests → + 21 failure-path tests

Focus: OTel exporter failure, span drop, batch overflow, attribute limit, missing context propagation.

### Task 4.2f: Remaining ~237 empty tests across `tests/unit/test_validation.py`, `tests/unit/test_ingestion_validation.py`, `tests/unit/test_security_logging.py`, `tests/scripts/test_data_ingestion.py`, `tests/test_mermaid_renders.py`, `tests/unit/test_websocket_tls_config.py`, and others.

- [ ] **Final step: run scanner again to confirm 0 empty tests**

Run: `cd /Users/les/Projects/akosha && .venv/bin/python scripts/audit_empty_tests.py`
Expected: prints `Found 0 empty tests`.

- [ ] **Commit (one per domain batch)**

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add tests/
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "test(akosha): rewrite empty no-assert tests with failure-path probes

Wave 4: ~382 empty no-assert tests across ~12 files rewritten in-place
to probe failure paths. Coverage % unchanged or higher; test count
preserved. Top domains: security, prometheus metrics, validation,
models/schemas, OTel tracing."
```

## Task 4.3: Coverage-quality guard test

**Files:**

- Create: `tests/unit/test_test_quality.py`

- [ ] **Step 1: Write the guard**

```python
from __future__ import annotations

import ast
from pathlib import Path


def _has_assertion(func: ast.FunctionDef) -> bool:
    for stmt in ast.walk(func):
        if isinstance(stmt, ast.Assert):
            return True
        if isinstance(stmt, ast.Raise) and isinstance(stmt.exc, ast.Call):
            func_node = stmt.exc.func
            if isinstance(func_node, ast.Attribute) and func_node.attr == "raises":
                return True
    return False


def test_no_empty_tests() -> None:
    """audit Section 6: no test_* function may have zero assertions."""
    tests_dir = Path("tests")
    offenders: list[tuple[str, str]] = []
    for test_file in tests_dir.rglob("*.py"):
        if test_file.name == "__init__.py":
            continue
        tree = ast.parse(test_file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                if not _has_assertion(node):
                    offenders.append((str(test_file), node.name))
    assert not offenders, f"Empty tests found: {offenders[:5]}... ({len(offenders)} total)"
```

- [ ] **Step 2: Run guard**

Run: `cd /Users/les/Projects/akosha && .venv/bin/pytest tests/unit/test_test_quality.py -v`
Expected: pass.

- [ ] **Step 3: Commit**

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add tests/unit/test_test_quality.py
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "test(akosha): guard against empty no-assert tests"
```

## Task 4.4: Wave 4 commit + feature-tracking entry

- [ ] **Step 1: Bump version to 0.14.7**

In `pyproject.toml` and `akosha/__init__.py:__version__`. Run `uv lock`.

- [ ] **Step 2: Update `docs/feature-tracking/2026-09-05-akosha-hardening.md`**

```markdown
## Wave 4 (test-quality refactor) — built: 2026-09-05

382 empty no-assert tests rewritten in-place with `pytest.raises` /
`assert` statements. Focus: security (18), prometheus metrics (38),
validation (35), models/schemas (33), OTel tracing (21), plus ~237
across 7 other domains.

`scripts/audit_empty_tests.py` scanner added for future audits.
`tests/unit/test_test_quality.py::test_no_empty_tests` CI guard prevents
regression to the empty-test anti-pattern.
```

- [ ] **Step 3: Commit wave 4**

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add docs/feature-tracking/2026-09-05-akosha-hardening.md pyproject.toml akosha/__init__.py uv.lock
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "chore(akosha): wave-4 done — bump to 0.14.7 + 382 empty tests rewritten"
```

---

# Wave 5 — Live MCP Backend Wiring + 0.15.0

## Task 5.1: Investigate why the MCP backend is empty

**Files:**

- Read-only: `akosha/main.py`, `akosha/processing/knowledge_graph.py`, `akosha/processing/otel_ingester.py` (or wherever OTel ingester lives), `akosha/ingestion/code_graph_ingester.py`

- [ ] **Step 1: Run Akosha MCP in foreground with debug logging**

Run: `cd /Users/les/Projects/akosha && AKOSHA_LOG_LEVEL=DEBUG .venv/bin/python -m akosha.mcp 2>&1 | tee /tmp/akosha-mcp-debug.log &`
Expected: server starts; logs show whether knowledge-graph population task, OTel ingester, and code indexer are scheduled.

- [ ] **Step 2: Wait 60 seconds, then query**

Run from another shell: `mcp__akosha__get_graph_statistics`, `mcp__akosha__query_local_traces`, `mcp__akosha__search_code_patterns` (via a Bodai CLI client or the Mahavishnu MCP bridge).

- [ ] **Step 3: Document findings in `## Appendix A` of the spec**

Open `/Users/les/Projects/akosha/docs/superpowers/specs/2026-09-05-akosha-hardening-design.md` and replace the `## Appendix A` placeholder with the actual investigation report.

- [ ] **Step 4: Commit investigation report**

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add docs/superpowers/specs/2026-09-05-akosha-hardening-design.md
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "docs(akosha): populate Wave-5 investigation report in spec appendix A"
```

## Task 5.2: Knowledge graph writer wiring

**Files:**

- Modify: `akosha/main.py` (boot hook in `AkoshaApplication.start()`)

- [ ] **Step 1: Based on Task 5.1 findings, identify the population entry point**

Most likely: `akosha/processing/knowledge_graph.py` already has writers but they're never called from boot.

- [ ] **Step 2: Add boot hook in `akosha/main.py`**

Find `AkoshaApplication.start()` (around line 145-273). Add after the HotStore initialization:

```python
        # Wave 5: populate knowledge graph from indexed sources.
        if self._kg_writer is not None:
            await self._kg_writer.populate_from_indexed_sources()
```

(Adapt names to actual `AkoshaApplication` attributes.)

- [ ] **Step 3: Add background task to keep the graph updated**

```python
        self._kg_writer_task = asyncio.create_task(
            self._kg_writer.periodic_refresh(),
            name="akosha.kg_writer",
        )
```

In `close()`:

```python
        if self._kg_writer_task is not None:
            self._kg_writer_task.cancel()
```

- [ ] **Step 4: Test (extend `tests/unit/test_main_boot.py` or create it)**

- [ ] **Step 5: Commit**

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add akosha/main.py tests/
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "feat(akosha): wire knowledge-graph population in AkoshaApplication.start"
```

## Task 5.3: OTel trace ingester wiring

**Files:**

- Modify: `akosha/main.py` (similar pattern as Task 5.2)

- [ ] **Step 1: Add OTel ingester boot hook**

In `AkoshaApplication.start()`, add:

```python
        # Wave 5: start OTel trace ingester polling loop.
        if self._otel_ingester is not None:
            self._otel_ingester_task = asyncio.create_task(
                self._otel_ingester.run(),
                name="akosha.otel_ingester",
            )
```

- [ ] **Step 2: Cancel on close**

```python
        if self._otel_ingester_task is not None:
            self._otel_ingester_task.cancel()
```

- [ ] **Step 3: Commit**

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add akosha/main.py
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "feat(akosha): wire OTel trace ingester polling loop in AkoshaApplication.start"
```

## Task 5.4: Code indexer wiring

**Files:**

- Modify: `akosha/main.py` (similar pattern)

- [ ] **Step 1: Add code indexer boot hook**

```python
        # Wave 5: index akosha source for search_code_patterns.
        if self._code_indexer is not None:
            await self._code_indexer.index_directory(Path("akosha"))
```

- [ ] **Step 2: Add file-watcher for incremental updates**

```python
        self._code_indexer_watcher = asyncio.create_task(
            self._code_indexer.watch_and_update(),
            name="akosha.code_indexer_watcher",
        )
```

- [ ] **Step 3: Commit**

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add akosha/main.py
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "feat(akosha): wire code indexer for search_code_patterns"
```

## Task 5.5: Live verification + version bump to 0.15.0

**Files:**

- Modify: `pyproject.toml`, `akosha/__init__.py:__version__`

- [ ] **Step 1: Restart Akosha MCP and verify**

Run: `cd /Users/les/Projects/akosha && .venv/bin/python -m akosha.mcp &`
Sleep 60 seconds. From another shell:

```bash
mcp__akosha__get_graph_statistics
mcp__akosha__query_local_traces
mcp__akosha__search_code_patterns(pattern="TODO")
```

Expected: non-empty results.

- [ ] **Step 2: Bump version to 0.15.0**

In `pyproject.toml` and `akosha/__init__.py:__version__`. Run `uv lock`.

- [ ] **Step 3: Update spec status to `complete`**

In `/Users/les/Projects/akosha/docs/superpowers/specs/2026-09-05-akosha-hardening-design.md`, change `status: draft` to `status: complete`.

- [ ] **Step 4: Update `docs/feature-tracking/2026-09-05-akosha-hardening.md`**

```markdown
## Wave 5 (live MCP wiring + 0.15.0) — built: 2026-09-05, adopted: 2026-09-05

All 5 waves complete. Spec marked `complete`.

Live MCP backend wired:
- Knowledge graph populated and refreshes periodically.
- OTel trace ingester polling loop active.
- Code indexer indexes `akosha/**/*.py` and updates incrementally.

Verification: `mcp__akosha__get_graph_statistics`, `query_local_traces`,
`search_code_patterns` return non-empty results after 60-second warmup.

Version bumped 0.14.x → 0.15.0.
```

Update frontmatter `built:` and `adopted:` dates.

- [ ] **Step 5: Commit wave 5**

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add docs/feature-tracking/2026-09-05-akosha-hardening.md docs/superpowers/specs/2026-09-05-akosha-hardening-design.md pyproject.toml akosha/__init__.py uv.lock
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "feat(akosha): wave-5 done — bump to 0.15.0 + live MCP backend wired

Spec marked complete. All 5 waves shipped to local main.
Knowledge graph, OTel traces, code patterns indexes now populated."
```

- [ ] **Step 6: Final feature-tracking entry — mark `adopted` for whole plan**

Open `docs/feature-tracking/2026-09-05-akosha-hardening.md` and update the frontmatter:

```yaml
built: 2026-09-05
wired: 2026-09-05
adopted: 2026-09-05
```

Commit:

```bash
cd /Users/les/Projects/akosha && git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' add docs/feature-tracking/2026-09-05-akosha-hardening.md
git -c user.email='les@wedgwoodwebworks.com' -c user.name='les' commit -m "chore(akosha): mark hardening plan adopted"
```