---
status: active
role: implementation
topic: cold-tier-fake-gcs
date: 2026-09-27
last_reviewed: 2026-09-27
---

# Cold Tier: fake-gcs-server default + Cloudflare R2 for local XDG

## Context

Akosha's cold tier is the Parquet-archival substrate for conversation
data, embeddings, and derived exports. The substrate decision today
splits across four oneiric adapters (`local`, `s3`, `gcs`, `azure`),
each with different production-quality free tiers and dev story:

| Backend | Production | Dev today | Problem |
|---|---|---|---|
| `local` | n/a | works | not a real substrate; no cross-host sharing |
| `s3` | works (R2 pattern) | works | healthy adapter, but `akosha.yaml` cold block doesn't expose `endpoint_url` or credentials |
| `gcs` | works | **broken** | oneiric `GCSStorageAdapter` doesn't accept `endpoint_url`; the official `google-cloud-storage` SDK always talks to `storage.googleapis.com` |
| `azure` | works | works | healthy adapter, but Azure free tier requires a credit card |

A dev-tier decision is overdue because:

1. **There is no committed cold-tier default that works on `git clone && akosha mcp start`.** Today `akosha.yaml` ships `cold.backend=local`, which is just a filesystem path — no archival semantics.
2. **fake-gcs-server is already Homebrew-installed and session-buddy ships lifecycle scripts** (`scripts/fake-gcs-{start,stop,init}.sh` + `session-buddy-fake-gcs` console script) but nothing in the Bodai ecosystem wires them into akosha's cold tier.
3. **Cloudflare R2's free tier (10 GB storage, 1M Class A, 10M Class B, free egress)** is the best production-grade archival choice for individual developers — but the current `akosha.yaml` `cold:` block has no fields to express an R2 endpoint or credentials, and `ColdStorageConfig` has no matching fields either. The R2 path would silently 401 at runtime.

This design wires both: **fake-gcs-server as the documented opt-in dev path** (committed default stays `local` so a fresh `git clone` user doesn't see hard startup failures), and **R2 as the documented personal XDG override** (so the operator gets a real cloud substrate when they want it).

## Goals

1. **Out-of-the-box cold tier.** `git clone && uv sync && akosha mcp start` runs against the committed `local` default — works with zero dependencies.
2. **One-line opt-in to fake-gcs.** Setting `cold.backend=gcs` + `cold.endpoint_url=http://127.0.0.1:4443` in `local.yaml` (or via env var) switches to fake-gcs-server without code changes.
3. **One-line vendor swap to R2.** Setting `cold.backend=s3` + R2 endpoint + R2 credentials switches the cold tier to Cloudflare R2 without code changes.
4. **No committed credentials.** All real credentials live in `local.yaml` (gitignored) or env vars.
5. **CI coverage on the GCS path.** `pytest -k test_cold_tier_local` runs against a fake-gcs-server subprocess and exercises the full Parquet export → upload → list → download round trip.
6. **Operator smoke-test recipe for R2.** Cheap end-to-end check operators run against their own R2 account before committing to it.

## Non-goals

- Real GCS / Azure setup (operators pick a cloud vendor on their own).
- Mahavishnu's separate OTel pgvector storage path (separate code path; documented follow-up).
- fastblocks / splashstand storage (no Bodai-tier model — confirmed via the 2026-09-27 brainstorm; fastblocks migrated from ACB to oneiric but doesn't carry the Bodai tier model).
- Multi-bucket cold tier (single bucket per akosha instance, matching today's `ColdStore` semantics).
- Migrating the existing flat `AKOSHA_COLD_*` env vars to the nested `AKOSHA__STORAGE__COLD__*` form (separate cleanup PR; the new nested form supersedes the flat ones and old env vars become silent no-ops, documented in the migration guide).

## Decisions

### D1. Committed default stays `local`; `gcs` is opt-in

**`akosha/settings/akosha.yaml`** keeps `cold.backend: local` as the committed default — works with zero external dependencies. The `gcs` (and `s3`) backends are documented opt-in via `local.yaml` or env vars.

`local.yaml` snippet for fake-gcs (opt-in dev):

```yaml
# settings/local.yaml (gitignored)
cold:
  backend: gcs
  bucket: akosha-cold-data
  prefix: conversations/
  format: parquet
  endpoint_url: http://127.0.0.1:4443   # fake-gcs-server loopback
  project: local-dev
  anonymous_credentials: true            # skip GCP service-account JSON
  region: us-west-2                      # unused for gcs; kept for parity
```

**Why `local` as committed default:** shipping `gcs` would mean a fresh `git clone` user without fake-gcs-server running hits hard startup failures. Keeping `local` preserves the forgiving no-op fallback; the `cold.backend=local` rollback signal in the integration contract (below) keeps working.

**Why hardcoded `http://127.0.0.1:4443` in local.yaml is fine:** it's a loopback emulator address, not a credentialed URL. The `feedback-bodai-claude-decisions-gitignore-2026-09-26` rule about not hardcoding URLs applies to credentialed/external endpoints, not loopback emulators.

**STORAGE_EMULATOR_HOST env var (future):** the Google Cloud Storage SDK natively honors `STORAGE_EMULATOR_HOST` for emulator setups. Phase D (deferred) could surface this as a oneiric config layer; out of scope for this design but worth a follow-up issue.

### D2. Oneiric `GCSStorageAdapter` accepts `endpoint_url` only (no separate `anonymous_credentials`)

**Files touched:** `oneiric/oneiric/adapters/storage/gcs.py`, plus tests.

`GCSStorageSettings` grows one field:

```python
endpoint_url: str | None = Field(
    default=None,
    description=(
        "Override the GCS API endpoint. Required for non-Google emulators "
        "such as fake-gcs-server. Pass http://127.0.0.1:4443 in dev; "
        "leave None for real GCS."
    ),
)
```

`init()` builds `client_options` with both `api_endpoint` and the SDK's built-in `use_auth_w_custom_endpoint=False` flag (which auto-wires `AnonymousCredentials` — no explicit import needed):

```python
client_kwargs: dict[str, Any] = {}
client_options: dict[str, Any] = {}
if self._settings.endpoint_url:
    client_options["api_endpoint"] = self._settings.endpoint_url
    client_options["use_auth_w_custom_endpoint"] = False
if client_options:
    client_kwargs["client_options"] = client_options

if self._settings.credentials_file:
    client_kwargs["credentials"] = (
        service_account.Credentials.from_service_account_file(
            str(self._settings.credentials_file)
        )
    )

if self._settings.project:
    client_kwargs["project"] = self._settings.project

self._client = storage.Client(**client_kwargs)

if self._settings.endpoint_url:
    self._logger.info("gcs-endpoint-override", endpoint=self._settings.endpoint_url)
```

**Why scope this minimal:** the existing fakes (`_FakeGCSBlob` / `_FakeGCSBucket` in `tests/adapters/test_storage_adapters.py:113-147`) cover upload/download/delete/list/exists at the adapter boundary; no new fake methods needed. The pattern at `tests/adapters/test_storage_adapters.py:267-305` (capture `**kwargs` into a `created` list and assert on the dict) extends directly to endpoint-url assertions.

**Why SDK-blessed `client_options` form, not explicit `AnonymousCredentials`:** the official `googleapis/python-storage` docs document `client_options={"api_endpoint": ..., "use_auth_w_custom_endpoint": False}` as the canonical pattern. The SDK handles `AnonymousCredentials` internally; passing both is redundant. One approach, no mixing.

**Single INFO log on init when `endpoint_url` is set:** useful for `grep`, zero logic, no false positives. Replaces an earlier "defensive layer" idea (Risk #2) that was over-engineered.

### D3. Akosha `ColdStorageConfig` exposes six new fields with nested env-var binding

**Files touched:** `akosha/akosha/config.py`, `akosha/settings/akosha.yaml`, `akosha/akosha/modes/standard.py`, `akosha/akosha/storage/cold_store.py`.

`ColdStorageConfig` (in `akosha/config.py`) is rewritten to use the manual `__init__` env-binding pattern that `HotStorageConfig` (lines 94-99) and `EventBridgeConfig` (lines 248-261) use. Fields added:

```python
endpoint_url: str | None = Field(default=None, description=...)
project: str | None = Field(
    default=None,
    description=(
        "GCP project ID for the gcs backend. Ignored on the s3 backend. "
        "Set via AKOSHA__STORAGE__COLD__PROJECT."
    ),
)
anonymous_credentials: bool = Field(
    default=False,
    description=(
        "Use anonymous GCS credentials via the SDK's built-in "
        "use_auth_w_custom_endpoint=False. Required for fake-gcs-server. "
        "Ignored on the s3 backend. Set via "
        "AKOSHA__STORAGE__COLD__ANONYMOUS_CREDENTIALS."
    ),
)
access_key_id: str | None = Field(
    default=None,
    description=(
        "S3 access key ID for the s3 backend (Cloudflare R2, etc.). "
        "Ignored on the gcs backend. Set via "
        "AKOSHA__STORAGE__COLD__ACCESS_KEY_ID."
    ),
)
secret_access_key: str | None = Field(
    default=None,
    description=(
        "S3 secret access key for the s3 backend (Cloudflare R2, etc.). "
        "Ignored on the gcs backend. Set via "
        "AKOSHA__STORAGE__COLD__SECRET_ACCESS_KEY."
    ),
)
region: str | None = Field(
    default=None,
    description=(
        "S3 region. Cloudflare R2 requires 'auto'. Ignored on the gcs backend. "
        "Set via AKOSHA__STORAGE__COLD__REGION."
    ),
)
```

`ColdStorageConfig.__init__` adds manual `os.getenv` bindings matching the `HotStorageConfig` pattern — OneiricMCPConfig does not auto-bind nested env vars.

**Backward-compat note for existing `AKOSHA_COLD_*` flat env vars:** the existing `AKOSHA_COLD_ENDPOINT` (referenced in `QUICKSTART.md:81` but unbound today) becomes a fallback binding for `endpoint_url`. Same pattern for `AKOSHA_COLD_REGION` → `region`. Flat `AKOSHA_COLD_BACKEND`/`AKOSHA_COLD_BUCKET` continue to work via the existing `Field(default_factory=...)` bindings.

**Two wiring sites, not one.** Both must change:

1. **`akosha/akosha/modes/standard.py:initialize_cold_storage()` (lines 98-148)** — currently instantiates `S3StorageSettings(bucket=bucket)` directly without forwarding `endpoint_url`/`access_key_id`/`secret_access_key`/`region`. This branch needs to pass all four fields through.

2. **`akosha/akosha/storage/cold_store.py:initialize()` (gcs branch, around line 315)** — currently passes only `project` and `credentials_file` to `GCSStorageSettings(...)`. Needs to forward `endpoint_url` too.

### D4. Operator's `local.yaml` for R2 — full credential plumbing

**Files touched:** `akosha/settings/local.yaml` (gitignored, per-user). Oneiric honors `~/.config/akosha/local.yaml` via `oneiric/core/config.py:457` (layer 4 of its config stack).

```yaml
# settings/local.yaml (gitignored) or ~/.config/akosha/local.yaml
cold:
  backend: s3                                # R2 speaks S3
  bucket: <user-r2-bucket-name>
  prefix: conversations/
  format: parquet
  region: auto                               # R2 mandatory
  endpoint_url: https://<accountid>.r2.cloudflarestorage.com
  # access_key_id / secret_access_key come from env (preferred):
  #   export AKOSHA__STORAGE__COLD__ACCESS_KEY_ID=...
  #   export AKOSHA__STORAGE__COLD__SECRET_ACCESS_KEY=...
```

**Why `s3` not `gcs` for R2:** R2's API surface is S3-compatible; oneiric's `S3StorageAdapter.endpoint_url` already exists. Using S3 here means zero oneiric changes for the R2 path. The GCS path is for fake-gcs (dev) and would be for real GCS (prod alternative).

**Why R2 over the PaaS vendors evaluated:**

| Vendor | Blob free tier | Why not |
|---|---|---|
| Neon | ❌ none | Postgres-only |
| Upsun | ⚠️ disk only | 15-day trial, pay-as-you-go |
| Render | ❌ none | Postgres + ephemeral disk only |
| Railway | 10 GB blob | viable but tighter free egress than R2 |
| Leapcell | 1 GB blob | too small for any real archival |
| **Cloudflare R2** | **10 GB + free egress** | **best dev-tier cold storage** |

(Full vendor matrix lives in the conversation transcript 2026-09-27.)

**Operator walkthrough — three steps the spec must cover:**

1. **Create the bucket.** R2 doesn't auto-create on first PUT like fake-gcs. Operator runs `wrangler r2 bucket create <bucket-name>` or uses the Cloudflare dashboard.
2. **Generate R2 API tokens.** Cloudflare dashboard → R2 → Manage R2 API Tokens → Create token. Copy the access key ID and secret access key into env vars (preferred) or `local.yaml`.
3. **Set the region.** `auto` is mandatory; R2 rejects empty or other region values.

**Credential rotation:** R2 tokens don't auto-rotate. Operators regenerate tokens in the Cloudflare dashboard, then update env vars (preferred) or `local.yaml`. Documented in `cold-tier-quickstart.md`.

**Bucket-name uniqueness constraint:** R2 bucket names are global (like S3); operators pick names that won't collide. Documented in `cold-tier-quickstart.md`.

### D5. CI exercises the GCS path; R2 is operator smoke-test

**CI cold-tier integration test** (`akosha/tests/integration/test_cold_tier_local.py`):

```python
# Pseudocode for the test
def test_cold_tier_fake_gcs_round_trip():
    data_dir = tempfile.mkdtemp(prefix="akosha-fake-gcs-")
    proc = subprocess.Popen(
        [
            "fake-gcs-server",
            "-filesystem-root", data_dir,
            "-port", "4443",
            "-host", "127.0.0.1",
            "-public-host", "127.0.0.1:4443",
            "-location", "US-CENTRAL1",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        wait_for_port("127.0.0.1", 4443, timeout=10)
        # Configure akosha for fake-gcs:
        #   backend=gcs, endpoint_url=http://127.0.0.1:4443,
        #   anonymous_credentials=true, project=local-dev
        # Export a ColdRecord batch, assert the Parquet blob lands
        # at data_dir/akosha-cold-data/conversations/.
        # List it back, download it, assert contents match.
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        shutil.rmtree(data_dir, ignore_errors=True)
```

**Why subprocess, not session-buddy's shell script:** `fake-gcs-server` is the actual binary; the shell scripts (`session-buddy/scripts/fake-gcs-start.sh`) are thin wrappers. Calling the binary directly eliminates cross-repo coupling and works on any machine with `fake-gcs-server` on PATH (already Homebrew-installed per the operator's environment).

**R2 is operator smoke-test, not CI.** R2 credentials in CI secrets is a maintenance burden (rotation, scope). The `cold-tier-quickstart.md` includes a recipe operators run against their own R2 account:

```bash
akosha mcp start
akosha cold export --batch sample.json
akosha cold list
```

Catches credential/endpoint misconfig without CI cost.

### D6. Phase plan — split B into B1 + B2

- **Phase A (oneiric)** — D2. Extend `GCSStorageAdapter` with `endpoint_url`. Tests. One PR to oneiric.
- **Phase B1 (akosha)** — D3 config surface + D5 integration test. Add the six new fields to `ColdStorageConfig` with nested env-var bindings. Update `akosha/settings/akosha.yaml` (only to comment-document the new fields, since the default stays `local`). Land even before Phase A.
- **Phase B2 (akosha)** — D3 wiring + D4 R2 env. After Phase A merges: extend `modes/standard.py` and `cold_store.py:initialize()` to forward the new fields into oneiric's settings. Land the operator quickstart doc.
- **Phase C (operator docs)** — `cold-tier-quickstart.md`. Same PR as Phase B2.
- **Phase D (deferred)** — STORAGE_EMULATOR_HOST env-var consideration; migrate flat `AKOSHA_COLD_*` to nested form.

The split between B1 and B2 lets the config surface ship and be reviewed independently of the wiring. If Phase A gets stuck, B1 still has value as dead-code reference for the eventual implementation.

## Integration contract (per `wire-up-contract.md`)

- **Triggered from:** `akosha mcp start` with `mode=standard` reads `cold.backend` + `cold.endpoint_url` (from YAML or env) → constructs `ColdStore(...)` via `modes/standard.py:initialize_cold_storage()` → cold export path uses the configured adapter.
- **Returns to / updates:** GCS-compatible bucket (fake-gcs-server on `127.0.0.1:4443` in dev), S3-compatible bucket (Cloudflare R2 in operator's local XDG), or local filesystem (`local` default fallback).
- **Demonstrable by:** `pytest -k test_cold_tier_local` runs `fake-gcs-server` via subprocess, exports a `ColdRecord` batch, asserts the Parquet blob lands at the configured `data_dir`, lists + downloads it back. Operator smoke-test recipe in `cold-tier-quickstart.md` does the same against the operator's R2 account.
- **Rollback signal:** setting `cold.backend=local` reverts to the existing `LocalStorageAdapter` (no-op fallback already in `ColdStore`).
- **Observability added:** structured log on `cold_export_total{backend,status}` counter; `cold_storage_health` gauge on `/health`.

## File-level change list

| File | Change | Lines (est.) |
|---|---|---|
| `oneiric/oneiric/adapters/storage/gcs.py` | +`endpoint_url` on `GCSStorageSettings`; +`client_options` block in `init()` with `use_auth_w_custom_endpoint=False`; +INFO log on endpoint override | +20 |
| `oneiric/tests/unit/adapters/storage/test_gcs_*.py` | +fake-gcs-server-style test cases (endpoint_url set; endpoint_url unset = no client_options) | +60 |
| `akosha/akosha/config.py` | Rewrite `ColdStorageConfig` with manual `__init__` env-binding; +6 fields (endpoint_url, project, anonymous_credentials, access_key_id, secret_access_key, region); +fallback bindings for `AKOSHA_COLD_ENDPOINT` and `AKOSHA_COLD_REGION` | +60 |
| `akosha/settings/akosha.yaml` | Comment-document the new fields (defaults unchanged) | +10 |
| `akosha/akosha/modes/standard.py` | Wire `endpoint_url` + `access_key_id` + `secret_access_key` + `region` into `S3StorageSettings` for the s3 branch | +10 |
| `akosha/akosha/storage/cold_store.py` | Wire `endpoint_url` into `GCSStorageSettings` for the gcs branch | +5 |
| `akosha/tests/integration/test_cold_tier_local.py` (NEW) | subprocess-based integration test against fake-gcs-server | +100 |
| `akosha/docs/operators/cold-tier-quickstart.md` (NEW) | fake-gcs setup, R2 walkthrough, operator smoke-test recipe, credential rotation, bucket naming | +150 |

Total: ~415 lines across 8 files. Three PRs: Phase A (oneiric), Phase B1 (akosha config), Phase B2 (akosha wiring + docs).

## Risks

1. **GCS SDK behavior with `client_options` is technically supported but lightly documented for non-Google emulators.** If a real GCS call later breaks with `client_options` set, we'd need to detect real-vs-emulator and only set the override in emulator mode. Mitigation: a single INFO log on init when `endpoint_url` is set; real-GCS users won't see it.
2. **`anonymous_credentials=true` accidentally set against real GCS** would produce confusing 401s. Mitigation: the same INFO log line in §D2 makes the override visible in startup logs.
3. **fake-gcs-server data dir is in the test's tmpdir, not `~/.cache/session-buddy/fake-gcs`.** This decouples the test from session-buddy's lifecycle scripts but loses the "operator can browse the data dir" convenience. Acceptable: the test is hermetic.
4. **Cloudflare R2 free-tier changes** (limits, region requirements, etc.) — operators follow the Cloudflare docs at deploy time; not our problem.
5. **Two wiring sites (modes/standard.py + cold_store.py)** increase review surface and regression risk. Mitigation: Phase B2 PR description names both sites and explicitly tests both backends via the integration test.

## Out of scope (explicit)

- Mahavishnu's OTel pgvector storage (`mahavishnu/ingesters/otel_ingester.py`) — separate code path. The warm-tier config (`AKOSHA__STORAGE__WARM__BACKEND=pgvector`) is independent and not affected by this design.
- fastblocks/splashstand — no Bodai-tier cold/warm/hot model.
- Multi-bucket cold tier — single bucket per akosha instance; matches today's `ColdStore` semantics.
- Migrating flat `AKOSHA_COLD_*` to nested form — separate cleanup PR; flat env vars continue to work via `Field(default_factory=...)` and the new `endpoint_url` fallback binding.
- `STORAGE_EMULATOR_HOST` oneiric config layer — Phase D follow-up.

## Review log

- 2026-09-27: Initial draft committed (`e9acd5a`).
- 2026-09-27: Three-agent review pass incorporated; spec rewritten (this commit). Material changes:
  - §D1: kept committed default as `local`, gated `gcs` behind `local.yaml` opt-in (avoids hard startup failure on fresh clone).
  - §D2: dropped defensive layer + separate `anonymous_credentials` field; use SDK's built-in `use_auth_w_custom_endpoint=False`.
  - §D3: split into B1 (config) + B2 (wiring); identified two wiring sites, not one; added `access_key_id` + `secret_access_key` (catches R2 silent-401 bug).
  - §D4: added `region: auto` and operator walkthrough (bucket create, API token, region requirement).
  - §D5: integration test calls `fake-gcs-server` directly via subprocess; R2 reframed as operator smoke-test.
