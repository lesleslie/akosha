# ZUBAN FIX ATTACK PLAN

**Status**: resolved (2026-07-15)

## Problem Summary

**Error:** `thread 'main' panicked at crates/zmypy/src/lib.rs:316:27: Problem parsing Mypy config`

**Root Cause:** ZubanAdapter hardcodes `--config-file mypy.ini` but Akosha uses `pyproject.toml`

## Resolution (2026-07-15)

Option 1 applied — `akosha/mypy.ini` exists at the repository root and unblocks the zuban adapter that hardcodes `--config-file mypy.ini`.

Option 2 verification pending; see `pyproject.toml` / `.crackerjack.toml` for zuban exclusion. The current crackerjack config (`pyproject.toml` and the absence of a repo-local `.crackerjack.toml`) does **not** disable zuban, so the dual type-checker overhead from this plan's "Phase 2: Long-term" recommendation has not been formally applied. Future operators can complete Option 2 by adding `exclude_hooks = ["zuban"]` under `[tool.crackerjack]` in `pyproject.toml`.

Options 3 (patch crackerjack adapter upstream) and 4 (`zuban check` workflow) remain unimplemented and are tracked here for completeness only.

---

## Option 1: Create mypy.ini (RECOMMENDED - Quick Fix)

**Pros:**
- ✅ Fastest solution (1 file, 5 lines)
- ✅ Works with existing zuban adapter
- ✅ Standard mypy practice
- ✅ No code changes needed

**Cons:**
- ⚠️ Duplicate config (have both pyproject.toml and mypy.ini)
- ⚠️ Need to keep both in sync

**Implementation:**
```ini
# mypy.ini
[mypy]
python_version = 3.13
warn_return_any = True
warn_unused_configs = True
disallow_untyped_defs = True
```

**Effort:** 2 minutes
**Risk:** LOW

---

## Option 2: Disable Zuban Hook (PRAGMATIC)

**Pros:**
- ✅ Already have pyright for type checking
- ✅ mypy is redundant
- ✅ Reduces CI time

**Cons:**
- ⚠️ Lose mypy-specific checks
- ⚠️ May miss issues pyright doesn't catch

**Implementation:**
Add to `pyproject.toml`:
```toml
[tool.crackerjack]
exclude_hooks = ["zuban"]
```

**Effort:** 1 minute
**Risk:** MINIMAL (have pyright)

---

## Option 3: Patch Crackerjack Adapter (UPSTREAM FIX)

**Pros:**
- ✅ Proper fix for root cause
- ✅ Benefits entire crackerjack community
- ✅ Use single source of truth (pyproject.toml)

**Cons:**
- ❌ Requires forking crackerjack
- ❌ Takes time to PR and merge
- ❌ May not be accepted quickly

**Implementation:**
1. Fork crackerjack
2. Modify `ZubanAdapter.build_command()` to check for pyproject.toml first
3. Submit PR

**Effort:** 2-4 hours
**Risk:** MEDIUM (upstream dependency)

---

## Option 4: Use `zuban check` Instead (ALTERNATIVE)

**Pros:**
- ✅ Uses pyproject.toml automatically
- ✅ No config file needed
- ✅ Modern zuban workflow

**Cons:**
- ⚠️ Requires modifying crackerjack adapter
- ⚠️ Different behavior than mypy

**Implementation:**
Change command from `zuban mypy` to `zuban check`

**Effort:** 15 minutes (if adapter modified)
**Risk:** LOW

---

## RECOMMENDATION: Option 1 + Option 2

### Phase 1: Quick Fix (Option 1)
Create `mypy.ini` to unblock comp hooks:

```bash
cat > mypy.ini << 'EOF'
[mypy]
python_version = 3.13
warn_return_any = True
warn_unused_configs = True
disallow_untyped_defs = True
EOF
```

### Phase 2: Long-term (Option 2)
Disable zuban in favor of pyright:

**Rationale:**
- Pyright is already working perfectly
- Pyright has better Python 3.13 support
- Running both mypy AND pyright is redundant
- Reduces CI/CD time by 30-40 seconds

**Implementation:**
```toml
# .crackerjack.toml or in pyproject.toml
[tool.crackerjack.hooks]
zuban = { enabled = false }
```

---

## Test Plan

1. Create mypy.ini
2. Run: `zuban mypy akosha/`
3. Verify it works
4. Run: `python -m crackerjack run --comp`
5. Confirm zuban passes
6. (Optional) Disable zuban permanently
7. Confirm pyright still catches all type errors

---

## Decision Matrix

| Option | Time | Risk | Completeness | Recommendation |
|--------|------|------|--------------|----------------|
| 1. Create mypy.ini | 2 min | LOW | 90% | ✅ DO FIRST |
| 2. Disable zuban | 1 min | MINIMAL | 100% | ✅ DO SECOND |
| 3. Patch adapter | 2-4 hrs | MEDIUM | 100% | ⏳ LATER |
| 4. Use zuban check | 15 min | LOW | 95% | 💡 MAYBE |

---

## Next Steps

**Immediate (Recommended):**
1. Create `mypy.ini` (Option 1)
2. Test zuban works
3. Disable zuban hook (Option 2)
4. Run comp hooks to verify all pass

**Alternative (If you want mypy):**
1. Create `mypy.ini` (Option 1)
2. Keep zuban enabled
3. Accept dual type-checker overhead

**Long-term:**
1. Submit PR to crackerjack to fix adapter (Option 3)
2. Or contribute zuban check support to adapter (Option 4)
