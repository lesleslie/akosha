# Cold Tier: fake-gcs-server + R2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend akosha's cold-tier substrate to support GCS-compatible storage (fake-gcs-server in dev, Cloudflare R2 in prod) with zero committed credentials and a one-line vendor swap via `local.yaml`.

**Architecture:** Extend oneiric's `GCSStorageAdapter` with `endpoint_url` so it can talk to fake-gcs-server (loops through the SDK's `client_options={"api_endpoint": ..., "use_auth_w_custom_endpoint": False}` mechanism — no separate `AnonymousCredentials` field). Extend akosha's `ColdStorageConfig` with six new fields (three for the gcs path: `endpoint_url`, `project`, `anonymous_credentials`; three for the s3 path: `access_key_id`, `secret_access_key`, `region`) using the manual `__init__` env-binding pattern that `HotStorageConfig` already uses. Wire the new fields into the two existing oneiric-settings construction sites (`ColdStore.initialize()` for gcs; `modes/standard.py:initialize_cold_storage()` for s3).

**Tech Stack:** Python 3.14, oneiric (storage adapters), google-cloud-storage SDK, aioboto3 (via oneiric `S3StorageAdapter`), pyarrow (Parquet), Pydantic v2, pytest.

**Spec:** `akosha/docs/specs/2026-09-27-cold-tier-fake-gcs-design.md` (commits `e9acd5a`, `8cebdd7`)

## Global Constraints

- **`from __future__ import annotations`** as the first non-comment line of every new/modified source file.
- Modern syntax: `X | None` (not `Optional[X]`), `list[str]` (not `List[str]`), `pathlib.Path` for filesystem paths.
- Function arguments with default `None` typed as `X | None = None` (mypy `no_implicit_optional = true`).
- All production code in `akosha/akosha/` and `oneiric/oneiric/` uses Oneiric logger (`oneiric.core.logging.get_logger`); test files use `pytest`'s plain assertions.
- Pre-1.0 direct merge to `main` (per `bodai-pre-1.0-merge-policy.md`); no PRs.
- Never hardcode paths like `/Users/les/...` in committed code; use `Path.home() / ".cache" / "<repo>"` or env vars.
- All commits end with the `Co-Authored-By: Claude Code <noreply@anthropic.com>` attribution line (per project policy).
- **Do NOT use `--no-verify` on code commits** — the global `~/.git-hooks/pre-commit` only blocks deletions of LICENSE/README/CLAUDE.md/AGENTS.md, none of which this plan touches. Plain `git commit -m "..."` is fine. (This plan was originally written with `--no-verify` everywhere; corrected after review.)
- Akosha pytest runs with `--strict-markers` (pyproject.toml line 93). Only registered markers may be used (`slow, integration, unit, performance, stress, flaky, network, expensive, security, maintenance`). Do NOT introduce new markers without first adding them to the `[tool.pytest.ini_options] markers` list.

## Cross-Repo Coordination

### Pre-execution setup (do this BEFORE dispatching any task)

- [ ] **Pre-A: Stash oneiric main's 4 dirty files** so the Phase A worktree doesn't conflict with them later:

```bash
cd /Users/les/Projects/oneiric
git stash push -u -m 'WIP pre-cold-tier-worktree: cli/mcp.py + core/config.py + audit script + xdg auth test'
git status --short  # confirm clean
```

(Per `feedback-workflow-parallel-same-repo-no-isolation.md` — dirty main files get lost when the worktree branch merges back later.)

- [ ] **Create a fresh oneiric worktree for Phase A** (run from oneiric):

```bash
cd /Users/les/Projects/oneiric
git worktree add /Users/les/Projects/oneiric-cold-tier -b feat/cold-tier-endpoint-url main
cd /Users/les/Projects/oneiric-cold-tier
```

All Phase A tasks (A1, A2, A3) run in `/Users/les/Projects/oneiric-cold-tier`. After A3 verifies, merge the worktree branch back to oneiric main:

```bash
cd /Users/les/Projects/oneiric
git merge --ff-only feat/cold-tier-endpoint-url  # fast-forward only; if it fails, STOP
git worktree remove /Users/les/Projects/oneiric-cold-tier
```

(Per `feedback-worktree-update-ref-drops-parallel-commits.md`: never use `git update-ref` to merge from a worktree — fast-forward is the safe path because the worktree-branch is a strict descendant of main.)

Akosha main is clean and stays on `/Users/les/Projects/akosha` for all B/C tasks. **No worktree needed for akosha** — single-agent per task with `cd` discipline.

### Subagent fanout groups

The plan executes in 6 waves. Within each wave, parallelizable tasks run as separate subagents dispatched in the same message.

| Wave | Tasks | Parallel? | Reason |
|---|---|---|---|
| 1 | A1 + B1.1 | ✓ | different repos |
| 2 | A2 + B1.2 | ✓ | different repos |
| 3 | A3 + (merge oneiric worktree) | sequential in oneiric; akosha idle | A3 verifies before merge |
| 4 | B2.1 (writes red tests in new file) | solo | no shared files |
| 5 | B2.2 → B2.3 → B2.4 | ✗ serialize within akosha | B2.2 and B2.3 share `cold_store.py`; B2.4 modifies `modes/standard.py` but runs after B2.3 to avoid stacked merge conflicts |
| 6 | C1 + C2 | ✓ different files | test file vs new docs file |

Within waves 1, 2, and 6 the subagent dispatch is **two Agent tool calls in one assistant message** so they run concurrently.

---

## Phase A: Oneiric `GCSStorageAdapter` accepts `endpoint_url`

### Task A1: Add failing test for `endpoint_url` passthrough

**Files:**
- Modify: `/Users/les/Projects/oneiric-cold-tier/tests/adapters/test_storage_adapters.py` (insert after the existing `test_gcs_init_with_credentials_file` around line 350)

**Interfaces:**
- Consumes: existing `GCSStorageSettings(bucket, project, credentials_file)` from `oneiric.adapters.storage.gcs`
- Produces: `GCSStorageSettings(bucket, project, credentials_file, endpoint_url=None)` field

- [ ] **Step 1: Add the failing test** (mirror the pattern from existing `test_gcs_init_without_client_uses_google_cloud_storage` at lines 267-305):

```python
async def test_gcs_init_with_endpoint_url_passes_client_options(monkeypatch) -> None:
    """init() wires endpoint_url into client_options={api_endpoint, use_auth_w_custom_endpoint=False}.

    Per the official google-cloud-storage SDK contract, the api_endpoint override
    travels via client_options (NOT as a top-level Client kwarg) and the SDK's
    built-in use_auth_w_custom_endpoint=False flag auto-wires AnonymousCredentials
    so we don't need to import google.auth.credentials.AnonymousCredentials.
    """
    import sys
    import types

    created: list[dict] = []

    class FakeStorageClient:
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)
            self._bucket = _FakeGCSBucket()

        def bucket(self, name: str) -> _FakeGCSBucket:
            return self._bucket

    fake_storage = types.ModuleType("google.cloud.storage")
    fake_storage.Client = FakeStorageClient  # type: ignore[attr-defined]

    fake_service_account = types.ModuleType("google.oauth2.service_account")
    fake_service_account.Credentials = object  # type: ignore[attr-defined]

    fake_oauth2 = types.ModuleType("google.oauth2")
    fake_google_cloud = types.ModuleType("google.cloud")
    fake_google_cloud.storage = fake_storage  # type: ignore[attr-defined]
    fake_google = types.ModuleType("google")

    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.cloud", fake_google_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", fake_storage)
    monkeypatch.setitem(sys.modules, "google.oauth2", fake_oauth2)
    monkeypatch.setitem(
        sys.modules, "google.oauth2.service_account", fake_service_account
    )

    adapter = GCSStorageAdapter(
        GCSStorageSettings(
            bucket="demo",
            project="my-project",
            endpoint_url="http://127.0.0.1:4443",
        )
    )
    await adapter.init()
    assert adapter._bucket is not None
    client_options = created[0]["client_options"]
    assert client_options["api_endpoint"] == "http://127.0.0.1:4443"
    assert client_options["use_auth_w_custom_endpoint"] is False
    await adapter.cleanup()
```

- [ ] **Step 2: Run the new test, verify it fails**

Run: `cd /Users/les/Projects/oneiric-cold-tier && pytest tests/adapters/test_storage_adapters.py::test_gcs_init_with_endpoint_url_passes_client_options -v`

Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'endpoint_url'`.

- [ ] **Step 3: Commit the failing test**

```bash
cd /Users/les/Projects/oneiric-cold-tier
git add tests/adapters/test_storage_adapters.py
git commit -m "test(oneiric): cover GCSStorageAdapter endpoint_url passthrough

Red test — endpoint_url field doesn't exist yet on GCSStorageSettings.
Phase A implementation will turn this green by adding the field and wiring
it into client_options={api_endpoint, use_auth_w_custom_endpoint=False}.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task A2: Add the `endpoint_url` field and wire it through `init()`

**Files:**
- Modify: `/Users/les/Projects/oneiric-cold-tier/oneiric/adapters/storage/gcs.py:18-28` (the `GCSStorageSettings` class) AND `gcs.py:58-77` (the `init()` body)

**Interfaces:**
- Produces: `GCSStorageSettings.endpoint_url: str | None = None` field; `init()` passes `client_options={api_endpoint, use_auth_w_custom_endpoint=False}` when set

- [ ] **Step 1: Add the new field to `GCSStorageSettings`** in `oneiric/adapters/storage/gcs.py`, immediately after `default_content_type`:

```python
    default_content_type: str | None = Field(
        default="application/octet-stream",
        description="Fallback content type used when uploads omit content_type.",
    )
    endpoint_url: str | None = Field(
        default=None,
        description=(
            "Override the GCS API endpoint. Required for non-Google emulators "
            "such as fake-gcs-server. Pass http://127.0.0.1:4443 in dev; "
            "leave None for real GCS."
        ),
    )
```

- [ ] **Step 2: Wire `endpoint_url` into `init()`** at `oneiric/adapters/storage/gcs.py:58-77`. Replace the existing `init()` body with:

```python
    async def init(self) -> None:
        if self._client is None:
            try:
                from google.cloud import storage  # type: ignore[attr-defined]
                from google.oauth2 import service_account
            except ModuleNotFoundError as exc:  # pragma: no cover - defensive
                raise LifecycleError("google-cloud-storage-missing") from exc
            client_kwargs: dict[str, Any] = {}
            client_options: dict[str, Any] = {}
            if self._settings.endpoint_url:
                client_options["api_endpoint"] = self._settings.endpoint_url
                client_options["use_auth_w_custom_endpoint"] = False
            if client_options:
                client_kwargs["client_options"] = client_options
            if self._settings.credentials_file:
                credentials: Any = (
                    service_account.Credentials.from_service_account_file(
                        str(self._settings.credentials_file)
                    )
                )
                client_kwargs["credentials"] = credentials
            if self._settings.project:
                client_kwargs["project"] = self._settings.project
            self._client = storage.Client(**client_kwargs)
        self._bucket = self._client.bucket(self._settings.bucket)
        if self._settings.endpoint_url:
            self._logger.info(
                "gcs-endpoint-override", endpoint=self._settings.endpoint_url
            )
        self._logger.info("adapter-init", adapter="gcs-storage")
```

- [ ] **Step 3: Add a regression test** that asserts `client_options` is NOT set when `endpoint_url=None`. Insert after the test from A1:

```python
async def test_gcs_init_without_endpoint_url_omits_client_options(monkeypatch) -> None:
    """Regression: init() must not pass an empty client_options dict when endpoint_url is unset."""
    import sys
    import types

    created: list[dict] = []

    class FakeStorageClient:
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)
            self._bucket = _FakeGCSBucket()

        def bucket(self, name: str) -> _FakeGCSBucket:
            return self._bucket

    fake_storage = types.ModuleType("google.cloud.storage")
    fake_storage.Client = FakeStorageClient  # type: ignore[attr-defined]
    fake_service_account = types.ModuleType("google.oauth2.service_account")
    fake_service_account.Credentials = object  # type: ignore[attr-defined]
    fake_oauth2 = types.ModuleType("google.oauth2")
    fake_google_cloud = types.ModuleType("google.cloud")
    fake_google_cloud.storage = fake_storage  # type: ignore[attr-defined]
    fake_google = types.ModuleType("google")

    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.cloud", fake_google_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", fake_storage)
    monkeypatch.setitem(sys.modules, "google.oauth2", fake_oauth2)
    monkeypatch.setitem(
        sys.modules, "google.oauth2.service_account", fake_service_account
    )

    adapter = GCSStorageAdapter(GCSStorageSettings(bucket="demo"))
    await adapter.init()
    assert "client_options" not in created[0]
    await adapter.cleanup()
```

- [ ] **Step 4: Run both new tests, verify both pass**

Run: `cd /Users/les/Projects/oneiric-cold-tier && pytest tests/adapters/test_storage_adapters.py -v -k gcs_init`

Expected: Both new tests PASS, plus the existing `test_gcs_init_without_client_uses_google_cloud_storage` and `test_gcs_init_with_credentials_file` still PASS.

- [ ] **Step 5: Run the full storage-adapter test file, verify no regressions**

Run: `cd /Users/les/Projects/oneiric-cold-tier && pytest tests/adapters/test_storage_adapters.py -v`

Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/les/Projects/oneiric-cold-tier
git add oneiric/adapters/storage/gcs.py tests/adapters/test_storage_adapters.py
git commit -m "feat(oneiric): GCSStorageAdapter accepts endpoint_url

Wires client_options={api_endpoint, use_auth_w_custom_endpoint=False} when
endpoint_url is set. The SDK's use_auth_w_custom_endpoint=False flag
auto-wires AnonymousCredentials so no separate credentials field is needed.

Matches official googleapis/python-storage docs. Single INFO log on init
when endpoint_url is set (informational; no defensive layer).

Tests:
- test_gcs_init_with_endpoint_url_passes_client_options
- test_gcs_init_without_endpoint_url_omits_client_options (regression guard)

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task A3: Verify Phase A on oneiric worktree

- [ ] **Step 1: Confirm the commit landed on the worktree branch**

Run: `cd /Users/les/Projects/oneiric-cold-tier && git log --oneline -3`

Expected: Top commit is the feat from A2.

- [ ] **Step 2: Sanity-gate the full GCS test surface**

Run: `cd /Users/les/Projects/oneiric-cold-tier && pytest tests/adapters/test_storage_adapters.py tests/unit/adapters/storage/test_gcs_stream.py -v`

Expected: All tests pass.

- [ ] **Step 3: STOP and report.** Do not merge the worktree branch — that happens after the user reviews Phase A. Report the commit hash and the green test output to the parent agent.

---

## Phase B1: Akosha `ColdStorageConfig` exposes new fields (no wiring yet)

This phase ships **before** Phase A merges. The new fields exist on the akosha config model but are not yet wired into the storage code — Phase B2 wires them.

### Task B1.1: Add failing tests for new `ColdStorageConfig` fields

**Files:**
- Modify: `/Users/les/Projects/akosha/tests/unit/test_config.py` (NOT `test_config_settings.py` — that file doesn't exist; the actual class is `TestColdStorageConfig` at line 62 of `test_config.py`)

**Interfaces:**
- Consumes: existing `ColdStorageConfig` constructor at `akosha/config.py:162-178`
- Produces: `ColdStorageConfig(endpoint_url=..., project=..., anonymous_credentials=..., access_key_id=..., secret_access_key=..., region=...)` accepts all six fields

- [ ] **Step 1: Read `akosha/tests/unit/test_config.py:62-100`** to find the existing `TestColdStorageConfig` class and confirm the import style (the existing tests use inline `from akosha.config import ColdStorageConfig`).

- [ ] **Step 2: Add new test methods** inside `TestColdStorageConfig`:

```python
def test_cold_storage_config_has_endpoint_url_field() -> None:
    """ColdStorageConfig exposes endpoint_url for fake-gcs-server / R2."""
    from akosha.config import ColdStorageConfig

    cfg = ColdStorageConfig(endpoint_url="http://127.0.0.1:4443")
    assert cfg.endpoint_url == "http://127.0.0.1:4443"


def test_cold_storage_config_has_s3_credential_fields() -> None:
    """ColdStorageConfig exposes access_key_id + secret_access_key for S3 backends."""
    from akosha.config import ColdStorageConfig

    cfg = ColdStorageConfig(
        access_key_id="AKIA_test",
        secret_access_key="secret_test",
        region="auto",
    )
    assert cfg.access_key_id == "AKIA_test"
    assert cfg.secret_access_key == "secret_test"
    assert cfg.region == "auto"


def test_cold_storage_config_binds_endpoint_url_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AKOSHA__STORAGE__COLD__ENDPOINT_URL binds into ColdStorageConfig.endpoint_url."""
    monkeypatch.setenv("AKOSHA__STORAGE__COLD__ENDPOINT_URL", "http://emulator:4443")
    monkeypatch.setenv("AKOSHA__STORAGE__COLD__ACCESS_KEY_ID", "env_key")
    monkeypatch.setenv("AKOSHA__STORAGE__COLD__SECRET_ACCESS_KEY", "env_secret")
    monkeypatch.setenv("AKOSHA__STORAGE__COLD__REGION", "auto")

    from akosha.config import ColdStorageConfig

    cfg = ColdStorageConfig()
    assert cfg.endpoint_url == "http://emulator:4443"
    assert cfg.access_key_id == "env_key"
    assert cfg.secret_access_key == "env_secret"
    assert cfg.region == "auto"


def test_cold_storage_config_binds_flat_fallback_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backward-compat: AKOSHA_COLD_ENDPOINT and AKOSHA_COLD_REGION (flat) bind into the
    nested-form fields when the nested env vars are unset.

    QUICKSTART.md references both AKOSHA_COLD_ENDPOINT and AKOSHA_COLD_REGION but
    they were unbound in code prior to this spec. This test pins the flat→nested
    fallback so the existing doc references start working.
    """
    monkeypatch.setenv("AKOSHA_COLD_ENDPOINT", "http://legacy:4443")
    monkeypatch.setenv("AKOSHA_COLD_REGION", "legacy-region")
    monkeypatch.delenv("AKOSHA__STORAGE__COLD__ENDPOINT_URL", raising=False)
    monkeypatch.delenv("AKOSHA__STORAGE__COLD__REGION", raising=False)

    from akosha.config import ColdStorageConfig

    cfg = ColdStorageConfig()
    assert cfg.endpoint_url == "http://legacy:4443"
    assert cfg.region == "legacy-region"
```

- [ ] **Step 3: Run the new tests, verify they all fail**

Run: `cd /Users/les/Projects/akosha && pytest tests/unit/test_config.py -v -k "cold_storage_config"`

Expected: All four new tests FAIL (fields don't exist yet).

- [ ] **Step 4: Commit the failing tests**

```bash
cd /Users/les/Projects/akosha
git add tests/unit/test_config.py
git commit -m "test(akosha): cover ColdStorageConfig new fields + env bindings

Red tests for the Phase B1 config surface: endpoint_url, project,
anonymous_credentials, access_key_id, secret_access_key, region. Plus
fallback bindings for the legacy flat AKOSHA_COLD_* env vars referenced
in QUICKSTART.md but unbound in code today.

Phase B1 implementation will turn these green.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task B1.2: Rewrite `ColdStorageConfig` with new fields + env bindings

**Files:**
- Modify: `/Users/les/Projects/akosha/akosha/config.py:162-178` (the `ColdStorageConfig` class)

**Interfaces:**
- Consumes: existing `os.getenv` pattern from `HotStorageConfig.__init__` at `akosha/config.py:94-99`
- Produces: `ColdStorageConfig` with 6 new fields, all bound via nested env vars; flat `AKOSHA_COLD_*` env vars become fallback bindings

- [ ] **Step 1: Read the existing `ColdStorageConfig`** at `akosha/config.py:162-178` and the `HotStorageConfig.__init__` pattern at lines 94-99 to mirror the env-binding convention.

- [ ] **Step 2: Replace `ColdStorageConfig`** with the new shape:

```python
class ColdStorageConfig(BaseModel):
    """Cold storage configuration.

    Attributes:
        backend: Storage backend type (local, s3, gcs, azure)
        bucket: Bucket name for cloud storage
        prefix: Prefix for objects in bucket
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
    3. Environment variables (nested form, preferred):
       AKOSHA__STORAGE__COLD__ENDPOINT_URL,
       AKOSHA__STORAGE__COLD__PROJECT,
       AKOSHA__STORAGE__COLD__ANONYMOUS_CREDENTIALS,
       AKOSHA__STORAGE__COLD__ACCESS_KEY_ID,
       AKOSHA__STORAGE__COLD__SECRET_ACCESS_KEY,
       AKOSHA__STORAGE__COLD__REGION
    4. Environment variables (flat legacy form, fallback):
       AKOSHA_COLD_ENDPOINT, AKOSHA_COLD_REGION
    """

    backend: str = Field(default_factory=lambda: os.getenv("AKOSHA_COLD_BACKEND", "local"))
    bucket: str = Field(default_factory=lambda: os.getenv("AKOSHA_COLD_BUCKET", "akosha-cold-data"))
    prefix: str = "conversations/"
    format: str = "parquet"
    region: str | None = Field(
        default_factory=lambda: os.getenv("AKOSHA_COLD_REGION", "us-west-2")
    )
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
```

- [ ] **Step 3: Run the four new tests from B1.1, verify they all pass**

Run: `cd /Users/les/Projects/akosha && pytest tests/unit/test_config.py -v -k "cold_storage_config"`

Expected: All four pass.

- [ ] **Step 4: Run the full config-settings test file, verify no regressions**

Run: `cd /Users/les/Projects/akosha && pytest tests/unit/test_config.py -v`

Expected: All existing tests still pass.

- [ ] **Step 5: Document the new fields in committed YAML**

Edit `/Users/les/Projects/akosha/settings/akosha.yaml`, replace the `cold:` block (lines 25-30) with:

```yaml
# Cold storage — committed default is "local" (works with zero dependencies).
# To opt in to fake-gcs-server in dev: set cold.backend=gcs +
# cold.endpoint_url=http://127.0.0.1:4443 in settings/local.yaml.
# To opt in to Cloudflare R2 in prod: set cold.backend=s3 + R2 credentials
# (see docs/operators/cold-tier-quickstart.md for the full walkthrough).
# New fields exposed in ColdStorageConfig (Phase B1):
#   cold.endpoint_url — override API endpoint (fake-gcs / R2)
#   cold.project — GCP project ID (gcs backend only)
#   cold.anonymous_credentials — use anonymous GCS creds (fake-gcs only)
#   cold.access_key_id / cold.secret_access_key — S3 credentials (R2 etc.)
#   cold.region — S3 region ('auto' for R2)
cold:
  backend: local
  bucket: akosha-cold-data
  prefix: conversations/
  format: parquet
  region: us-west-2
```

- [ ] **Step 6: Commit**

```bash
cd /Users/les/Projects/akosha
git add akosha/config.py settings/akosha.yaml tests/unit/test_config.py
git commit -m "feat(akosha): ColdStorageConfig exposes 6 new fields + env bindings

Phase B1 (config surface, ships before Phase A's oneiric adapter extension):

New fields on ColdStorageConfig:
- endpoint_url: override the API endpoint (fake-gcs-server in dev, R2 in prod)
- project: GCP project ID (gcs backend only; ignored on s3)
- anonymous_credentials: use anonymous GCS creds (fake-gcs-server only)
- access_key_id: S3 access key ID (Cloudflare R2 etc.)
- secret_access_key: S3 secret access key
- region: S3 region ('auto' for R2)

Env bindings follow the manual __init__ pattern from HotStorageConfig
(nested form, AKOSHA__STORAGE__COLD__*). AKOSHA_COLD_ENDPOINT is now bound
as a fallback for endpoint_url (was referenced in QUICKSTART.md:81 but
unbound in code; now resolves).

Phase B2 will wire these into the storage adapter construction sites.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Phase B2: Akosha wires the new fields into storage adapter construction

This phase lands **after** Phase A merges (oneiric `GCSStorageAdapter` accepts `endpoint_url`).

### Task B2.1: Add failing tests for the wiring sites (gcs-only this round)

**Files:**
- Modify: `/Users/les/Projects/akosha/tests/integration/test_cold_tier_local.py` (NEW file)

**Interfaces:**
- Consumes: `ColdStore(backend="gcs", bucket=..., endpoint_url=..., project=...)` from `akosha/storage/cold_store.py:36-83, 294-323`
- Produces: `ColdStore.initialize()` constructs `GCSStorageSettings(bucket=..., project=..., endpoint_url=...)`

The s3 wiring test is **deferred to B2.3** because `ColdStore.__init__` doesn't accept `access_key_id`/`secret_access_key` until then. This avoids a fixture sequencing bug.

- [ ] **Step 1: Create `akosha/tests/integration/test_cold_tier_local.py`** with this initial content (B2.3 will append the s3 wiring test, B2.4 will append the StandardMode test, C1 will append the end-to-end test):

```python
"""Cold-tier storage tests.

Covers ColdStore.initialize() wiring for new fields (endpoint_url, project,
access_key_id, secret_access_key) and the end-to-end round trip against
fake-gcs-server. Tests are appended incrementally as the plan progresses:
- B2.1: gcs wiring (this file's initial state)
- B2.3: s3 wiring (added in B2.3)
- B2.4: StandardMode wiring (added in B2.4)
- C1: end-to-end subprocess test (added in C1)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest


def test_cold_store_gcs_branch_forwards_endpoint_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ColdStore.initialize() with backend='gcs' passes endpoint_url to GCSStorageSettings.

    Without the wiring fix, GCSStorageSettings would only receive
    bucket + project + credentials_file — endpoint_url would be dropped.
    """
    import asyncio

    from akosha.storage.cold_store import ColdStore

    captured: list[dict] = []

    class FakeGCSStorageAdapter:
        def __init__(self, settings: Any) -> None:
            captured.append(settings.model_dump())

    store = ColdStore(
        bucket="akosha-cold-data",
        prefix="conversations/",
        storage_backend="gcs",
        project="local-dev",
        endpoint_url="http://127.0.0.1:4443",
    )

    with patch("akosha.storage.cold_store.GCSStorageAdapter", FakeGCSStorageAdapter):
        asyncio.run(store.initialize())

    assert len(captured) == 1
    settings = captured[0]
    assert settings["bucket"] == "akosha-cold-data"
    assert settings["endpoint_url"] == "http://127.0.0.1:4443"
    assert settings["project"] == "local-dev"
```

- [ ] **Step 2: Run the new test, verify it fails**

Run: `cd /Users/les/Projects/akosha && pytest tests/integration/test_cold_tier_local.py::test_cold_store_gcs_branch_forwards_endpoint_url -v`

Expected: FAIL — wiring doesn't forward `endpoint_url` yet.

- [ ] **Step 3: Commit the failing wiring test**

```bash
cd /Users/les/Projects/akosha
git add tests/integration/test_cold_tier_local.py
git commit -m "test(akosha): cover ColdStore gcs branch forwards endpoint_url

Red test for the Phase B2.2 wiring: ColdStore.initialize() must forward
endpoint_url into GCSStorageSettings. Without this fix, fake-gcs-server
fails at runtime (connection refused on default GCS endpoint).

Phase B2.2 implementation will turn this green.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task B2.2: Wire `endpoint_url` into the gcs branch of `ColdStore.initialize()`

**Files:**
- Modify: `/Users/les/Projects/akosha/akosha/storage/cold_store.py:315-323` (the gcs branch of `initialize()`)

- [ ] **Step 1: Edit `cold_store.py:318-322`** to add `endpoint_url=self._endpoint_url`:

```python
        elif self._storage_backend == "gcs":
            if not self.bucket:
                raise ValueError("GCS backend requires a non-empty 'bucket' argument")
            settings = GCSStorageSettings(
                bucket=self.bucket,
                project=self._project,
                credentials_file=self._credentials_file,
                endpoint_url=self._endpoint_url,
            )
            adapter = GCSStorageAdapter(settings=settings)
```

- [ ] **Step 2: Run `test_cold_store_gcs_branch_forwards_endpoint_url`**, verify it passes

Run: `cd /Users/les/Projects/akosha && pytest tests/integration/test_cold_tier_local.py::test_cold_store_gcs_branch_forwards_endpoint_url -v`

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
cd /Users/les/Projects/akosha
git add akosha/storage/cold_store.py
git commit -m "feat(akosha): ColdStore gcs branch forwards endpoint_url

Wires the new endpoint_url field into GCSStorageSettings so fake-gcs-server
(and any other GCS-compatible endpoint) works without code changes.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task B2.3: Wire `access_key_id` + `secret_access_key` into the s3 branch

**Files:**
- Modify: `/Users/les/Projects/akosha/akosha/storage/cold_store.py:36-83` (`ColdStore.__init__`) AND `akosha/storage/cold_store.py:309-313` (the s3 branch of `initialize()`)

**Interfaces:**
- Produces: `ColdStore(..., access_key_id: str | None = None, secret_access_key: str | None = None)` constructor params; `S3StorageSettings(bucket=, region=, endpoint_url=, access_key_id=, secret_access_key=)` in `initialize()`

- [ ] **Step 1: Add the new constructor params to `ColdStore.__init__`** at line 36-83. After the existing `# S3 / R2` block (after `endpoint_url: str | None = None,` at line 45), add the credential params:

```python
        # S3 / R2
        region: str | None = None,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
```

- [ ] **Step 2: In the body of `__init__`, after `self._endpoint_url = endpoint_url` at line 78, add:**

```python
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
```

- [ ] **Step 3: Append the failing s3 wiring test** to `akosha/tests/integration/test_cold_tier_local.py`:

```python
def test_cold_store_s3_branch_forwards_credentials() -> None:
    """ColdStore.initialize() with backend='s3' passes access_key_id + secret_access_key to S3StorageSettings.

    Without the wiring fix, S3StorageSettings would only receive bucket + region +
    endpoint_url — credentials would be dropped and R2 would 401 at runtime.
    """
    import asyncio
    from unittest.mock import patch

    from akosha.storage.cold_store import ColdStore

    captured: list[dict] = []

    class FakeS3StorageAdapter:
        def __init__(self, settings: Any) -> None:
            captured.append(settings.model_dump())

    store = ColdStore(
        bucket="akosha-cold-data",
        prefix="conversations/",
        storage_backend="s3",
        endpoint_url="https://test-account.r2.cloudflarestorage.com",
        region="auto",
        access_key_id="AKIA_test",
        secret_access_key="secret_test",
    )

    with patch("akosha.storage.cold_store.S3StorageAdapter", FakeS3StorageAdapter):
        asyncio.run(store.initialize())

    assert len(captured) == 1
    settings = captured[0]
    assert settings["bucket"] == "akosha-cold-data"
    assert settings["endpoint_url"] == "https://test-account.r2.cloudflarestorage.com"
    assert settings["region"] == "auto"
    assert settings["access_key_id"] == "AKIA_test"
    assert settings["secret_access_key"] == "secret_test"
```

- [ ] **Step 4: Run the new s3 wiring test, verify it fails**

Run: `cd /Users/les/Projects/akosha && pytest tests/integration/test_cold_tier_local.py::test_cold_store_s3_branch_forwards_credentials -v`

Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'access_key_id'` (the constructor doesn't accept the kwargs yet — that's the bug B2.3 is about to fix).

- [ ] **Step 5: Edit `cold_store.py:309-313`** to forward the credentials into `S3StorageSettings`:

```python
        elif self._storage_backend == "s3":
            if not self.bucket:
                raise ValueError("S3 backend requires a non-empty 'bucket' argument")
            settings = S3StorageSettings(
                bucket=self.bucket,
                region=self._region,
                endpoint_url=self._endpoint_url,
                access_key_id=self._access_key_id,
                secret_access_key=self._secret_access_key,
            )
            adapter = S3StorageAdapter(settings=settings)
```

- [ ] **Step 6: Run `test_cold_store_s3_branch_forwards_credentials`**, verify it passes

Run: `cd /Users/les/Projects/akosha && pytest tests/integration/test_cold_tier_local.py::test_cold_store_s3_branch_forwards_credentials -v`

Expected: PASS.

- [ ] **Step 7: Run the full integration test file**, verify both wiring tests pass

Run: `cd /Users/les/Projects/akosha && pytest tests/integration/test_cold_tier_local.py -v`

Expected: Both wiring tests pass.

- [ ] **Step 8: Commit**

```bash
cd /Users/les/Projects/akosha
git add akosha/storage/cold_store.py tests/integration/test_cold_tier_local.py
git commit -m "feat(akosha): ColdStore s3 branch forwards access_key_id + secret_access_key

Adds the two new constructor parameters to ColdStore.__init__ and
wires them into S3StorageSettings in initialize(). Without this,
Cloudflare R2 (and any other S3-compatible backend) would silently 401
at runtime because the oneiric S3 client never sees the operator's
API tokens.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task B2.4: Wire new fields through `modes/standard.py:initialize_cold_storage()`

**Files:**
- Modify: `/Users/les/Projects/akosha/akosha/modes/standard.py` (the cold-storage branch of `initialize_cold_storage`)

**Interfaces:**
- Produces: when `cold_storage_enabled` is true, the code reads cold-tier config from `self.config` dict and forwards all six new fields into oneiric's `S3StorageSettings` (s3 branch) and `GCSStorageSettings` (gcs branch).

**Important architecture note:** `BaseMode.__init__(self, config: dict[str, Any])` (`akosha/modes/base.py:42`) takes a flat dict, NOT an `AkoshaConfig`. The existing code reads keys like `cold_storage_backend`, `cold_bucket`, `cold_prefix` from this dict (see `modes/standard.py:116-118`). The plan's edit must use this flat-dict access pattern, NOT the Pydantic attribute access used in `ColdStorageConfig`.

- [ ] **Step 1: Read `akosha/modes/standard.py` lines 98-148** to confirm the existing dict-access shape and identify the s3 and gcs branches that need new field forwarding.

- [ ] **Step 2: Append the failing wiring test** to `akosha/tests/integration/test_cold_tier_local.py`:

```python
def test_standard_mode_cold_storage_forwards_s3_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """StandardMode.initialize_cold_storage() must read cold-tier config from
    self.config (flat dict per BaseMode.__init__) and forward endpoint_url +
    credentials + region into S3StorageSettings.
    """
    import asyncio
    from unittest.mock import patch

    # Configure env vars the StandardMode config builder reads from
    monkeypatch.setenv("AKOSHA_COLD_BACKEND", "s3")
    monkeypatch.setenv("AKOSHA_COLD_BUCKET", "akosha-r2")
    monkeypatch.setenv("AKOSHA_COLD_REGION", "auto")
    monkeypatch.setenv(
        "AKOSHA_COLD_ENDPOINT_URL", "https://test.r2.cloudflarestorage.com"
    )
    monkeypatch.setenv("AKOSHA_COLD_ACCESS_KEY_ID", "AKIA_test")
    monkeypatch.setenv("AKOSHA_COLD_SECRET_ACCESS_KEY", "secret_test")

    captured: list[dict] = []

    class FakeS3StorageAdapter:
        def __init__(self, settings: Any) -> None:
            captured.append(settings.model_dump())

    # The implementer must identify how StandardMode's config dict is
    # populated. The simplest path: build the dict explicitly to mirror
    # what load_config() would produce.
    from akosha.modes.standard import StandardMode

    mode = StandardMode(
        {
            "cold_storage_enabled": True,
            "cold_storage_backend": "s3",
            "cold_bucket": "akosha-r2",
            "cold_prefix": "conversations/",
            "cold_format": "parquet",
            "cold_region": "auto",
            "cold_endpoint_url": "https://test.r2.cloudflarestorage.com",
            "cold_access_key_id": "AKIA_test",
            "cold_secret_access_key": "secret_test",
        }
    )

    with patch("akosha.modes.standard.S3StorageAdapter", FakeS3StorageAdapter):
        asyncio.run(mode.initialize_cold_storage())

    assert len(captured) >= 1, "S3StorageAdapter was never constructed"
    settings = captured[0]
    assert settings["bucket"] == "akosha-r2"
    assert settings["endpoint_url"] == "https://test.r2.cloudflarestorage.com"
    assert settings["region"] == "auto"
    assert settings["access_key_id"] == "AKIA_test"
    assert settings["secret_access_key"] == "secret_test"
```

- [ ] **Step 3: Run the new test, verify it fails**

Run: `cd /Users/les/Projects/akosha && pytest tests/integration/test_cold_tier_local.py::test_standard_mode_cold_storage_forwards_s3_credentials -v`

Expected: FAIL — `modes/standard.py` doesn't read or forward the new fields today.

- [ ] **Step 4: Edit `modes/standard.py:initialize_cold_storage()`** to read the new fields from the dict and forward them. The exact edit depends on the existing branch shape:

For the `s3` branch (around line 128-132), update the `S3StorageSettings(...)` constructor:

```python
            elif backend == "s3":
                from oneiric.adapters.storage.s3 import (
                    S3StorageAdapter,
                    S3StorageSettings,
                )
                settings = S3StorageSettings(
                    bucket=bucket,
                    region=self.config.get("cold_region", "us-west-2"),
                    endpoint_url=self.config.get("cold_endpoint_url"),
                    access_key_id=self.config.get("cold_access_key_id"),
                    secret_access_key=self.config.get("cold_secret_access_key"),
                )
                adapter = S3StorageAdapter(settings=settings)
```

For the `gcs` branch (around line 128-135), update `GCSStorageSettings(...)`:

```python
            elif backend == "gcs":
                from oneiric.adapters.storage.gcs import (
                    GCSStorageAdapter,
                    GCSStorageSettings,
                )
                settings = GCSStorageSettings(
                    bucket=bucket,
                    project=self.config.get("cold_project"),
                    endpoint_url=self.config.get("cold_endpoint_url"),
                    credentials_file=self.config.get("cold_credentials_file"),
                )
                adapter = GCSStorageAdapter(settings=settings)
```

**Important:** the dict keys (`cold_region`, `cold_endpoint_url`, `cold_access_key_id`, `cold_secret_access_key`, `cold_project`, `cold_credentials_file`) must match whatever code populates `self.config`. The implementer must trace how `StandardMode.config` is built (likely via `load_config()` → `AkonfigConfig().model_dump()` or a similar flattening step) and add the new keys there if they don't already exist. If the dict key names don't match what the test expects, adjust the test accordingly — the wiring edit is the source of truth, not the test.

- [ ] **Step 5: Re-run the test, verify it passes**

Run: `cd /Users/les/Projects/akosha && pytest tests/integration/test_cold_tier_local.py::test_standard_mode_cold_storage_forwards_s3_credentials -v`

Expected: PASS.

- [ ] **Step 6: Run the full integration test file**, verify no regressions

Run: `cd /Users/les/Projects/akosha && pytest tests/integration/test_cold_tier_local.py -v`

Expected: All three wiring tests pass.

- [ ] **Step 7: Commit**

```bash
cd /Users/les/Projects/akosha
git add akosha/modes/standard.py tests/integration/test_cold_tier_local.py
git commit -m "feat(akosha): StandardMode forwards cold-tier credentials

Wires the new fields (endpoint_url, access_key_id, secret_access_key, project)
through StandardMode.initialize_cold_storage() into the oneiric settings
constructors. Without this, the akosha-side env vars would never reach
the S3 / GCS adapter regardless of how Phase A wired oneiric.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Phase C: Cold-tier integration test against fake-gcs-server subprocess

### Task C1: Add the subprocess-based end-to-end test

**Files:**
- Modify: `/Users/les/Projects/akosha/tests/integration/test_cold_tier_local.py` (append the new test)

**Interfaces:**
- Consumes: `fake-gcs-server` binary on PATH (Homebrew-installed per the operator's environment); `ColdRecord` from `akosha.storage.models` (NOT `akosha.models`)
- Produces: a hermetic end-to-end test that exercises the full Parquet-export → GCS-upload → list → download round trip

**Critical field-name corrections** (reviewer feedback): `ColdRecord` lives at `akosha/storage/models.py:55-66` and uses `fingerprint: list[int]`, `timestamp: datetime`, `metadata: dict[str, Any]` (NOT `bytes`/`int`/`daily_metrics`).

- [ ] **Step 1: Confirm `fake-gcs-server` is on PATH**

Run: `which fake-gcs-server`

Expected: `/usr/local/bin/fake-gcs-server` (or similar). If absent, the test should `pytest.skip` — see step 2.

- [ ] **Step 2: Append the end-to-end test** to `akosha/tests/integration/test_cold_tier_local.py`:

```python
def test_cold_tier_fake_gcs_round_trip(tmp_path_factory: pytest.TempPathFactory) -> None:
    """End-to-end: fake-gcs-server subprocess + ColdStore.export_batch() + file-on-disk round trip.

    This test requires fake-gcs-server on PATH (brew install fake-gcs-server).
    Skipped automatically if the binary isn't found.
    """
    import shutil
    import socket
    import subprocess
    import time
    from datetime import UTC, datetime

    if shutil.which("fake-gcs-server") is None:
        pytest.skip("fake-gcs-server not on PATH; install via: brew install fake-gcs-server")

    from akosha.storage.models import ColdRecord
    from akosha.storage.cold_store import ColdStore

    data_dir = tmp_path_factory.mktemp("fake-gcs")
    port = 4443

    proc = subprocess.Popen(
        [
            "fake-gcs-server",
            "-filesystem-root", str(data_dir),
            "-port", str(port),
            "-host", "127.0.0.1",
            "-public-host", f"127.0.0.1:{port}",
            "-location", "US-CENTRAL1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    def port_open(host: str, port: int, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex((host, port)) == 0:
                    return True
            time.sleep(0.1)
        return False

    try:
        assert port_open("127.0.0.1", port), "fake-gcs-server did not start within 10s"

        async def run() -> str:
            store = ColdStore(
                bucket="akosha-cold-data",
                prefix="conversations/",
                storage_backend="gcs",
                project="local-dev",
                endpoint_url=f"http://127.0.0.1:{port}",
            )
            records = [
                ColdRecord(
                    system_id="test-system",
                    conversation_id="conv-1",
                    fingerprint=[1, 2, 3],
                    ultra_summary="hello world",
                    timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                    metadata={"events": 42},
                ),
            ]
            return await store.export_batch(records, partition_path="system-001/2026/01/01")

        import asyncio

        key = asyncio.run(run())
        assert key.endswith(".parquet")
        blob_path = data_dir / "akosha-cold-data" / key
        assert blob_path.exists(), f"blob not found at {blob_path}"
        assert blob_path.stat().st_size > 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
```

- [ ] **Step 3: Run the new test, verify it passes**

Run: `cd /Users/les/Projects/akosha && pytest tests/integration/test_cold_tier_local.py::test_cold_tier_fake_gcs_round_trip -v`

Expected: PASS (or SKIPPED if `fake-gcs-server` isn't on PATH).

- [ ] **Step 4: Run the full integration test file**, verify all tests pass

Run: `cd /Users/les/Projects/akosha && pytest tests/integration/test_cold_tier_local.py -v`

Expected: All four tests pass (three wiring tests + one end-to-end).

- [ ] **Step 5: Commit**

```bash
cd /Users/les/Projects/akosha
git add tests/integration/test_cold_tier_local.py
git commit -m "test(akosha): end-to-end cold tier via fake-gcs-server subprocess

Hermetic subprocess-based test that exercises the full
ColdStore.export_batch() -> GCS upload -> file-on-disk round trip against
a locally-running fake-gcs-server. Skipped if the binary isn't on PATH.

ColdRecord fields verified against akosha/storage/models.py:55-66:
fingerprint is list[int], timestamp is datetime, metadata is dict
(not the bytes/int/daily_metrics field names an earlier draft used).

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task C2: Operator quickstart documentation

**Files:**
- Create: `/Users/les/Projects/akosha/docs/operators/cold-tier-quickstart.md`

**Interfaces:**
- Consumes: the spec's `§D1`, `§D4`, `§D5` content
- Produces: operator-facing instructions for fake-gcs-server setup, R2 setup, and smoke-test recipe

- [ ] **Step 1: Create the file**

```markdown
# Cold Tier Quickstart

This guide covers three things:

1. Running akosha's cold tier against fake-gcs-server (dev).
2. Pointing the cold tier at Cloudflare R2 (prod).
3. A smoke-test recipe for verifying your R2 wiring before committing.

## 1. fake-gcs-server (dev)

### Install

```bash
brew install fake-gcs-server
```

### Start

The session-buddy repo ships lifecycle scripts at `scripts/fake-gcs-{start,stop,init}.sh`,
but the simplest path is to call the binary directly:

```bash
mkdir -p ~/.cache/akosha/fake-gcs
nohup fake-gcs-server \
  -filesystem-root ~/.cache/akosha/fake-gcs \
  -port 4443 \
  -host 127.0.0.1 \
  -public-host 127.0.0.1:4443 \
  -location US-CENTRAL1 \
  > ~/.cache/akosha/fake-gcs/server.log 2>&1 &
```

### Verify

```bash
curl -i http://127.0.0.1:4443/storage/v1/b/o
# Expect: 200 OK (empty list of objects is fine)
```

### Configure akosha

In `settings/local.yaml` (gitignored) or `~/.config/akosha/local.yaml`:

```yaml
cold:
  backend: gcs
  bucket: akosha-cold-data
  prefix: conversations/
  format: parquet
  endpoint_url: http://127.0.0.1:4443
  project: local-dev
  region: us-west-2  # unused for gcs; kept for parity
```

Or via env vars (preferred for CI):

```bash
export AKOSHA__STORAGE__COLD__BACKEND=gcs
export AKOSHA__STORAGE__COLD__ENDPOINT_URL=http://127.0.0.1:4443
export AKOSHA__STORAGE__COLD__PROJECT=local-dev
```

### Reset dev data

```bash
rm -rf ~/.cache/akosha/fake-gcs
```

## 2. Cloudflare R2 (prod)

R2 is S3-compatible and gives 10 GB free storage + free egress — best dev-tier
cold-storage option.

### Step 1: Create the bucket

R2 doesn't auto-create buckets on first PUT. Create it first:

```bash
# Option A: Cloudflare dashboard → R2 → Create bucket
# Option B: wrangler CLI (if installed)
wrangler r2 bucket create akosha-cold-data
```

Bucket names are global (like S3) — pick something unique.

### Step 2: Generate an API token

Cloudflare dashboard → R2 → Manage R2 API Tokens → Create token. Choose:
- Object Read & Write permissions (not Admin)
- Scope to the bucket you just created
- TTL: as short as you're comfortable with (rotate regularly)

Copy the **Access Key ID** and **Secret Access Key**.

### Step 3: Configure akosha

In `settings/local.yaml` (gitignored):

```yaml
cold:
  backend: s3
  bucket: akosha-cold-data  # your bucket name
  prefix: conversations/
  format: parquet
  region: auto  # R2 mandatory
  endpoint_url: https://<YOUR_ACCOUNT_ID>.r2.cloudflarestorage.com
```

Set credentials via env vars (preferred — keep `local.yaml` out of git):

```bash
export AKOSHA__STORAGE__COLD__ACCESS_KEY_ID=<your-access-key-id>
export AKOSHA__STORAGE__COLD__SECRET_ACCESS_KEY=<your-secret-access-key>
```

### Step 4: Credential rotation

R2 tokens don't auto-rotate. Periodically:

1. Generate a new token in the Cloudflare dashboard.
2. Update env vars / `local.yaml`.
3. Revoke the old token in the Cloudflare dashboard.

## 3. Smoke-test recipe

After configuring R2 (or fake-gcs), verify the wiring end-to-end:

```bash
akosha mcp start
akosha cold export --batch sample.json
akosha cold list
```

If `cold export` fails with a 401 / `SignatureDoesNotMatch`, your credentials
or region are wrong. If it fails with `ConnectionError`, your `endpoint_url`
is wrong. Check `akosha logs` for the actual error.

## Health checks

- fake-gcs-server: `curl http://127.0.0.1:4443/storage/v1/b/o`
- R2: not directly health-checkable without credentials; rely on the
  smoke-test recipe above.

## Logs

Successful cold exports emit:

```
INFO akosha.storage.cold_store Successfully exported N records to conversations/...
INFO oneiric.adapters.storage.gcs gcs-endpoint-override endpoint=http://127.0.0.1:4443
```

If `gcs-endpoint-override` doesn't appear when you expect it, your
`endpoint_url` env var didn't bind — check `ColdStorageConfig` loading.
```

- [ ] **Step 2: Commit**

```bash
cd /Users/les/Projects/akosha
git add docs/operators/cold-tier-quickstart.md
git commit -m "docs(akosha): cold-tier operator quickstart

Operator-facing guide for the three cold-tier scenarios:

1. fake-gcs-server in dev (brew install + direct binary invocation).
2. Cloudflare R2 in prod (bucket create + API token + endpoint URL).
3. Smoke-test recipe for verifying wiring before committing to a vendor.

Plus log shape + credential rotation notes. Per the spec's
integration contract: this doc is how operators verify the spec's
\"Demonstrable by\" clause.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Verification

After all tasks land:

- [ ] **All 9 commits land on their respective `main` branches** — verify with `git log --oneline -3` in each repo.
- [ ] **Oneiric full GCS test suite passes**: `cd /Users/les/Projects/oneiric && pytest tests/adapters/test_storage_adapters.py tests/unit/adapters/storage/test_gcs_stream.py -v`
- [ ] **Akosha full integration test passes**: `cd /Users/les/Projects/akosha && pytest tests/integration/test_cold_tier_local.py -v`
- [ ] **Akosha full config test passes**: `cd /Users/les/Projects/akosha && pytest tests/unit/test_config.py -v`
- [ ] **No regressions in either repo's full test suite** (run the full `pytest` in each repo).

## Rollback signals

- **Phase A**: revert commit `f6c0f2b` (or the worktree branch); oneiric GCS adapter returns to no-`endpoint_url` behavior. `GCSStorageSettings` no longer accepts the field.
- **Phase B1**: revert the B1.2 commit; `ColdStorageConfig` returns to 5 fields. No behavior change (new fields are unused).
- **Phase B2**: revert B2.2 + B2.3 + B2.4 commits; `ColdStore` and `StandardMode` return to not forwarding the new fields. B2 rollback is safe BEFORE B1 is used by any external caller; after B1 lands, env vars set by operators will silently no-op until the wiring lands too.
- **Phase C**: revert C1 + C2 commits; integration test removed + docs removed. No behavior change.

## Plan complete

All 9 tasks across 4 phases. Spec: `akosha/docs/specs/2026-09-27-cold-tier-fake-gcs-design.md` (commits `e9acd5a`, `8cebdd7`).
