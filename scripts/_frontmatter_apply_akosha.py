"""Apply YAML frontmatter to akosha docs (per-repo P7.B sweep).

User-authorized via the P7 cross-repo playbook at
/Users/les/Projects/session-buddy/docs/plans/2026-07-16-p7-cross-repo-playbook.md.

Layout detected (Step 1):
  - Loose docs/*.md (~21 files)
  - docs/runbooks/*.md (7 files)
  - docs/reference/service-dependencies.md (1 file)
  - docs/guides/operational-modes.md (1 file)
  - docs/superpowers/plans/2026-07-12-eventbridge-publisher.md (1 file)
  - docs/schemas/{document-frontmatter,topic-vocabulary}-v1.md (2 files; canonical schema)

Schema files get a fixed assignment (status: active, role: canonical, topic: lifecycle).

Per-file status/role/topic assignments derived from each file's body and filename
following the playbook's Step 3 matrix and the per-akosha topic vocabulary
extension in docs/schemas/topic-vocabulary-v1.md.
"""

from __future__ import annotations

from pathlib import Path

PLAN_FM_TEMPLATE = (
    "---\n"
    "status: {status}\n"
    "role: {role}\n"
    "date: 2026-07-16\n"
    "last_reviewed: 2026-07-16\n"
    "superseded_by: null\n"
    "blocks_on: []\n"
    "topic: {topic}\n"
    "---\n"
    "\n"
)


# Assignments keyed on repo-relative POSIX path.
# (status, role, topic)
ASSIGNMENTS: dict[str, tuple[str, str, str]] = {
    # Schema / vocabulary (canonical, never expires)
    "docs/schemas/document-frontmatter-v1.md": ("active", "canonical", "lifecycle"),
    "docs/schemas/topic-vocabulary-v1.md": ("active", "canonical", "lifecycle"),
    # Loose docs/*.md — user-facing guides & canonicals
    "docs/README.md": ("active", "canonical", "lifecycle"),
    "docs/ROADMAP.md": ("active", "canonical", "lifecycle"),
    "docs/RULES.md": ("active", "canonical", "lifecycle"),
    "docs/PROJECT_STRUCTURE.md": ("active", "canonical", "architecture"),
    "docs/QUICK_REFERENCE.md": ("active", "canonical", "lifecycle"),
    "docs/USER_GUIDE.md": ("active", "canonical", "lifecycle"),
    "docs/DEPLOYMENT_GUIDE.md": ("active", "canonical", "observability"),
    "docs/IMPLEMENTATION_GUIDE.md": ("active", "canonical", "storage-consolidation"),
    "docs/AKOSHA_IMPLEMENTATION_GUIDE.md": ("active", "canonical", "storage-consolidation"),
    "docs/ARCHITECTURE.md": ("active", "canonical", "architecture"),
    "docs/AKOSHA_STORAGE_ARCHITECTURE.md": ("active", "canonical", "storage-consolidation"),
    "docs/ADMIN_SHELL.md": ("active", "canonical", "observability"),
    "docs/SECRET_MANAGEMENT.md": ("active", "canonical", "auth"),
    "docs/PROMETHEUS_METICS.md": ("active", "canonical", "observability"),
    "docs/WARM_TIER_QUICKSTART.md": ("active", "canonical", "storage-consolidation"),
    "docs/WARM_TIER_STORAGE_STRATEGY.md": ("active", "canonical", "storage-consolidation"),
    "docs/alerting.md": ("complete", "historical", "observability"),
    # Reviews, status snapshots, analyses — historical
    "docs/CURRENT_STATUS.md": ("complete", "historical", "lifecycle"),
    "docs/CURRENT_IDENTIFIER_ANALYSIS.md": ("complete", "historical", "memory-architecture"),
    "docs/COMPREHENSIVE_ARCHITECTURE_REVIEW_2025-01-31.md": ("complete", "historical", "architecture"),
    "docs/DISTRIBUTED_SYSTEMS_INTEGRATION_REVIEW.md": ("complete", "historical", "architecture"),
    "docs/ADR_001_ARCHITECTURE_DECISIONS.md": ("complete", "historical", "architecture"),
    # Phase plans — implementation, draft or planned
    "docs/PHASE_2_ADVANCED_FEATURES.md": ("draft", "implementation", "lifecycle"),
    "docs/PHASE_3_PRODUCTION_HARDENING.md": ("draft", "implementation", "observability"),
    "docs/PHASE_4_SCALE_PREPARATION.md": ("draft", "implementation", "lifecycle"),
    "docs/ZUBAN_ATTACK_PLAN.md": ("draft", "implementation", "storage-consolidation"),
    # Migration guide — historical once shipped (was historical already)
    "docs/PATH_MIGRATION_GUIDE.md": ("complete", "historical", "storage-consolidation"),
    # Runbooks — canonical operational references
    "docs/runbooks/CLOUD_STORAGE_OUTAGE.md": ("active", "canonical", "observability"),
    "docs/runbooks/DEPLOYMENT_ROLLBACK.md": ("active", "canonical", "observability"),
    "docs/runbooks/GRACEFUL_SHUTDOWN.md": ("active", "canonical", "observability"),
    "docs/runbooks/HOT_STORE_FAILURE.md": ("active", "canonical", "observability"),
    "docs/runbooks/INGESTION_BACKLOG.md": ("active", "canonical", "observability"),
    "docs/runbooks/MAHAVISHNU_DOWN.md": ("active", "canonical", "observability"),
    "docs/runbooks/MILVUS_FAILURE.md": ("active", "canonical", "observability"),
    # Sub-stores
    "docs/guides/operational-modes.md": ("active", "canonical", "lifecycle"),
    "docs/reference/service-dependencies.md": ("active", "canonical", "architecture"),
    "docs/superpowers/plans/2026-07-12-eventbridge-publisher.md": ("draft", "implementation", "observability"),
}


def add_legacy_comment(text: str) -> str:
    """Append a trailing HTML legacy comment on the first 'Status:' / '**Status**' line.

    Mirrors the C1.2 helper at /Users/les/Projects/mahavishnu/scripts/_orphan_sweep_C1_2.py.
    """
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Match `**Status**: ...`, `**Status** ...`, `**Status:** ...`, etc.
        if stripped.startswith("**Status") and "Status" in stripped:
            original = stripped.rstrip("\n")
            if "-- see YAML frontmatter" not in original:
                lines[i] = original + "  <!-- legacy status — see YAML frontmatter -->\n"
            break
    return "".join(lines)


def main() -> None:
    repo_root = Path("/Users/les/Projects/akosha")
    results: list[tuple[str, str, str, str]] = []
    missing_or_skipped: list[str] = []

    for rel_path, (status, role, topic) in ASSIGNMENTS.items():
        path = repo_root / rel_path
        if not path.is_file():
            missing_or_skipped.append(f"SKIP (missing): {rel_path}")
            continue
        original = path.read_text(encoding="utf-8")
        if original.lstrip().startswith("---\n"):
            missing_or_skipped.append(f"SKIP (already has frontmatter): {rel_path}")
            continue
        frontmatter = PLAN_FM_TEMPLATE.format(
            status=status, role=role, topic=topic
        )
        body_with_comment = add_legacy_comment(original)
        new_content = frontmatter + body_with_comment
        path.write_text(new_content, encoding="utf-8")
        results.append((rel_path, status, role, topic))

    print("\n--- Skips ---")
    for s in missing_or_skipped:
        print(s)
    print(f"\nEdited {len(results)} files:")
    for rel, st, rl, tp in results:
        print(f"  {rel}: status={st} role={rl} topic={tp}")


if __name__ == "__main__":
    main()
