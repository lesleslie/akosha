"""Unit tests for ``scripts/regenerate_plan_index.py``'s store contract.

The store list is not cosmetic. ``_render_registry`` emits one section
per entry in ``DEFAULT_STORES`` and appends that store's entries to
``all_entries``, which feeds the Lifecycle x Role distribution table. A
repeated store therefore renders its section *twice* and double-counts
every file it holds in the distribution -- an index that disagrees with
itself about how many plans it has.

The duplicate arrived through a directory rename rather than an edit.
Commit 3aaaaa0 ("relocate plans to docs/plans/, specs to docs/specs/")
mechanically rewrote two distinct stores onto one path::

    "docs/superpowers/specs/"  ->  "docs/specs/"
    "docs/superpowers/plans/"  ->  "docs/plans/"   # collides with the
                                                      # pre-existing
                                                      # "docs/plans/" entry

The collision stayed invisible because a repeated key in a dict literal
keeps the LAST value: ``STORE_LABELS`` silently dropped "Plans &
Specifications" for ``docs/plans/`` in favour of "Superpowers Plans",
with no error anywhere. Only the ordering of the two entries revealed
that a label had been shadowed.

These tests pin the invariant the rename broke, so the next relocation
fails loudly instead of rendering a doubled index.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "regenerate_plan_index.py"

#: The canonical fleet-wide store set. Oneiric, crackerjack and
#: session-buddy all carry this exact tuple in their own copy of the
#: script; akosha's copy diverged at the 2026-10-03 relocation.
CANONICAL_STORES: tuple[str, ...] = (
    "docs/adr/",
    "docs/plans/",
    "docs/specs/",
    ".claude/decisions/",
    "docs/followups/",
)


@pytest.fixture(scope="module")
def regenerator() -> ModuleType:
    """Import the script by path -- ``scripts/`` is not a package."""
    spec = importlib.util.spec_from_file_location(
        "akosha_regenerate_plan_index", SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the script defines dataclasses, and
    # ``dataclasses`` resolves a field's ClassVar through
    # ``sys.modules[cls.__module__].__dict__``. An unregistered module
    # makes that lookup None.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_default_stores_lists_each_store_once(regenerator: ModuleType) -> None:
    """Every store appears exactly once.

    A repeat renders the section twice and double-counts its files in
    the distribution table.
    """
    stores = list(regenerator.DEFAULT_STORES)

    duplicates = sorted({s for s in stores if stores.count(s) > 1})
    assert not duplicates, (
        f"DEFAULT_STORES lists {duplicates} more than once: {stores}"
    )


def test_default_stores_matches_the_canonical_fleet_set(
    regenerator: ModuleType,
) -> None:
    """The store set is the canonical one, in the canonical order.

    Order matters: it is the order the registry sections appear in.
    """
    assert regenerator.DEFAULT_STORES == CANONICAL_STORES


def test_store_labels_are_keyed_one_to_one_by_store(regenerator: ModuleType) -> None:
    """``STORE_LABELS`` covers exactly the stores.

    Guards the *coverage* half of the contract: every store has a label
    and no label names a store that does not exist. It cannot detect a
    shadowed key -- see the AST test below for that.
    """
    stores = set(regenerator.DEFAULT_STORES)
    labels = set(regenerator.STORE_LABELS)

    assert labels == stores, (
        f"STORE_LABELS keys {sorted(labels)} do not match DEFAULT_STORES "
        f"{sorted(stores)}"
    )


def test_store_labels_has_no_shadowed_key() -> None:
    """No label key is written twice in the ``STORE_LABELS`` literal.

    A repeated key in a dict literal keeps the LAST value, so the first
    label is dropped without any error -- and the resulting dict's key
    set is unchanged, so comparing key *sets* cannot see it. Only the
    literal's source shows the repeat, hence the AST walk.
    """
    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))

    counts: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign):
            continue
        if getattr(node.target, "id", None) != "STORE_LABELS":
            continue
        assert isinstance(node.value, ast.Dict), "STORE_LABELS is not a dict literal"
        for key in node.value.keys:
            assert isinstance(key, ast.Constant) and isinstance(key.value, str)
            counts[key.value] = counts.get(key.value, 0) + 1

    assert counts, "STORE_LABELS literal not found"

    shadowed = sorted(k for k, n in counts.items() if n > 1)
    assert not shadowed, (
        f"STORE_LABELS writes {shadowed} more than once; the earlier "
        f"label is silently discarded"
    )


def test_every_store_has_a_non_empty_label(regenerator: ModuleType) -> None:
    """No store renders under an empty or whitespace-only heading."""
    for store in regenerator.DEFAULT_STORES:
        label = regenerator.STORE_LABELS.get(store, "")
        assert label.strip(), f"store {store!r} has no usable label"
