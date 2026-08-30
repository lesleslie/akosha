"""Lifecycle tracking for ``docs/feature-tracking/2026-08-29-akosha-websocket-search.md``.

This file pins the frontmatter of Akosha's adoption entry for the
websocket-invocations search work (Sub-plans A/B/C landed in
``cb4111c``, ``1830353``, ``4499467``). When the entry's ``built``,
``wired``, and ``adopted`` fields read ``2026-08-29`` we know the
lifecycle flipped to ``adopted`` and the contract is durable.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest
import yaml

FEATURE_TRACKING_ENTRY = (
    Path(__file__).resolve().parent.parent.parent
    / "docs"
    / "feature-tracking"
    / "2026-08-29-akosha-websocket-search.md"
)


def _read_frontmatter(path: Path) -> dict[str, object]:
    """Parse the YAML frontmatter between leading and trailing ``---`` markers."""
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(?P<fm>.*?)\n---\n", text, flags=re.DOTALL)
    assert match is not None, f"No YAML frontmatter delimited by '---' in {path}"
    loaded = yaml.safe_load(match.group("fm"))
    assert isinstance(loaded, dict), f"Frontmatter must parse to a dict in {path}"
    return loaded


def test_feature_tracking_entry_exists_with_adopted_field() -> None:
    """Akosha's websocket-search adoption entry must exist and reach ``adopted``.

    Reads ``docs/feature-tracking/2026-08-29-akosha-websocket-search.md``
    frontmatter and asserts the three lifecycle dates
    (``built``, ``wired``, ``adopted``) all read ``2026-08-29``.
    """
    assert FEATURE_TRACKING_ENTRY.exists(), (
        f"Missing feature-tracking entry at {FEATURE_TRACKING_ENTRY} — "
        "Sub-plan D (lifecycle flip) has not landed."
    )

    frontmatter = _read_frontmatter(FEATURE_TRACKING_ENTRY)

    # PyYAML parses bare ISO-8601 dates as ``datetime.date`` objects; accept
    # either string or date so the entry can follow the YAML convention.
    expected = date(2026, 8, 29)

    for field in ("built", "wired", "adopted"):
        actual = frontmatter.get(field)
        assert actual == expected or actual == expected.isoformat(), (
            f"Frontmatter `{field}` must be '2026-08-29' (got "
            f"{actual!r}) in {FEATURE_TRACKING_ENTRY.name}"
        )
