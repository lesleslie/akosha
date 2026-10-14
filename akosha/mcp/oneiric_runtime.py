"""Boot push: report Akosha's runtime facts to Oneiric (Phase 3 Task 3.1).

Mirrors the Mahavishnu Phase 2 (Task 2.3) ``report_runtime_facts`` boot
push but in Akosha-flavored shape. The push is fire-and-forget: the
caller schedules the coroutine via ``asyncio.create_task`` from inside
the FastMCP lifespan, and the coroutine itself swallows every
exception so a degraded Oneiric instance never blocks Akosha boot.

Design contract:

* No oneiric imports. The push is a pure HTTP/JSON-RPC POST via
  :mod:`httpx` to ``http://127.0.0.1:8681/mcp`` (overridable via
  ``AKOSHA__ONBOARDING__ONEIRIC_URL``). The request envelope is the
  MCP JSON-RPC 2.0 ``tools/call`` for ``oneiric_report_runtime``.
* One POST per static agent. Each push is wrapped in ``try/except``
  so a single bad agent does not starve the others.
* The whole coroutine is wrapped in an outer ``try/except Exception``
  belt-and-braces: the documented contract is "Oneiric unavailability
  MUST NOT block Akosha boot" — so we err on the side of logging and
  returning rather than raising into the lifespan.
* Structured log line: ``runtime-push component=akosha
  adapter=<key> status=ok|fail``. ``adapter`` is the agent name in
  the static catalog; ``status=ok`` is logged once the Oneiric call
  returns 2xx with no JSON-RPC ``error`` envelope; ``status=fail`` is
  logged for every other outcome (timeout, connection refused, 5xx,
  JSON-RPC error, parse failure, schema mismatch).
"""

from __future__ import annotations

import logging
import os
import uuid
from time import time_ns
from typing import TYPE_CHECKING, Any

import httpx2 as httpx

if TYPE_CHECKING:
    from fastmcp import FastMCP


logger = logging.getLogger("akosha.mcp.oneiric_runtime")


# Endpoint defaults — mirrors Mahavishnu Phase 2 (mahavishnu/core/app.py:1316).
ONEIRIC_MCP_URL: str = "http://127.0.0.1:8681/mcp"
ONEIRIC_ENV_VAR: str = "AKOSHA__ONBOARDING__ONEIRIC_URL"
ONEIRIC_TIMEOUT_ENV_VAR: str = "AKOSHA__ONBOARDING__ONEIRIC_TIMEOUT_SECONDS"
DEFAULT_PUSH_TIMEOUT_SECONDS: float = 5.0

# Caller component tag baked into the Oneiric envelope so downstream
# observability can group pushes by source. Kept in sync with the
# ``mahavishnu`` / ``session-buddy`` tags used in their own boot pushes.
CALLER_COMPONENT: str = "akosha"

# Phase tag for the Oneiric registry schema. Bump only on breaking
# changes to the envelope shape (additive changes keep the same tag).
PHASE_TAG: str = "plan1-phase3-task3"


def _oneiric_url() -> str:
    """Resolve the Oneiric MCP endpoint URL from env (with default)."""
    return os.environ.get(ONEIRIC_ENV_VAR, ONEIRIC_MCP_URL)


def _push_timeout_seconds() -> float:
    """Resolve the per-push HTTP timeout in seconds (default 5s)."""
    raw = os.environ.get(ONEIRIC_TIMEOUT_ENV_VAR)
    if raw is None:
        return DEFAULT_PUSH_TIMEOUT_SECONDS
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "oneiric_runtime: invalid %s=%r; falling back to default %.1fs",
            ONEIRIC_TIMEOUT_ENV_VAR,
            raw,
            DEFAULT_PUSH_TIMEOUT_SECONDS,
        )
        return DEFAULT_PUSH_TIMEOUT_SECONDS


async def _push_one_agent(
    client: httpx.AsyncClient,
    oneiric_url: str,
    *,
    name: str,
    metadata: dict[str, Any],
    runtime_meta: dict[str, Any],
) -> None:
    """POST a single agent's runtime facts to Oneiric via JSON-RPC ``tools/call``.

    Raises on transport / JSON-RPC / schema failure so the caller can
    log ``status=fail`` and continue. The ``client`` is shared so we
    reuse the connection across pushes within one boot.
    """
    envelope: dict[str, Any] = {
        "success": True,
        "registry": {
            "agents": [
                {
                    "key": name,
                    "provider": CALLER_COMPONENT,
                    "domain": "agent",
                    "enabled": True,
                    "name": metadata.get("name", name),
                    "version": metadata.get("version", "unknown"),
                    "model": metadata.get("model", "unknown"),
                    "tools": list(metadata.get("tools") or []),
                }
            ],
            "domain": "agent",
        },
        "runtime": runtime_meta.copy(),
    }
    runtime_facts: dict[str, Any] = {
        "name": metadata.get("name", name),
        "title": metadata.get("title"),
        "version": metadata.get("version", "unknown"),
        "model": metadata.get("model", "unknown"),
        "tools": list(metadata.get("tools") or []),
        "tool_refs": list(metadata.get("tool_refs") or []),
        "category": metadata.get("category"),
        "owner": metadata.get("owner"),
        "status": metadata.get("status"),
        "last_reviewed": metadata.get("last_reviewed"),
        "scope": metadata.get("scope", "user-global"),
        "metadata": runtime_meta.copy(),
        "source": f"{CALLER_COMPONENT}.mcp.tools.agents_tools",
    }
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {
            "name": "oneiric_report_runtime",
            "arguments": {
                "domain": "agent",
                "key": name,
                "provider": CALLER_COMPONENT,
                "runtime_facts": runtime_facts,
                "caller_component": CALLER_COMPONENT,
                "envelope": envelope,
            },
        },
    }
    response = await client.post(oneiric_url, json=payload)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(f"oneiric rpc error: {data['error']}")


async def report_runtime_to_oneiric(app: FastMCP) -> None:
    """Push Akosha's static agent catalog to Oneiric as JSON-RPC ``tools/call``.

    Iterates :class:`RuntimeFactsBuilder`'s output, POSTs each agent's
    metadata to the Oneiric MCP endpoint, and logs
    ``runtime-push component=akosha adapter=<name> status=ok|fail``
    per agent.

    Fail-soft contract: every push is wrapped in ``try/except`` so a
    single bad agent or transient Oneiric outage is logged but does
    not abort the loop. An outer ``try/except Exception`` belt-and-
    braces guards against any unforeseen failure (e.g. ``_STATIC_AGENTS``
    import error) so the lifespan can never observe an exception from
    this coroutine.

    Parameters
    ----------
    app : FastMCP
        The FastMCP application instance. Forwarded to
        :class:`RuntimeFactsBuilder` so the builder can read
        app-owned signing keys / catalog overrides (mirrors the
        Mahavishnu Phase 2 caller surface).
    """
    try:
        from .runtime_facts import RuntimeFactsBuilder

        builder = RuntimeFactsBuilder(app)
        facts = builder.build_all()

        oneiric_url = _oneiric_url()
        timeout_seconds = _push_timeout_seconds()

        runtime_meta: dict[str, Any] = {
            "generated_at_unix": time_ns() // 1_000_000_000,
            "caller_component": CALLER_COMPONENT,
            "phase": PHASE_TAG,
        }

        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            for metadata in facts:
                # The ``name`` field is the agent identifier in the
                # registry; fall back to a synthetic key so a missing
                # name cannot crash the loop.
                name = str(metadata.get("name") or "unknown")
                try:
                    await _push_one_agent(
                        client,
                        oneiric_url,
                        name=name,
                        metadata=metadata,
                        runtime_meta=runtime_meta,
                    )
                except Exception:
                    logger.exception(
                        "runtime-push component=%s adapter=%s status=fail",
                        CALLER_COMPONENT,
                        name,
                    )
                    continue
                logger.info(
                    "runtime-push component=%s adapter=%s status=ok",
                    CALLER_COMPONENT,
                    name,
                )
    except Exception:
        logger.exception("runtime-push component=%s outer-failure status=fail", CALLER_COMPONENT)
        return
