"""Tests for Oneiric action-kit adoption in Akosha.

Wave 3 (W3) migration:
- ``AuditLogger.log`` -> oneiric.actions.workflow.WorkflowAuditAction
- ``sanitize_span_attributes`` -> oneiric.actions.data.DataSanitizeAction
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from akosha.api.middleware import AuditLogger, _audit_action
from akosha.observability.tracing import (
    _PII_MASK_FIELDS,
    asanitize_span_attributes,
    sanitize_span_attributes,
)


@pytest.fixture(autouse=True)
def _reset_audit_action_cache() -> None:
    _audit_action.cache_clear()
    yield
    _audit_action.cache_clear()


@pytest.mark.asyncio
async def test_audit_action_redacts_secrets() -> None:
    """The canonical audit envelope redacts secret-shaped fields."""
    action = _audit_action()
    record = await action.execute(
        {
            "event": "user.login",
            "details": {
                "user_id": "u-1",
                "password": "hunter2",
                "authorization": "Bearer abc",
                "api_key": "sk-xxxxx",
                "safe": "ok",
            },
            "include_timestamp": True,
        }
    )

    assert record["status"] == "recorded"
    assert record["details"]["safe"] == "ok"
    # The kit masks configured redact_fields with '***'.
    for redact in ("password", "authorization", "api_key"):
        assert record["details"][redact] == "***"


@pytest.mark.asyncio
async def test_audit_logger_writes_canonical_envelope(tmp_path: Path) -> None:
    """AuditLogger.log writes JSONL with redacted details via WorkflowAuditAction."""
    log_file = tmp_path / "audit.log"
    logger = AuditLogger(log_file=str(log_file))

    await logger.log(
        user_id="u-42",
        action="create",
        resource="upload:abc",
        result="success",
        details={
            "ip": "10.0.0.1",
            "password": "leaked-pw",
            "metadata": {"token": "tk-1", "note": "fine"},
        },
    )

    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])

    assert entry["action"] == "create"
    assert entry["resource"] == "upload:abc"
    assert entry["user_id"] == "u-42"
    # The flat envelope preserves the original ``details`` shape so existing
    # log consumers keep working, but the secrets are still redacted.
    assert entry["details"]["password"] == "***"
    assert entry["details"]["metadata"]["token"] == "***"


def test_pii_mask_fields_constant_is_complete() -> None:
    """The constant covers the canonical PII surface expected by the ecosystem."""
    required = {"email", "password", "token", "api_key", "authorization"}
    assert required.issubset(set(_PII_MASK_FIELDS))


def test_sanitize_span_attributes_sync_masks_pii() -> None:
    attrs = {
        "email": "alice@example.com",
        "token": "tk-leaked",
        "function.name": "ingest_upload",
        "duration_ms": 42,
    }
    out = sanitize_span_attributes(attrs)
    assert out["email"] == "***"
    assert out["token"] == "***"
    assert out["function.name"] == "ingest_upload"
    assert out["duration_ms"] == 42


@pytest.mark.asyncio
async def test_asanitize_span_attributes_masks_pii() -> None:
    attrs = {"password": "x", "metadata.note": "ok"}
    out = await asanitize_span_attributes(attrs)
    assert out["password"] == "***"
    assert out["metadata.note"] == "ok"


def test_sanitize_span_attributes_rejects_running_loop(monkeypatch) -> None:
    """Calling sync helper inside a running loop fails loudly rather than hanging."""
    import asyncio

    async def _in_loop() -> None:
        with pytest.raises(RuntimeError, match="asanitize_span_attributes"):
            sanitize_span_attributes({"x": 1})

    asyncio.run(_in_loop())
