"""B-4 path-traversal rejection tests for AgentMetadata (Phase 3).

Per plan §5 Phase 3 task #1 and §11 B-4, ``name`` and ``server_key``
fields are constrained to a strict allowlist that prevents path-traversal
attacks via server-supplied metadata. The unit test asserts the validator
rejects every forbidden shape AND accepts the happy-path shape.

Also covers B-6 ``content_hash`` body integrity:

- ``content_hash`` MUST equal ``sha256(system_prompt)`` — a forged hash
  is rejected at the model boundary.
- An empty ``system_prompt`` is allowed at the schema level (so tests
  can construct minimal payloads); the ``agents_tools`` layer rejects
  empty bodies before signing.

This test does NOT spin up the MCP server — it exercises the Pydantic
model directly so it can run without the full lifespan state.

Implementation note: ``str_strip_whitespace=True`` strips leading and
trailing whitespace from ALL string fields, including ``system_prompt``.
We therefore keep the test's body whitespace-stable (no trailing newline
in particular) so the pre-computed ``content_hash`` matches the
post-strip bytes that the schema's B-6 validator hashes.
"""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from akosha.mcp.agent_schema import AgentMetadata


def _valid_kwargs(**overrides: object) -> dict[str, object]:
    """Return valid kwargs with sensible defaults; pass ``overrides`` to mutate."""
    # No leading/trailing whitespace: ``str_strip_whitespace=True``
    # would otherwise mutate the body before the B-6 validator hashes
    # it, breaking the pre-computed ``content_hash`` below.
    system_prompt = (
        "# akosha-specialist\n\nYou are the Akosha cross-system "
        "intelligence specialist."
    )
    defaults: dict[str, object] = {
        "schema_version": 1,
        "id": "akosha:akosha-specialist:1.0.0",
        "server_key": "akosha",
        "name": "akosha-specialist",
        "title": "Akosha Specialist",
        "description": "Test description",
        "version": "1.0.0",
        "model": "opus",
        "tools": ["mcp__akosha__akosha_search_all_systems"],
        "system_prompt": system_prompt,
        "dependencies": [],
        "tool_refs": ["mcp__akosha__akosha_search_all_systems"],
        "category": "memory-aggregation",
        "owner": "akosha",
        "status": "active",
        "last_reviewed": "2026-09-10",
        "scope": "user-global",
        "content_hash": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
        "signature": None,
        "server_pubkey_id": None,
    }
    defaults.update(overrides)
    return defaults


class TestAllowlistHappyPath:
    """Valid identifiers parse without raising."""

    def test_lowercase_with_dash(self) -> None:
        m = AgentMetadata(**_valid_kwargs(name="akosha-specialist"))
        assert m.name == "akosha-specialist"

    def test_lowercase_with_dot(self) -> None:
        m = AgentMetadata(**_valid_kwargs(name="v1.2.3"))
        assert m.name == "v1.2.3"

    def test_lowercase_with_underscore(self) -> None:
        m = AgentMetadata(**_valid_kwargs(name="foo_bar"))
        assert m.name == "foo_bar"

    def test_single_char(self) -> None:
        """Length 1 is the minimum the regex allows."""
        m = AgentMetadata(**_valid_kwargs(name="a"))
        assert m.name == "a"

    def test_max_length_63(self) -> None:
        """Length 63 is the maximum the regex allows."""
        m = AgentMetadata(**_valid_kwargs(name="a" + "b" * 62))
        assert len(m.name) == 63

    def test_server_key_with_dash(self) -> None:
        m = AgentMetadata(**_valid_kwargs(server_key="session-buddy"))
        assert m.server_key == "session-buddy"


class TestNameAllowlistRejection:
    """B-4: forbidden name shapes must raise ValidationError."""

    def test_empty_string(self) -> None:
        with pytest.raises(ValidationError, match="allowlist"):
            AgentMetadata(**_valid_kwargs(name=""))

    def test_max_length_64(self) -> None:
        """64 chars exceeds the {0,62} limit."""
        with pytest.raises(ValidationError, match="allowlist"):
            AgentMetadata(**_valid_kwargs(name="a" * 64))

    @pytest.mark.parametrize(
        "bad_name",
        [
            "../../foo",  # path traversal: parent dirs
            "foo/bar",  # slash anywhere
            ".hidden",  # leading dot
            "FOO",  # uppercase
            "Foo",  # mixed case
            "foo bar",  # whitespace
            "foo!",  # punctuation
            "foo$bar",  # shell metacharacter
            "foo|bar",  # pipe
            "foo;bar",  # semicolon
            "foo&bar",  # ampersand
            "foo`bar`",  # backtick
            "foo\nbar",  # newline
            "foo\rbar",  # carriage return
            "foo\tbar",  # tab
        ],
    )
    def test_forbidden_characters(self, bad_name: str) -> None:
        """B-4 negative cases: every forbidden character class rejected."""
        with pytest.raises(ValidationError, match="allowlist"):
            AgentMetadata(**_valid_kwargs(name=bad_name))

    def test_double_dot_substring_rejected(self) -> None:
        """Defense-in-depth: ``..`` is forbidden even mid-string.

        The regex ``^[a-z0-9][a-z0-9._-]{0,62}$`` does not forbid ``..``
        in the middle of a string (e.g. ``a..b``). The brief requires
        forbidding ``..`` explicitly as defense-in-depth.
        """
        with pytest.raises(ValidationError, match=r"\.\."):
            AgentMetadata(**_valid_kwargs(name="a..b"))


class TestServerKeyAllowlistRejection:
    """B-4 applies to BOTH ``name`` and ``server_key`` (a forged ``server_key``
    value could trick a client into path-traversal too)."""

    def test_server_key_with_slash_rejected(self) -> None:
        with pytest.raises(ValidationError, match="allowlist"):
            AgentMetadata(**_valid_kwargs(server_key="akosha/../etc"))

    def test_server_key_uppercase_rejected(self) -> None:
        with pytest.raises(ValidationError, match="allowlist"):
            AgentMetadata(**_valid_kwargs(server_key="AkoSHA"))

    def test_server_key_double_dot_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"\.\."):
            AgentMetadata(**_valid_kwargs(server_key="ak..osha"))


class TestIdShapeValidation:
    """``id`` must be ``{server_key}:{name}:{version}`` with three parts."""

    def test_id_with_two_parts_rejected(self) -> None:
        with pytest.raises(ValidationError, match="server_key:name:version"):
            AgentMetadata(**_valid_kwargs(id="akosha:akosha-specialist"))

    def test_id_with_four_parts_rejected(self) -> None:
        with pytest.raises(ValidationError, match="server_key:name:version"):
            AgentMetadata(
                **_valid_kwargs(id="akosha:akosha-specialist:1.0.0:extra"),
            )

    def test_id_with_invalid_server_key_segment_rejected(self) -> None:
        with pytest.raises(ValidationError, match="invalid server_key segment"):
            AgentMetadata(
                **_valid_kwargs(id="AKOSHA:akosha-specialist:1.0.0"),
            )

    def test_id_with_invalid_name_segment_rejected(self) -> None:
        with pytest.raises(ValidationError, match="invalid name segment"):
            AgentMetadata(
                **_valid_kwargs(id="akosha:bad/name:1.0.0"),
            )

    def test_id_with_invalid_version_segment_rejected(self) -> None:
        with pytest.raises(ValidationError, match="invalid version segment"):
            AgentMetadata(
                **_valid_kwargs(id="akosha:akosha-specialist:1.0.0/../../etc"),
            )


class TestSchemaInvariants:
    """Default values and field constraints beyond the allowlist."""

    def test_default_dependencies_empty(self) -> None:
        """Omitting ``dependencies`` yields ``[]`` via default_factory."""
        kwargs = _valid_kwargs()
        del kwargs["dependencies"]
        m = AgentMetadata(**kwargs)
        assert m.dependencies == []

    def test_default_tools_empty(self) -> None:
        """Omitting ``tools`` yields ``[]`` via default_factory."""
        kwargs = _valid_kwargs()
        del kwargs["tools"]
        m = AgentMetadata(**kwargs)
        assert m.tools == []

    def test_default_scope_user_global(self) -> None:
        m = AgentMetadata(**_valid_kwargs(scope="user-global"))
        assert m.scope == "user-global"

    def test_default_status_active(self) -> None:
        m = AgentMetadata(**_valid_kwargs(status="active"))
        assert m.status == "active"

    def test_extras_forbidden(self) -> None:
        """Unknown fields raise ValidationError (model_config extra='forbid')."""
        with pytest.raises(ValidationError, match="Extra inputs"):
            AgentMetadata(**_valid_kwargs(unknown_field="x"))

    def test_signature_default_none(self) -> None:
        """``signature`` and ``server_pubkey_id`` default to None (unsigned)."""
        m = AgentMetadata(**_valid_kwargs())
        assert m.signature is None
        assert m.server_pubkey_id is None

    def test_description_max_length_1024(self) -> None:
        """Description is bounded at 1024 chars per plan §5 task #1."""
        long_desc = "x" * 1025
        with pytest.raises(ValidationError, match="description"):
            AgentMetadata(**_valid_kwargs(description=long_desc))

    def test_description_empty_after_strip_rejected(self) -> None:
        """Description must be non-empty after stripping whitespace."""
        with pytest.raises(ValidationError, match="description must be non-empty"):
            AgentMetadata(**_valid_kwargs(description="   "))


class TestB6ContentHashIntegrity:
    """B-6 body integrity: ``content_hash`` MUST equal ``sha256(system_prompt)``.

    A forged ``content_hash`` is rejected at the model boundary.
    """

    def test_correct_content_hash_accepted(self) -> None:
        body = "Test system prompt body"
        expected = hashlib.sha256(body.encode("utf-8")).hexdigest()
        m = AgentMetadata(**_valid_kwargs(system_prompt=body, content_hash=expected))
        assert m.content_hash == expected

    def test_forged_content_hash_rejected(self) -> None:
        """A bogus ``content_hash`` value MUST raise ValidationError."""
        with pytest.raises(ValidationError, match="content_hash mismatch"):
            AgentMetadata(
                **_valid_kwargs(system_prompt="test body", content_hash="0" * 64),
            )

    def test_truncated_content_hash_rejected(self) -> None:
        """A too-short hash is rejected (wrong byte length)."""
        with pytest.raises(ValidationError, match="content_hash mismatch"):
            AgentMetadata(
                **_valid_kwargs(
                    system_prompt="test body",
                    content_hash="0" * 32,  # 32 hex chars (md5) ≠ 64 (sha256)
                ),
            )

    def test_empty_system_prompt_with_matching_hash(self) -> None:
        """Empty system_prompt is allowed at the schema level — agents_tools
        enforces the non-empty invariant at sign-time (B-6 install path).

        ``sha256(b"") == e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855``
        """
        empty_hash = hashlib.sha256(b"").hexdigest()
        m = AgentMetadata(
            **_valid_kwargs(system_prompt="", content_hash=empty_hash),
        )
        assert m.system_prompt == ""
        assert m.content_hash == empty_hash


class TestOptionalGovernanceFields:
    """The picker / federation surfaces read these fields when present."""

    def test_optional_fields_default_none(self) -> None:
        kwargs = _valid_kwargs()
        for field_name in ("title", "category", "owner", "status", "last_reviewed"):
            del kwargs[field_name]
        m = AgentMetadata(**kwargs)
        assert m.title is None
        assert m.category is None
        assert m.owner is None
        assert m.status is None
        assert m.last_reviewed is None

    def test_status_literal_constraint(self) -> None:
        """``status`` must be one of active/archived/draft."""
        with pytest.raises(ValidationError):
            AgentMetadata(**_valid_kwargs(status="experimental"))

    def test_scope_literal_constraint(self) -> None:
        """``scope`` must be one of user-global/project-local."""
        with pytest.raises(ValidationError):
            AgentMetadata(**_valid_kwargs(scope="session-scoped"))
