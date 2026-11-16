"""Unit tests for the plan-index renderer's store contract.

The store list is not cosmetic. The renderer emits one section per entry
in ``DEFAULT_STORES`` and appends that store's entries to the
distribution table's input. A repeated store therefore renders its
section *twice* and double-counts every file it holds — an index that
disagrees with itself about how many plans it has.

The duplicate arrived through a directory rename rather than an edit.
Commit 3aaaaa0 ("relocate plans to docs/plans/, specs to docs/specs/")
mechanically rewrote two distinct stores onto one path::

    "docs/superpowers/specs/"  ->  "docs/specs/"
    "docs/superpowers/plans/"  ->  "docs/plans/"   # collides with the
                                                      # pre-existing
                                                      # "docs/plans/" entry

The collision stayed invisible because a repeated key in a dict literal
keeps the LAST value: the label map silently dropped "Plans &
Specifications" for ``docs/plans/`` in favour of "Superpowers Plans",
with no error anywhere. Only the ordering of the two entries revealed
that a label had been shadowed.

These tests pin the invariant the rename broke, so the next relocation
fails loudly instead of rendering a doubled index.

The renderer used to be a *copy* in each repo's ``scripts/`` directory,
which meant the contract was asserted against a file that the publish
phase never ran. It now lives in exactly one place —
``crackerjack.tools.plan_index_sync`` — and the repo-local script is a
thin wrapper over it, so these tests now assert against the
implementation that actually writes the index.
"""

from __future__ import annotations

from pathlib import Path

from crackerjack.tools import plan_index_sync as renderer

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The canonical fleet-wide store set. Oneiric, crackerjack, akosha,
#: mcp-common and session-buddy all index exactly these stores.
CANONICAL_STORES: tuple[str, ...] = (
    "docs/adr/",
    "docs/plans/",
    "docs/specs/",
    ".claude/decisions/",
    "docs/followups/",
)


def test_default_stores_lists_each_store_once() -> None:
    """Every store appears exactly once.

    A repeat renders the section twice and double-counts its files in
    the distribution table.
    """
    stores = list(renderer.DEFAULT_STORES)

    duplicates = sorted({s for s in stores if stores.count(s) > 1})
    assert not duplicates, (
        f"DEFAULT_STORES lists {duplicates} more than once: {stores}"
    )


def test_default_stores_matches_the_canonical_fleet_set() -> None:
    """The store set is the canonical one, in the canonical order.

    Order matters: it is the order the registry sections appear in.
    """
    assert renderer.DEFAULT_STORES == CANONICAL_STORES


def test_every_store_renders_exactly_one_non_empty_section() -> None:
    """Each store gets its own heading, and no heading is empty.

    The renderer derives the heading from the store path, so two stores
    deriving the same heading would render two identically-titled
    sections — the current form of the shadowed-label bug.
    """
    rendered = renderer.render_plan_index(REPO_ROOT)

    headings = [
        line[len("### ") :]
        for line in rendered.splitlines()
        if line.startswith("### ")
    ]
    for store in renderer.DEFAULT_STORES:
        label = (
            store.rstrip("/").replace("docs/", "Docs: ").replace(".claude/", ".claude/")
        )
        assert label.strip(), f"store {store!r} has no usable heading"
        assert headings.count(label) == 1, (
            f"store {store!r} renders {headings.count(label)} sections "
            f"under {label!r}; headings: {headings}"
        )
