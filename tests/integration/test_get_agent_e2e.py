"""End-to-end test: akosha_get_agent returns ``body = system_prompt`` (B-6 critical).

Per plan §11 B-6, this is the **critical fix** that distinguishes a
working agent installer from one that ships non-functional agents.
The agent body IS the system prompt — Claude Code reads ``system_prompt``
directly, NOT the surrounding metadata. Without ``body == system_prompt``
on the response, the installer would write a marker file with no
real instructions and the picker would launch a broken agent.

The test:

1. Seeds a stand-in MCP server shim.
2. Registers ``akosha_get_agent`` against it via ``register_agents_tools``.
3. Stubs the lifespan-owned :class:`SkillsSigner` (so signing can run
   without a persisted keypair).
4. Invokes the inner function and asserts:
   - ``success`` is ``True``
   - ``body`` equals ``metadata.system_prompt`` byte-for-byte
   - ``content_hash`` equals ``sha256(body)``
   - The signature is non-empty base64 over the canonical payload
   - Errors are returned (not raised) for invalid input

This is the Phase 3 exit criterion alongside
``test_list_agents_e2e.py``.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from typing import Any

import pytest

from akosha.mcp.tools.agents_tools import register_agents_tools


# ---------------------------------------------------------------------------
# Test fixtures: stand-in MCP app + signer state.
# ---------------------------------------------------------------------------


@dataclass
class _SignedPayload:
    """Mirror of :class:`akosha.skills_signer.SignedPayload`."""

    signature_b64: str
    key_id: str
    canonical_payload: bytes = b""


@dataclass
class _StubSigner:
    """Stand-in for :class:`akosha.skills_signer.SkillsSigner`."""

    key_id: str = "0" * 16

    def sign(self, canonical_payload: bytes) -> _SignedPayload:
        # Deterministic fake signature — base64 of sha256 of the payload
        # bytes, prefixed with a stable byte. The installer never reads
        # the signature in this test; we just need a non-empty value
        # so the schema's optional ``signature`` field gets populated.
        digest = hashlib.sha256(canonical_payload).hexdigest().encode("ascii")
        sig_bytes = b"AKOSHA_TEST_SIG:" + digest
        return _SignedPayload(
            signature_b64=base64.b64encode(sig_bytes).decode("ascii"),
            key_id=self.key_id,
            canonical_payload=canonical_payload,
        )


@dataclass
class _StubSignerFeedState:
    """Stand-in for :class:`akosha.mcp.signer_feed.SignerFeedState`."""

    signer: _StubSigner = field(default_factory=_StubSigner)
    cycles_total: int = 0
    errors_total: int = 0

    def record_cycle(self) -> None:
        self.cycles_total += 1

    def record_error(self) -> None:
        self.errors_total += 1


class _ToolRegistry:
    """Minimal MCP-server stand-in that captures ``app.tool()`` calls."""

    def __init__(self) -> None:
        self._tools: dict[str, Any] = {}

    def tool(self, name: str | None = None) -> Any:
        def decorator(fn: Any) -> Any:
            self._tools[name or fn.__name__] = fn
            return fn

        return decorator

    def get(self, name: str) -> Any:
        return self._tools[name]


@pytest.fixture
def app_with_signer() -> tuple[_ToolRegistry, _StubSignerFeedState]:
    """Return an app + signer feed state wired together.

    The signer state is injected via the module-level singleton so the
    tool handlers can read it via ``get_signer_feed_state()``.
    """
    from akosha.mcp import signer_feed

    state = _StubSignerFeedState()
    signer_feed._signer_state = state  # type: ignore[attr-defined]

    app = _ToolRegistry()
    register_agents_tools(app)

    yield app, state

    # Teardown: clear the singleton so other tests start clean.
    signer_feed._signer_state = None  # type: ignore[attr-defined]


@pytest.fixture
def app_without_signer() -> _ToolRegistry:
    """Return an app where the signer has NOT been initialized."""
    from akosha.mcp import signer_feed

    signer_feed._signer_state = None  # type: ignore[attr-defined]

    app = _ToolRegistry()
    register_agents_tools(app)
    return app


# ---------------------------------------------------------------------------
# B-6 critical: body MUST equal system_prompt.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_agent_akosha_specialist_body_equals_system_prompt(
    app_with_signer: tuple[_ToolRegistry, _StubSignerFeedState],
) -> None:
    """B-6 critical: ``response.body == metadata.system_prompt`` byte-for-byte."""
    app, _ = app_with_signer
    fn = app.get("akosha_get_agent")

    result = await fn(name="akosha-specialist")

    assert result.get("success") is True, f"unexpected error: {result.get('error')}"
    body = result["body"]
    metadata = result["metadata"]
    assert body == metadata["system_prompt"], (
        "B-6 invariant violated: body MUST equal system_prompt "
        "so the installer writes a working agent file"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent_name",
    ["akosha-specialist", "search-agent", "pattern-agent"],
)
async def test_get_agent_body_equals_system_prompt_for_every_agent(
    app_with_signer: tuple[_ToolRegistry, _StubSignerFeedState],
    agent_name: str,
) -> None:
    """B-6 invariant holds for ALL 3 starter agents."""
    app, _ = app_with_signer
    fn = app.get("akosha_get_agent")

    result = await fn(name=agent_name)

    assert result.get("success") is True, f"unexpected error: {result.get('error')}"
    body = result["body"]
    metadata = result["metadata"]
    assert body == metadata["system_prompt"]
    # Belt-and-braces: assert the bytes are identical, not just equal
    # as strings (encoding differences wouldn't show up otherwise).
    assert body.encode("utf-8") == metadata["system_prompt"].encode("utf-8")


@pytest.mark.asyncio
async def test_get_agent_content_hash_matches_body_bytes(
    app_with_signer: tuple[_ToolRegistry, _StubSignerFeedState],
) -> None:
    """Exit criterion: ``content_hash == sha256(body)``."""
    app, _ = app_with_signer
    fn = app.get("akosha_get_agent")

    result = await fn(name="akosha-specialist")

    assert result["success"] is True
    body = result["body"]
    declared_hash = result["metadata"]["content_hash"]
    expected = hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert declared_hash == expected, (
        f"content_hash mismatch: declared {declared_hash!r} but "
        f"sha256(body)={expected!r}"
    )


# ---------------------------------------------------------------------------
# Schema / signature smoke tests.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_agent_returns_signed_metadata(
    app_with_signer: tuple[_ToolRegistry, _StubSignerFeedState],
) -> None:
    """After signing, ``signature`` and ``server_pubkey_id`` are populated."""
    app, state = app_with_signer
    fn = app.get("akosha_get_agent")

    result = await fn(name="akosha-specialist")

    assert result["success"] is True
    metadata = result["metadata"]
    assert metadata["signature"] is not None
    assert isinstance(metadata["signature"], str)
    assert len(metadata["signature"]) > 0
    assert metadata["server_pubkey_id"] == state.signer.key_id
    # Sanity: base64 decodes without error
    base64.b64decode(metadata["signature"], validate=True)


@pytest.mark.asyncio
async def test_get_agent_bumps_feed_cycle_counter(
    app_with_signer: tuple[_ToolRegistry, _StubSignerFeedState],
) -> None:
    """B-7: every successful tool call bumps ``cycles_total``."""
    app, state = app_with_signer
    fn = app.get("akosha_get_agent")

    before = state.cycles_total
    await fn(name="akosha-specialist")
    after = state.cycles_total
    assert after == before + 1


@pytest.mark.asyncio
async def test_get_agent_returns_full_metadata_fields(
    app_with_signer: tuple[_ToolRegistry, _StubSignerFeedState],
) -> None:
    """Metadata carries all the Phase 3 schema fields."""
    app, _ = app_with_signer
    fn = app.get("akosha_get_agent")

    result = await fn(name="akosha-specialist")

    metadata = result["metadata"]
    expected_fields = {
        "schema_version",
        "id",
        "server_key",
        "name",
        "description",
        "version",
        "model",
        "tools",
        "system_prompt",
        "content_hash",
        "signature",
        "server_pubkey_id",
        "dependencies",
        "tool_refs",
        "scope",
        "status",
    }
    missing = expected_fields - set(metadata.keys())
    assert not missing, f"metadata missing fields: {missing}"
    assert metadata["server_key"] == "akosha"
    assert metadata["name"] == "akosha-specialist"
    assert metadata["id"] == "akosha:akosha-specialist:1.0.0"


@pytest.mark.asyncio
async def test_get_agent_body_is_substantial(
    app_with_signer: tuple[_ToolRegistry, _StubSignerFeedState],
) -> None:
    """B-6 quality bar: bodies carry real, non-trivial content (≥200 chars)."""
    app, _ = app_with_signer
    fn = app.get("akosha_get_agent")

    for agent_name in ("akosha-specialist", "search-agent", "pattern-agent"):
        result = await fn(name=agent_name)
        assert result["success"] is True
        body = result["body"]
        assert len(body) >= 200, (
            f"body for {agent_name!r} is too short ({len(body)} chars); "
            f"per plan §11 B-6 installer needs a real non-trivial body"
        )


# ---------------------------------------------------------------------------
# Negative cases — error envelopes, not exceptions.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_agent_rejects_forbidden_name_path_traversal(
    app_with_signer: tuple[_ToolRegistry, _StubSignerFeedState],
) -> None:
    """B-4: forbidden ``name`` shapes return an error envelope, not raise."""
    app, _ = app_with_signer
    fn = app.get("akosha_get_agent")

    for bad_name in ("../../etc/passwd", "FOO", "foo/bar", ".hidden", "a..b"):
        result = await fn(name=bad_name)
        assert result.get("success") is False, f"expected error for {bad_name!r}"
        assert "error" in result
        assert "allowlist" in result["error"].lower() or "B-4" in result["error"]


@pytest.mark.asyncio
async def test_get_agent_returns_error_when_signer_not_initialized(
    app_without_signer: _ToolRegistry,
) -> None:
    """Pre-lifespan / lite mode: error envelope, not raise."""
    fn = app_without_signer.get("akosha_get_agent")

    result = await fn(name="akosha-specialist")

    assert result.get("success") is False
    assert "signer" in result["error"].lower()


@pytest.mark.asyncio
async def test_get_agent_returns_error_for_unknown_name(
    app_with_signer: tuple[_ToolRegistry, _StubSignerFeedState],
) -> None:
    """Unknown name (but B-4-allowlisted) returns an error envelope."""
    app, _ = app_with_signer
    fn = app.get("akosha_get_agent")

    result = await fn(name="nonexistent-agent")

    assert result.get("success") is False
    assert "not found" in result["error"]
