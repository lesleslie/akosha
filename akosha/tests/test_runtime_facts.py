"""Tests for the Phase 3 ``RuntimeFactsBuilder`` and oneiric-runtime boot push.

Mirrors the Mahavishnu Phase 2 runtime-facts test layout. The wire
contract under test is:

1. ``RuntimeFactsBuilder.build_all()`` returns the same
   ``list[dict[str, Any]]`` shape that the legacy inline
   ``akosha_list_agents`` body returns (per entry:
   ``AgentMetadata.model_dump(mode="json")``).
2. Neither ``runtime_facts.py`` nor ``oneiric_runtime.py`` import
   ``oneiric`` directly — the boot push is a pure HTTP/JSON-RPC POST
   via :mod:`httpx`.

Per the test-dir-shadows-site-package convention (``akosha.mcp``
already exists as a real package), this test module does NOT add an
``__init__.py`` to the ``akosha/tests/`` directory.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

if TYPE_CHECKING:
    import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_FACTS_PATH = REPO_ROOT / "akosha" / "mcp" / "runtime_facts.py"
ONEIRIC_RUNTIME_PATH = REPO_ROOT / "akosha" / "mcp" / "oneiric_runtime.py"
AGENTS_TOOLS_PATH = REPO_ROOT / "akosha" / "mcp" / "tools" / "agents_tools.py"
SERVER_PATH = REPO_ROOT / "akosha" / "mcp" / "server.py"

# Files that MUST stay free of direct oneiric imports. Boot push is
# pure httpx; cross-component imports would break Phase 3 isolation.
NO_ONEIRIC_FILES = (RUNTIME_FACTS_PATH, ONEIRIC_RUNTIME_PATH, SERVER_PATH)


def test_akosha_runtime_facts_preserves_wire_shape() -> None:
    """RuntimeFactsBuilder.build_all matches the legacy ``akosha_list_agents`` wire shape.

    The Phase 3 wiring delegates the inline body of
    ``akosha_list_agents`` to :class:`RuntimeFactsBuilder`. The new
    builder MUST return the same ``list[dict[str, Any]]`` shape so
    Phase 3 installers see no behavioral change.

    Verifies, against the live static catalog:

    * Return type is ``list``.
    * Length matches the catalog (``_STATIC_AGENTS``).
    * Each item is a ``dict`` whose keys cover the AgentMetadata
      model contract — specifically ``name``, ``description``,
      ``version``, ``tools``, ``system_prompt``, and ``content_hash``.
    """
    from akosha.mcp.runtime_facts import RuntimeFactsBuilder
    from akosha.mcp.tools.agents_tools import _STATIC_AGENTS

    mock_app = MagicMock()
    result = RuntimeFactsBuilder(mock_app).build_all()

    # Shape contract
    assert isinstance(result, list)
    assert len(result) == len(_STATIC_AGENTS), (
        f"expected {len(_STATIC_AGENTS)} agents, got {len(result)}"
    )

    # Per-entry contract: dict with AgentMetadata.model_dump fields.
    required_fields = {
        "name",
        "description",
        "version",
        "tools",
        "system_prompt",
        "content_hash",
    }
    for entry, item in zip(_STATIC_AGENTS, result, strict=True):
        assert isinstance(item, dict), f"entry {entry['name']!r} returned non-dict"
        missing = required_fields - set(item.keys())
        assert not missing, f"entry {entry['name']!r} missing AgentMetadata fields: {missing}"
        # The ``name`` field must match the catalog entry so the
        # installer can resolve the agent to its body file.
        assert item["name"] == entry["name"], (
            f"name mismatch for {entry['name']!r}: got {item['name']!r}"
        )


def test_akosha_no_oneiric_imports() -> None:
    """None of the Phase 3 files import oneiric directly.

    The boot push contract is: pure HTTP/JSON-RPC via :mod:`httpx`.
    A direct ``import oneiric`` or ``from oneiric import ...`` would
    re-couple Akosha to Oneiric at the Python level (the very thing
    the Oneiric MCP server is designed to decouple).
    """
    pattern = re.compile(r"^\s*(from\s+oneiric|import\s+oneiric)\b", re.MULTILINE)
    for path in NO_ONEIRIC_FILES:
        assert path.is_file(), f"Phase 3 file missing: {path}"
        content = path.read_text(encoding="utf-8")
        match = pattern.search(content)
        assert match is None, f"direct oneiric import found in {path.name}: {match.group(0)!r}"


def test_akosha_runtime_facts_survives_broken_catalog_entry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken ``_build_unsigned_metadata`` call MUST skip, not raise.

    The pre-Phase-3 inline body catches
    ``(FileNotFoundError, ValidationError, KeyError)`` per entry and
    continues. :class:`RuntimeFactsBuilder` MUST preserve that
    fail-soft semantics so a single broken body file does not poison
    the whole ``list_agents`` response.
    """
    from unittest.mock import patch

    from akosha.mcp.runtime_facts import RuntimeFactsBuilder

    mock_app = MagicMock()

    # Force every ``_build_unsigned_metadata`` call to raise
    # ``FileNotFoundError`` (mimics a missing body file). The builder
    # must swallow the exception, log, and return an empty list.
    with (
        patch(
            "akosha.mcp.tools.agents_tools._build_unsigned_metadata",
            side_effect=FileNotFoundError("simulated missing body"),
        ),
        caplog.at_level("ERROR", logger="akosha.mcp.runtime_facts"),
    ):
        result = RuntimeFactsBuilder(mock_app).build_all()

    assert not result, "broken entries must be skipped, not raised"
    assert any("list_agents: failed to build metadata" in rec.message for rec in caplog.records), (
        "expected an error log per broken entry"
    )
