---
status: active
role: canonical
kind: plan
date: 2026-09-27
last_reviewed: 2026-09-27
superseded_by: null
topic: version-source-of-truth
---

# Pyproject as Single Source of Truth for Version Strings

**Date:** 2026-09-27
**Status:** `draft, planning` (promote to `active` on user approval)
**Owner:** Bodai Core Eng
**Scope:** All 5 Bodai core repos — `oneiric`, `mahavishnu`, `akosha`, `session-buddy`, `crackerjack`. Plus `mcp-common` if it carries version stamps.
**Purpose:** Eliminate version-stamp drift across bodai packages by replacing hardcoded `__version__ = "X.Y.Z"` literals with `importlib.metadata.version("NAME")`, anchored to `pyproject.toml`. The CI guard already exists (`akosha/tests/unit/test_version_sync.py`); what changes is *what it guards against*.

## 1. Outcome

Bodai ships releases where the canonical version lives in exactly one place per repo: `pyproject.toml`. Every consumer of the version string reads it via the standard `importlib.metadata` API, with a documented fallback for editable-install edge cases. A user-driven bump in `pyproject.toml` propagates automatically to every consumer the next time the package is imported.

**Concrete success criterion**: for every bodai core repo, the regex `__version__ = ["']\d+\.\d+\.\d+["']` returns zero matches across the package tree. The whole codebase has zero hardcoded version literals. CI guard `test_version_sync.py` gets rewritten to assert `metadata.version("akosha") == akosha.__version__` rather than regex-scanning literals.

## 2. Goals

1. **Zero version literals in package code.** `git grep '__version__ = ["'"'"']\d' -- '*.py'` returns empty for every bodai core repo.
2. **One canonical pattern.** `from importlib import metadata` + `try: __version__ = metadata.version("NAME") except metadata.PackageNotFoundError: __version__ = "0.0.0"` (or `"0+unknown"`). No new helper symbol — stdlib only.
3. **Existing internal contracts preserved.** Akosha's `APP_VERSION == __version__` and `SERVICE_VERSION == __version__` invariants (already enforced by `test_mcp_server.py:22` and `test_mcp_health_tools.py:28`) continue to hold without rewriting.
4. **CI guard rewritten.** `tests/unit/test_version_sync.py` checks runtime parity (`metadata.version("akosha") == akosha.__version__`) instead of regex literal equality, because there are no literals left to regex.
5. **Cross-repo consistency.** Every bodai core adopts the pattern in the same release window — a fresh `test_version_sync.py` (or equivalent) ships in each repo with the new assertion.

## 3. Non-Goals

- **No new module/symbol in oneiric.** User explicitly rejected naming like `oneiric.bodai_version`. Stdlib is sufficient.
- **No `VERSION` or `VERSION.txt` file.** The pattern is `pyproject.toml` + `importlib.metadata`; introducing a sidecar file re-introduces the multi-source problem.
- **No runtime build-step.** Stamps become metadata at install time (already shipped by `hatch` / `setuptools` via the existing build backend). No `setup.py` rewrite, no `setup.cfg` regeneration.
- **No `mcp-common` rename of `mcp_common.__version__`.** That's its own package's stamps; follows the same pattern independently.
- **No automation of the CI guard** — the existing per-repo test stays in the suite; no shared bodai-wide hook.

## 4. Current Findings

**The pattern already exists in oneiric** (`oneiric/oneiric/__init__.py:9-12`):
```python
try:
    __version__ = metadata.version("oneiric")
except metadata.PackageNotFoundError:  # pragma: no cover - fallback for local dev
    __version__ = "0.0.0"
```
This is the canonical pattern. Oneiric is the proof — the work is *propagation*, not design.

**The bug surface is the rest of the ecosystem**:
- `akosha/__init__.py:15` — `__version__ = "0.17.5"` (drifted from pyproject `0.19.1`; fixed in commit `75ab9e1`)
- `akosha/mcp/__init__.py:5` — `__version__ = "0.17.5"` (same; fixed `75ab9e1`)
- `akosha/mcp/server.py:50` — `APP_VERSION: Final = "0.17.5"` (same; fixed `75ab9e1`)
- `akosha/mcp/tools/__init__.py:43` — `SERVICE_VERSION = "0.17.5"` (same; fixed `75ab9e1`)
- `README.md` — Version header (fixed `75ab9e1`)

**Aka**: every other repo in the bodai core set has parallel stamps. Manual `git grep` is the proof — until each migrates, it carries the same risk.

**Internal invariants already exist** (akosha):
- `akosha/tests/unit/test_mcp_server.py:22` — `assert APP_VERSION == __version__`
- `akosha/tests/unit/test_mcp_health_tools.py:28` — `assert SERVICE_VERSION == __version__`
- `akosha/tests/test_doc_drift.py:135` — `test_user_agent_matches_package_version` (catches hardcoded User-Agent strings)

These prove the *internal* lockstep contract is `STAMP == __version__`. The missing link is `__version__` itself — make it read from `pyproject.toml` and the rest follows.

**Scanned CI guard that we're replacing**: `akosha/tests/unit/test_version_sync.py` — currently regex-scans literal `__version__ = "..."` strings. Will become `assert metadata.version("akosha") == akosha.__version__`.

## 4.5 Requirements

```yaml
requirements:
  - id: REQ-001
    title: "pyproject.toml is the only place a canonical version literal lives in any bodai core repo"
  - id: REQ-002
    title: "Every package's top-level __version__ resolves via importlib.metadata.version('NAME') at import time"
  - id: REQ-003
    title: "Editable-install edge case handled: __version__ falls back to '0+unknown' (PEP 410 local-version label) when metadata is absent"
  - id: REQ-004
    title: "Internal cross-stamp contracts preserved (APP_VERSION == __version__ in akosha + equivalents per repo)"
  - id: REQ-005
    title: "Existing version-lock CI guard replaced with runtime-equality assertion, not regex literal scan"
```

## 5. Implementation Phases

### Phase 1: Akosha migration (proof-of-concept + first ship)

**Goal:** Demonstrate the pattern in akosha end-to-end before propagating. The proximate-cause repo ships first.

**Tasks:**
- Replace `akosha/__init__.py:15` literal with `importlib.metadata` call.
- Replace `akosha/mcp/server.py:50` literal: `APP_VERSION = akosha.__version__` (or `from akosha import __version__ as APP_VERSION`).
- Replace `akosha/mcp/tools/__init__.py:43` literal: `SERVICE_VERSION = akosha.__version__` (same approach).
- Replace `akosha/mcp/__init__.py:5` literal: `from akosha import __version__` re-export.
- Rewrite `akosha/tests/unit/test_version_sync.py` to assert runtime equality, not regex literal scan.
- Keep `akosha/tests/test_doc_drift.py` `test_user_agent_matches_package_version` — still relevant (it forbids `3.14` in User-Agent strings).

**Exit criteria:**
- `git grep '__version__ = ["'"'"']\d' -- akosha/akosha akosha/tests` returns empty.
- `pytest tests/unit/test_version_sync.py tests/test_doc_drift.py` all pass.
- Existing `test_mcp_server.py` + `test_mcp_health_tools.py` invariants still pass.

#### Phase 1 Integration Contract

- **Triggered from**: developer runs `uv pip install -e ".[dev]"` in akosha and imports `akosha`; the import statement executes the new `__init__.py` body.
- **Returns to / updates**: `akosha.__version__` value in the importable module namespace; binding for `APP_VERSION` and `SERVICE_VERSION` re-exports via `from akosha import __version__`.
- **Demonstrable by**: `python -c "import akosha; print(akosha.__version__ == importlib.metadata.version('akosha'))"` prints `True`.
- **Rollback signal**: `pytest tests/unit/test_version_sync.py::test_package_init_matches_pyproject` fails (the literal version reads mismatched). Edible install side effects: `akosha.__version__` is `"0+unknown"` not the bumped version — surface in tests.
- **Observability added**: a one-line log on import `logging.debug("akosha __version__ loaded from pyproject: %s", __version__)` (gated by `if __debug__`, suppressed when `-O`). Log key: `akosha.init.version`.

### Phase 2: Oneiric migration (which is also a no-op confirm)

**Goal:** Confirm oneiric's existing implementation stays the model and update its internal stamps if any remain.

**Tasks:**
- Inventory oneiric for any other hardcoded version literals (likely just the `__init__.py` already migrated).
- If `oneiric.adapters.metadata.PACKAGE_VERSION` exists (saw `metadata.py` in `adapters/`), audit it for consistency.
- No code change expected — verify only.

**Exit criteria:** `git grep '__version__ = ["'"'"']\d' -- oneiric/oneiric` returns zero matches besides any private `_DEFAULT_LEGACY_VERSION` (deprecation stub, exempt).

#### Phase 2 Integration Contract

- **Triggered from**: `uv pip install -e ".[dev]" && python -c "import oneiric; print(oneiric.__version__)"`.
- **Returns to / updates**: `oneiric.__version__` module attribute (already correct, no change).
- **Demonstrable by**: `python -c "import oneiric, importlib.metadata; assert oneiric.__version__ == importlib.metadata.version('oneiric')"` exits 0.
- **Rollback signal**: same as Phase 1.
- **Observability added**: none — phase is verification, not change.

### Phase 3: Mahavishnu + Session-Buddy + Crackerjack migration (3 repos in parallel)

**Goal:** Roll the pattern out to the remaining 3 bodai core repos. Same Phase 1 mechanics per repo; releases coordinated.

**Tasks per repo:**
- Replace top-level `__init__.py` `__version__ = "X.Y.Z"` with `importlib.metadata.version("NAME")` call.
- Add parity CI guard (`tests/unit/test_version_sync.py` or equivalent) asserting `metadata.version("REPO") == REPO.__version__`.
- For mahavishnu specifically, replace any service-specific stamps (e.g. `mcp/server.py:APP_VERSION` analog) with re-exports of `__version__`.
- For session-buddy and crackerjack, audit their MCP-server / hooks for parallel `APP_VERSION` / `SERVICE_VERSION` constants and rewrite.
- Update each repo's CLAUDE.md "When bumping a version" subsection to reference this plan (`docs/plans/2026-09-27-pyproject-single-source-of-truth.md`).

**Exit criteria:** All 3 repos pass their new version-parity CI guard; combined `git grep '__version__ = ["'"'"']\d' -- */` over all 5 repos returns zero hits in `__init__.py` or any package file (excepting exemption list).

#### Phase 3 Integration Contract

- **Triggered from**: each repo's CI runs `pytest tests/unit/test_version_sync.py` on every push.
- **Returns to / updates**: per-repo `__version__` runtime value + per-repo CI guard status.
- **Demonstrable by**: `cd <repo> && .venv/bin/pytest tests/unit/test_version_sync.py` passes.
- **Rollback signal**: any per-repo CI guard failure trips the `version-drift-check` Mahavishnu job (separate integration; future work).
- **Observability added**: each per-repo guard emits the standard assertion message naming the repo + the runtime read.

### Phase 4: Bodai-wide release-window coordination

**Goal:** Cut all 5 releases within a short window so a future user-driven bump in any repo's `pyproject.toml` doesn't have to fight cross-repo timing. Per Bodai policy, version bumps are user-driven — this plan only *mechanically enables* a future single-source-of-truth world, not the cut itself.

**Tasks:**
- Document the per-repo PR ordering for the next bump window in `docs/RELEASE_NOTES.md` (or each repo's RELEASING.md if it has one).
- Update each repo's `feedback-mcp-common-version-bump-is-user.md` analog memory if it exists, to add: "and the canonical version is now derived from `pyproject.toml` via `importlib.metadata`".

**Exit criteria:** All 5 repos document the pattern in their RELEASING/release notes; a future bump workflow that updates only `pyproject.toml` is sufficient — every consumer derives from there.

#### Phase 4 Integration Contract

- **Triggered from**: future maintainer runs `mahavishnu repo-tag --repo akosha --level minor --bump-only` (or equivalent user-driven action).
- **Returns to / updates**: `akosha/pyproject.toml` version field; downstream consumers observe the new version on next install.
- **Demonstrable by**: `python -c "import akosha, importlib.metadata; assert akosha.__version__ == importlib.metadata.version('akosha')" && python -c "import akosha; assert akosha.__version__.startswith('0.20')"` exits 0 after a single-file edit to pyproject.toml.
- **Rollback signal**: `pytest tests/unit/test_version_sync.py` failure (runtime equality breaks because pyproject bumped but venv cache stale).
- **Observability added**: a `release.bodai.version.drift` log emitted when a CI guard detects runtime mismatch between `metadata.version()` and the value at import time.

## 6. Required Code Changes

### akosha (5 files, prove-then-template)
- `akosha/__init__.py` — replace literal
- `akosha/mcp/__init__.py` — replace literal with `from akosha import __version__`
- `akosha/mcp/server.py` — `APP_VERSION = akosha.__version__`
- `akosha/mcp/tools/__init__.py` — `SERVICE_VERSION = akosha.__version__`
- `akosha/tests/unit/test_version_sync.py` — rewrite to runtime equality

### oneiric (verification only)
- `oneiric/oneiric/__init__.py` — already correct; no edit
- `oneiric/docs/plans/2026-09-27-pyproject-single-source-of-truth.md` cross-link

### mahavishnu (similar 4-5 file pattern)
- `mahavishnu/__init__.py` — replace literal
- `mahavishnu/mcp/server.py` — APP_VERSION derivation
- (auditing per-repo as work begins)

### session-buddy (similar 4-5 file pattern)
- analogous 4-5 sites, depending on what MCP/hooks layer carries

### crackerjack (similar 4-5 file pattern)
- analogous 4-5 sites

### docs / memory (cross-cutting)
- Add `feedback-bodai-pyproject-sso-version.md` to CC memory (new)
- Each repo's CLAUDE.md "When bumping a version" subsection gets one bullet referencing this plan
- `akosha/docs/PLAN_INDEX.md` — add new entry

## 7. Validation Matrix

| Check | Expected outcome | Evidence |
|---|---|---|
| `git grep '__version__ = ["'"'"']\d\.\d\.\d' -- '*/*/init.py'` over all 5 repos | zero hits | run in CI; saved log |
| `pytest tests/unit/test_version_sync.py` (per repo) | 6 passed (per existing akosha shape) | CI logs |
| `pytest tests/test_doc_drift.py::test_user_agent_matches_package_version` | pass | CI logs |
| `python -c "import <repo>; assert <repo>.__version__ == importlib.metadata.version('<repo>')"` (per repo) | exits 0 | manual + crackerjack hook |
| `pip install -e ".[dev]" && pytest tests/unit/test_version_sync.py` on a fresh venv | passes | CI sandbox on PR |
| `git grep -RE '__version__|APP_VERSION|SERVICE_VERSION' -- '*.py'` per repo | only `from akosha import __version__` re-exports remain | saved log |

## 8. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Editable install miscounts metadata version (`importlib.metadata.version` returns cached `0.0.0` during development) | medium | `PackageNotFoundError` fallback to `"0+unknown"` (PEP 410 local-version label); verify with `.venv/bin/pytest` after each `uv pip install -e .` |
| One MCP server in mahavishnu/session-buddy/crackerjack runs as daemon and pre-imports `__version__` before metadata is materialized | low | daemon read happens at module import (one-shot) — verify by smoke-testing daemons in fresh venvs |
| README version drift (literals in human-facing docs) | medium | exclude from automated regex check; rely on `human-CI` review per release |
| Internal contracts break silently (e.g. `APP_VERSION == __version__` was enforced by `==` on strings; runtime version might add `+local` suffix in editable installs) | medium | the contract test reads both from `__version__`, not from pyproject; so editable-install suffix is preserved consistently |
| Cross-repo PR ordering causes a temporary state where one repo's CI fails because a dependency bumped ahead | low | document the per-repo bump order in Phase 4; each PR is self-contained |

## 9. Decision Rule

This plan is "done enough" when:
- All 5 bodai core repos ship the pattern (Phase 1 + 2 + 3 complete)
- Each repo's CI green for runtime-equality version sync (no regex literal-scan version)
- One full release cycle has elapsed where a bump in any one repo's `pyproject.toml` *automatically* propagates to every consumer of that repo's version stamp (Phase 4 demonstrable)

Until those three conditions are met, the plan stays `active, partial`. After: promote to `complete, canonical` and downstream plans can `superseded_by: 2026-09-27-pyproject-single-source-of-truth.md`.

## References

- `oneiric/oneiric/__init__.py:9-12` — the canonical pattern this plan propagates
- `akosha/tests/unit/test_version_sync.py` — current CI guard (regex literal-scan; to be rewritten as runtime equality)
- `akosha/tests/test_doc_drift.py` — User-Agent version drift guard (kept as-is)
- `akosha/tests/unit/test_mcp_server.py:22` — internal `APP_VERSION == __version__` contract
- `akosha/tests/unit/test_mcp_health_tools.py:28` — internal `SERVICE_VERSION == __version__` contract
- Commit `75ab9e1` — the 5-file literal-sync fix (the symptom the plan prevents recurring)
- Commit `9dc487c` — the `audit_empty_tests.py` docstring-filter fix (unrelated; included for cross-reference on what shipped this turn)
- `.claude/CLAUDE.md` § Memory Routing — for why the canonical "this is the plan" lives in this file, not in CC memory
- `feedback-mcp-common-version-bump-is-user.md` (CC memory) — version bumps are user-driven; this plan only mechanizes the distribution, not the cut
- `.claude/decisions/wire-up-contract.md` — the policy this plan's Integration Contract blocks satisfy
- `docs/plans/TEMPLATE.md` — the template this plan conforms to
