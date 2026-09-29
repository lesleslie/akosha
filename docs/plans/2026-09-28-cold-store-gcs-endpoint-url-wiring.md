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

`ColdStore.initialize()` with `backend='gcs'` does not forward
`endpoint_url` to `GCSStorageSettings`. The test
`tests/integration/test_cold_tier_local.py::test_cold_store_gcs_branch_forwards_endpoint_url`
fails with `KeyError: 'endpoint_url'`.

The test asserts the contract that any caller passing `endpoint_url`
(e.g. for fake-gcs-server during local dev, or for GCS-compatible
endpoints) gets it propagated to the storage adapter. The production
code drops it on the floor.

## Failing test (verbatim, as of 2026-09-28)

```
$ .venv/bin/python -m pytest tests/integration/test_cold_tier_local.py::test_cold_store_gcs_branch_forwards_endpoint_url -x --no-header
tests/integration/test_cold_tier_local.py:56: in test_cold_store_gcs_branch_forwards_endpoint_url
    assert settings["endpoint_url"] == "http://127.0.0.1:4443"
E   KeyError: 'endpoint_url'
FAILED tests/integration/test_cold_tier_local.py::test_cold_store_gcs_branch_forwards_endpoint_url
```

The test was added in commit `f187f557 test(session-buddy): GCSStorageOneiric round-trip via fake-gcs-server` (2026-09, on the parallel session-buddy repo) and the akosha equivalent is the
same class of wiring gap surfaced in `feedback-cli-flag-consumer-wiring.md`.

## Root cause

`akosha/storage/cold_store.py` GCS branch in `initialize()` constructs
`GCSStorageSettings` without `endpoint_url`. The constructor accepts
`endpoint_url` (per the test fixture at line 47) and stores it on
`self`, but never passes it to the settings object.

By contrast, the `s3` branch DOES forward `endpoint_url` (verified by
the sibling `test_cold_store_s3_branch_forwards_credentials` test, which
passes). The asymmetry is a wiring-bug smell, not a design intent.

## Fix

In `akosha/storage/cold_store.py::initialize()`, GCS branch:

```python
if self.storage_backend == "gcs":
    settings = GCSStorageSettings(
        bucket=self.bucket,
        project=self.project,
        endpoint_url=self.endpoint_url,   # ← add this line
        credentials_file=self.credentials_file,
    )
```

Then re-run the test. Expected: passes.

## Why this ticket exists (unrelated to current session)

This failure was first surfaced during the 2026-09-28 tool-surface-quality
session while running the akosha test suite. It is **independent of the
ingestion / cache / lifecycle work** that session shipped. Filing as a
separate ticket so the fix can be prioritized and PR'd on its own
schedule without blocking any other workstream.

## Impact

- **No production data path broken** — the GCS branch is not the default
  cold storage backend in production; local-dev is the use case.
- **No follow-on plan blocked** — the cold-tier fake-gcs integration
  (`docs/superpowers/plans/2026-09-27-cold-tier-fake-gcs-impl.md`) cannot
  land its end-to-end test (`test_cold_tier_fake_gcs_round_trip`) until
  this fix ships, because the wiring foundation is broken.
- **Test is correctly written** — the production code is wrong, not the
  test. Per `feedback-no-backwards-compat-pre-1.0.md`, the fix is
  forward (add the wiring), not backward (revert the test).

## Acceptance criteria

- [ ] `pytest tests/integration/test_cold_tier_local.py::test_cold_store_gcs_branch_forwards_endpoint_url` passes.
- [ ] `pytest tests/integration/test_cold_tier_local.py` full file passes (the sibling S3 + StandardMode tests should still be green).
- [ ] No regressions in the rest of the akosha suite (`pytest tests/`).
- [ ] The change is atomic: one hunk, one commit, single-line fix is fine.

## Notes

- This ticket was created in a session whose primary focus was the
  tool-surface-quality plan (mahavishnu) — it's filed here on akosha
  because the failing code lives in akosha.
- The S3 branch has the SAME shape of test
  (`test_cold_store_s3_branch_forwards_credentials`) and it passes;
  the fix is symmetric to the S3 branch's existing pattern.
