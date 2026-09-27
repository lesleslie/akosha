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
| `s3` | works (R2 pattern) | works | healthy adapter, but `akosha.yaml` doesn't expose `endpoint_url` for non-AWS endpoints |
| `gcs` | works | **broken** | oneiric `GCSStorageAdapter` doesn't accept `endpoint_url`; official `google-cloud-storage` SDK always talks to `storage.googleapis.com` |
| `azure` | works | works | healthy adapter, but Azure free tier requires a credit card |

A dev-tier decision is overdue because:

1. **There is no committed cold-tier default that works on `git clone && akosha mcp start`.** Today `akosha.yaml` ships `cold.backend=local`, which is just a filesystem path — no archival semantics.
2. **fake-gcs-server is already Homebrew-installed and session-buddy ships lifecycle scripts** (`scripts/fake-gcs-{start,stop,init}.sh` + `session-buddy-fake-gcs` console script) but nothing in the Bodai ecosystem wires them into akosha's cold tier.
3. **Cloudflare R2's free tier (10 GB storage, 1M Class A, 10M Class B, free egress)** is the best production-grade archival choice for individual developers — but the current `akosha.yaml` `cold:` block has no fields to express an R2 endpoint.

This design wires both: **fake-gcs-server as the committed default** (so anyone can run the full cold tier with no account), and **R2 as the documented personal XDG override** (so the operator gets a real cloud substrate when they want it).

## Goals

1. **Out-of-the-box cold tier.** `git clone && uv sync && akosha mcp start` exercises the full cold-tier code path against a locally-running fake-gcs-server.
2. **One-line vendor swap.** Moving from dev (fake-gcs) to prod (R2) is a `local.yaml` edit, not a code change.
3. **No committed credentials.** All real credentials live in `local.yaml` (gitignored) or env vars.
4. **CI coverage on the GCS path.** `pytest -k test_cold_tier_local` runs against fake-gcs-server and exercises the full Parquet export → upload → list → download round trip.

## Non-goals

- Real GCS / Azure setup (operators pick a cloud vendor on their own).
- mahavishnu's separate OTel pgvector storage path (separate code path; documented follow-up).
- fastblocks / splashstand storage (no Bodai-tier model — out of scope per the brainstorm on 2026-09-27).
- multi-bucket cold tier (single bucket per akosha instance, matching today's `ColdStore` semantics).

## Decisions

### D1. Committed default = `gcs` + `fake-gcs-server`

**`akosha/settings/akosha.yaml`** ships with:

```yaml
cold:
  backend: gcs
  bucket: akosha-cold-data
  prefix: conversations/
  format: parquet
  endpoint_url: http://127.0.0.1:4443   # fake-gcs-server
  project: local-dev
  anonymous_credentials: true            # skip GCP service-account JSON
  region: us-west-2                      # unused for gcs; kept for parity
```

**Why `gcs` not `s3` as the committed default:** the akosha `ColdStore` constructor already routes `gcs` through `oneiric.adapters.storage.gcs.GCSStorageAdapter`, which is the adapter this design extends (D2). The `s3` adapter already supports `endpoint_url` but the committed `akosha.yaml` cold block doesn't expose it — extending akosha's config surface to add `endpoint_url` + `project` + `anonymous_credentials` to the `s3` path is a separate follow-up.

**Why the URL is hardcoded in committed yaml:** `http://127.0.0.1:4443` is a loopback emulator address, not a credentialed URL. The `feedback-bodai-claude-decisions-gitignore-2026-09-26` rule about not hardcoding URLs applies to credentialed/external endpoints, not loopback emulators.

### D2. Oneiric `GCSStorageAdapter` accepts `endpoint_url` + `anonymous_credentials`

**Files touched:** `oneiric/oneiric/adapters/storage/gcs.py`, plus tests.

`GCSStorageSettings` grows two fields:

```python
endpoint_url: str | None = Field(
    default=None,
    description=(
        "Override the GCS API endpoint. Required for non-Google emulators "
        "such as fake-gcs-server. Pass http://127.0.0.1:4443 in dev; "
        "leave None for real GCS."
    ),
)
anonymous_credentials: bool = Field(
    default=False,
    description=(
        "Skip service-account JSON and use anonymous credentials. Required "
        "for emulators like fake-gcs-server that don't enforce auth."
    ),
)
```

`init()` builds `client_options` and switches on `anonymous_credentials`:

```python
client_kwargs: dict[str, Any] = {}
client_options: dict[str, Any] = {}
if self._settings.endpoint_url:
    client_options["api_endpoint"] = self._settings.endpoint_url
if client_options:
    client_kwargs["client_options"] = client_options

if self._settings.anonymous_credentials:
    client_kwargs["credentials"] = AnonymousCredentials()
elif self._settings.credentials_file:
    client_kwargs["credentials"] = (
        service_account.Credentials.from_service_account_file(
            str(self._settings.credentials_file)
        )
    )

if self._settings.project:
    client_kwargs["project"] = self._settings.project

self._client = storage.Client(**client_kwargs)
```

**Why scope this minimal:** the existing fakes (`_FakeGCSBlob` / `_FakeGCSBucket` in `tests/adapters/test_storage_adapters.py`) plus the new endpoint-url test case give us high-confidence coverage without changing the adapter's public surface beyond the two fields.

**Why `AnonymousCredentials` from `google.auth.credentials`:** that's what the official GCS SDK and `gcsfake-server.dev` documentation recommend for emulator setups; it's a one-line import and the SDK handles the rest.

### D3. Akosha exposes cold-tier fields in committed config

**Files touched:** `akosha/akosha/config.py`, `akosha/settings/akosha.yaml`, `akosha/akosha/modes/standard.py`.

`ColdStorageConfig` (in `akosha/config.py`) grows three fields matching the `ColdStore` constructor:

```python
endpoint_url: str | None = Field(
    default=None,
    description=(
        "Override the GCS / S3 API endpoint. Use http://127.0.0.1:4443 "
        "for fake-gcs-server in dev, your R2 endpoint URL in prod. "
        "Set via AKOSHA__STORAGE__COLD__ENDPOINT_URL."
    ),
)
project: str | None = Field(
    default=None,
    description=(
        "GCP project ID for the gcs backend. Set via "
        "AKOSHA__STORAGE__COLD__PROJECT."
    ),
)
anonymous_credentials: bool = Field(
    default=False,
    description=(
        "Use anonymous GCS credentials. Required for fake-gcs-server; "
        "leave false for real GCS / R2-via-gcs-protocol. Set via "
        "AKOSHA__STORAGE__COLD__ANONYMOUS_CREDENTIALS."
    ),
)
```

`akosha/modes/standard.py:initialize_cold_storage()` (line ~98-148) wires `endpoint_url` and `project` into the `ColdStore(...)` constructor call. The current code only passes `backend`, `bucket`, and `prefix`; this design fills the gap.

### D4. Operator's `local.yaml` overrides to R2

**Files touched:** `akosha/settings/local.yaml` (gitignored, per-user).

```yaml
cold:
  backend: s3                                # R2 speaks S3
  bucket: <user-r2-bucket-name>
  prefix: conversations/
  format: parquet
  region: auto
  endpoint_url: https://<accountid>.r2.cloudflarestorage.com
  # access_key_id / secret_access_key come from env:
  #   export AKOSHA__STORAGE__COLD__S3__ACCESS_KEY_ID=...
  #   export AKOSHA__STORAGE__COLD__S3__SECRET_ACCESS_KEY=...
```

**Why `s3` not `gcs` for R2:** R2's API surface is S3-compatible; oneiric's `S3StorageAdapter.endpoint_url` already exists (D2 makes the equivalent for GCS). Using S3 here means zero oneiric changes for the R2 path. The GCS path is for fake-gcs (dev) and would be for real GCS (prod alternative).

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

### D5. CI exercises the GCS path only

The CI cold-tier integration test (`akosha/tests/integration/test_cold_tier_local.py`) starts fake-gcs-server as a subprocess (re-using session-buddy's `scripts/fake-gcs-start.sh`), exports a `ColdRecord` batch, asserts the Parquet blob lands at the expected path, and tears down the emulator.

The S3 path (R2) is **not** in CI — R2 credentials would have to live in CI secrets and rotate. Operators verify the S3 path on their own machines against their R2 account; the contract is that the same `ColdStore.export_batch()` code path covers both backends.

## Integration contract (per `wire-up-contract.md`)

- **Triggered from:** `akosha mcp start` with `mode=standard` reads `cold.backend` + `cold.endpoint_url` → constructs `ColdStore(...)` → `initialize_cold_storage()` calls `oneiric.adapters.storage.<backend>.init()` → cold export path uses the adapter.
- **Returns to / updates:** GCS-compatible bucket (fake-gcs-server on `127.0.0.1:4443` in dev, Cloudflare R2 in operator's local XDG).
- **Demonstrable by:** `pytest -k test_cold_tier_local` runs `session-buddy-fake-gcs start`, exports a `ColdRecord` batch, asserts the Parquet blob lands at `~/.cache/session-buddy/fake-gcs/akosha-cold-data/conversations/`.
- **Rollback signal:** setting `cold.backend=local` reverts to the existing `LocalStorageAdapter` (already wired as a no-op fallback in `ColdStore`).
- **Observability added:** structured log on `cold_export_total{backend,status}` counter; `gcs_cold_health` gauge on `/health`.

## File-level change list

| File | Change | Lines (est.) |
|---|---|---|
| `oneiric/oneiric/adapters/storage/gcs.py` | +`endpoint_url`, +`anonymous_credentials`; +`client_options` in `init()`; +`AnonymousCredentials` import | +30 |
| `oneiric/tests/unit/adapters/storage/test_gcs_*.py` | +fake-gcs-server-style test case (endpoint_url + anon) | +80 |
| `akosha/akosha/config.py` | +3 fields on `ColdStorageConfig` + env-var binding | +25 |
| `akosha/settings/akosha.yaml` | extend `cold:` block with `endpoint_url`, `project`, `anonymous_credentials` | +5 |
| `akosha/akosha/modes/standard.py` | wire `endpoint_url` + `project` + `anonymous_credentials` into `ColdStore(...)` constructor | +5 |
| `akosha/tests/integration/test_cold_tier_local.py` (NEW) | subprocess-based integration test against fake-gcs-server | +120 |
| `akosha/docs/operators/cold-tier-quickstart.md` (NEW) | how to flip from fake-gcs to R2 | +80 |

Total: ~345 lines across 7 files. Two PRs (one per repo: oneiric, akosha).

## Risks

1. **GCS SDK behavior with `api_endpoint` is technically supported but lightly documented.** If a real GCS call (not fake-gcs) later breaks with `client_options` set, we'd need to detect real-vs-emulator and only set `api_endpoint` when it's an emulator. Mitigation: the test suite covers both with fakes; a real GCS user would notice immediately.
2. **`anonymous_credentials=true` accidentally set against real GCS** would produce confusing 401s. Mitigation: log a warning on `init()` if both `anonymous_credentials=true` AND `endpoint_url` is unset (suggests real GCS misconfig).
3. **fake-gcs-server data dir lives in `~/.cache/session-buddy/fake-gcs`** — this couples akosha's dev storage to a session-buddy-owned path. Acceptable for now (the lifecycle scripts already exist there); flagged as a follow-up if session-buddy ever moves that path.

## Out of scope (explicit)

- Mahavishnu's OTel pgvector storage (`mahavishnu/ingesters/otel_ingester.py`) — separate code path. The warm-tier config (`AKOSHA__STORAGE__WARM__BACKEND=pgvector`) is independent and not affected by this design.
- fastblocks/splashstand — no Bodai-tier cold/warm/hot model. (Confirmed via the 2026-09-27 brainstorm — fastblocks migrated from ACB to oneiric but doesn't carry the Bodai tier model.)
- Multi-bucket cold tier — single bucket per akosha instance; matches today's `ColdStore` semantics.
- R2 in CI — credentials + rotation concerns outweight the value.

## Phase plan

- **Phase A (oneiric)**: D2 — extend `GCSStorageAdapter` with `endpoint_url` + `anonymous_credentials`. Tests. One PR to oneiric.
- **Phase B (akosha)**: D3 — extend `ColdStorageConfig` + `akosha.yaml` + `modes/standard.py`. Integration test (D5). One PR to akosha.
- **Phase C (operator docs)**: `cold-tier-quickstart.md`. Same PR as Phase B.
- **Phase D (deferred)**: R2 setup docs once Phase B is verified end-to-end against fake-gcs.

## Review log

- 2026-09-27: Initial draft committed to `akosha/docs/superpowers/specs/`.
