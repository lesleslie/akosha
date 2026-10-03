---
status: draft
role: implementation
kind: plan
date: 2026-09-28
last_reviewed: 2026-09-28
superseded_by: null
blocks_on: []
blocks: []
activation_signal: ColdStore GCS branch is missing endpoint_url wiring; fake-gcs-server round-trip (B2) cannot pass until this is fixed.
topic: cold-store-wiring
---

# ColdStore GCS Branch — Missing endpoint_url Wiring (TICKET)

## What

`tests/integration/test_cold_tier_local.py::test_cold_store_gcs_branch_forwards_endpoint_url`
fails with `KeyError: 'endpoint_url'`. The test asserts that
`GCSStorageSettings` constructed by `ColdStore.initialize(backend='gcs')`
includes the `endpoint_url` field, so fake-gcs-server (and other
GCS-compatible endpoints) can be used.

## Failing test (verbatim, as of 2026-09-28)

```
$ .venv/bin/python -m pytest tests/integration/test_cold_tier_local.py::test_cold_store_gcs_branch_forwards_endpoint_url -x --no-header
tests/integration/test_cold_tier_local.py:56: in test_cold_store_gcs_branch_forwards_endpoint_url
    assert settings["endpoint_url"] == "http://127.0.0.1:4443"
E   KeyError: 'endpoint_url'
FAILED tests/integration/test_cold_tier_local.py::test_cold_store_gcs_branch_forwards_endpoint_url
```

## Root cause (DIAGNOSED 2026-09-28, supersedes initial analysis)

The akosha wiring in `akosha/storage/cold_store.py:330` is **already correct**:

```python
elif self._storage_backend == "gcs":
    ...
    settings = GCSStorageSettings(
        bucket=self.bucket,
        project=self._project,
        credentials_file=self._credentials_file,
        endpoint_url=self._endpoint_url,   # <-- already passed
    )
```

The reason the test still fails is that the **installed** oneiric in
akosha's venv (`/Users/les/Projects/akosha/.venv/lib/python3.14/site-packages/oneiric`,
version 0.23.0) does NOT have an `endpoint_url` field on
`GCSStorageSettings`. Pydantic silently drops the parameter at
construction time, so `settings.model_dump()` returns a dict that lacks
`endpoint_url`.

The **local** oneiric source at `/Users/les/Projects/oneiric` (version 0.26.0)
DOES have `endpoint_url` — it was added in commit
`d809cad feat(oneiric): GCSStorageAdapter accepts endpoint_url`
(which is between 0.23.0 and 0.26.0).

Verified directly:

```python
# In akosha venv (oneiric 0.23.0)
>>> from oneiric.adapters.storage.gcs import GCSStorageSettings
>>> s = GCSStorageSettings(bucket='x', endpoint_url='http://127.0.0.1:4443')
>>> s.model_dump()
{'bucket': 'x', 'project': None, 'credentials_file': None,
 'default_content_type': 'application/octet-stream'}
# ^ no 'endpoint_url' key — silently dropped
```

The fix is therefore NOT in akosha's cold_store.py — it's a **oneiric
version bump** for the akosha venv. The wiring in cold_store.py was
written against the post-`d809cad` oneiric API; akosha's pinned
dependency just doesn't include that release yet.

## Fix

1. Bump `oneiric` in `akosha/pyproject.toml` to a version that
   includes commit `d809cad` (≥ 0.24.0 by my best guess — verify
   by checking the version that contains `d809cad`).

   **Per `feedback-mcp-common-version-bump-is-user.md`: the user
   does version bumps. This ticket is filed here; the version bump
   is a user-action, not a Claude-action.**

2. Re-sync akosha's venv: `uv pip install -e .` (or `uv sync`).

3. Re-run the test:
   ```
   .venv/bin/python -m pytest tests/integration/test_cold_tier_local.py::test_cold_store_gcs_branch_forwards_endpoint_url -x
   ```
   Expected: PASSES.

## Why this ticket exists (unrelated to current session)

This failure was first surfaced during the 2026-09-28 tool-surface-quality
session while running the akosha test suite. It is **independent of the
ingestion / cache / lifecycle work** that session shipped. Filing as a
separate ticket so the version bump can be prioritized and PR'd on its
own schedule without blocking any other workstream.

## Impact

- **No production data path broken** — the GCS branch is not the
  default cold storage backend in production; local-dev is the use case.
- **No follow-on plan blocked** — the cold-tier fake-gcs integration
  (`docs/plans/2026-09-27-cold-tier-fake-gcs-impl.md`)
  cannot land its end-to-end test
  (`test_cold_tier_fake_gcs_round_trip`) until this version bump ships.
- **Test is correctly written** — the production code is correct
  (verified by inspection), the dependency is stale. Per
  `feedback-no-backwards-compat-pre-1.0.md`, the fix is forward (bump
  the dep), not backward (revert the test).

## Acceptance criteria

- [ ] `oneiric >= 0.24.0` (or version containing `d809cad`) declared
      in `akosha/pyproject.toml`.
- [ ] `pytest tests/integration/test_cold_tier_local.py::test_cold_store_gcs_branch_forwards_endpoint_url` passes.
- [ ] `pytest tests/integration/test_cold_tier_local.py` full file
      passes (sibling S3 + StandardMode tests still green).
- [ ] No regressions in the rest of the akosha suite (`pytest tests/`).

## Notes

- The S3 branch has the SAME shape of test
  (`test_cold_store_s3_branch_forwards_credentials`) and it passes —
  the S3 `endpoint_url` field is in oneiric 0.23.0, the GCS one isn't.
- This ticket was created in a session whose primary focus was the
  tool-surface-quality plan (mahavishnu) — it's filed here on akosha
  because the failing code lives in akosha.
- Initial diagnosis (2026-09-28 first pass) said "one-line fix in
  cold_store.py". That diagnosis was wrong — the wiring was already
  present. The corrected diagnosis (above) is a oneiric version bump,
  not a code change.
