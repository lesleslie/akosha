"""Shared wiring for the event subscribers that feed the hot tier.

Both the standalone FastAPI application (``akosha/main.py``) and the
MCP server (``akosha/mcp/server.py``) need the same pair of
subscribers:

- ``BodaiToolInvocationSubscriber`` — the Redis-Streams push path
  (``bodai:events`` -> ``hot_store.insert``). This is the ONLY producer
  of the ``conversations`` table inside the MCP process.
- ``WebSocketInvocationsSubscriber`` — the orchestrator that owns
  ingestion for those rows.

Before this module existed, the subscribers were wired ONLY into the
standalone FastAPI application. The only Akosha process that actually
runs is ``python -m akosha mcp start`` (``akosha/cli.py:mcp_start``),
so the MCP process had no producer at all: the knowledge-graph refresh
loop read an empty ``conversations`` table on every cycle and the graph
stayed permanently empty.

Config is read through :func:`akosha.config.get_config` — the Oneiric
layered loader — so the operator's ``~/.config/akosha/local.yaml``
overlay is honoured. The previous implementation read
``<repo>/settings/akosha.yaml`` directly, which bypassed the XDG layer
entirely and made the overlay a no-op.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from akosha.config import (
        BodaiToolInvocationSubscriberConfig,
        WebSocketInvocationsSubscriberConfig,
    )

logger = logging.getLogger(__name__)

#: Opt-out for offline test suites and for operators who deliberately
#: run Akosha without Redis. Mirrors the ``AKOSHA_SKIP_*`` family used by
#: the other lifespan-owned background producers.
SKIP_ENV_VAR = "AKOSHA_SKIP_EVENT_SUBSCRIBER"


def build_event_subscribers(
    hot_store: Any,
    *,
    websocket_cfg: WebSocketInvocationsSubscriberConfig,
    bodai_cfg: BodaiToolInvocationSubscriberConfig,
) -> Any | None:
    """Build the event-subscriber stack for a hot store.

    Args:
        hot_store: The lifespan-owned hot store the subscribers write to.
        websocket_cfg: The ``websocket_invocations_subscriber`` block.
        bodai_cfg: The ``bodai_tool_invocation_subscriber`` block.

    Returns:
        A started-or-startable ``WebSocketInvocationsSubscriber``, or
        ``None`` when the stack is disabled. The subscriber's own
        ``start()`` fails soft: if Redis is unreachable it logs a warning
        and stays not-running rather than taking the process down.
    """
    if os.getenv(SKIP_ENV_VAR, "").strip().lower() in {"1", "true", "yes", "on"}:
        logger.info(
            "%s is set; the event subscriber is not started",
            SKIP_ENV_VAR,
        )
        return None

    if not websocket_cfg.enabled:
        logger.info(
            "websocket_invocations_subscriber disabled; the conversations "
            "table will not be populated and the knowledge graph stays empty"
        )
        return None

    try:
        from akosha.ingestion.bodai_event_subscriber import (
            BodaiToolInvocationSubscriber,
        )
        from akosha.ingestion.websocket_invocations_subscriber import (
            WebSocketInvocationsSubscriber,
        )
    except Exception as exc:  # pragma: no cover - import-time failure
        logger.warning("Event subscriber imports failed (%s); subscriber disabled", exc)
        return None

    bodai_sub: Any | None = None
    if bodai_cfg.enabled:
        bodai_sub = BodaiToolInvocationSubscriber(
            redis_url=bodai_cfg.redis_url,
            consumer_group=bodai_cfg.consumer_group,
            hot_store=hot_store,
            xreadgroup_block_ms=bodai_cfg.xreadgroup_block_ms,
            per_event_timeout_seconds=bodai_cfg.per_event_timeout_seconds,
        )
    else:
        logger.info(
            "bodai_tool_invocation_subscriber disabled; no Redis-Streams "
            "producer will populate the conversations table"
        )

    return WebSocketInvocationsSubscriber(
        hot_store=hot_store,
        poll_interval_seconds=websocket_cfg.poll_interval_seconds,
        bodai_subscriber=bodai_sub,
    )


async def start_event_subscriber(subscriber: Any | None) -> bool:
    """Start the subscriber stack, reporting whether it is running.

    Returns:
        ``True`` when the subscriber started and reports itself running,
        ``False`` otherwise. Never raises: a subscriber that cannot reach
        Redis must not take the server down.
    """
    if subscriber is None:
        return False
    try:
        await subscriber.start()
    except Exception as exc:
        logger.warning("Event subscriber start failed (%s)", exc)
        return False
    running = bool(getattr(subscriber, "running", False))
    if not running:
        logger.warning(
            "Event subscriber did not start (source=%s); the conversations "
            "table will stay empty and the knowledge graph will stay empty",
            getattr(subscriber, "source", None),
        )
    return running


async def stop_event_subscriber(subscriber: Any | None) -> None:
    """Stop the subscriber stack. Never raises."""
    if subscriber is None:
        return
    try:
        await subscriber.stop()
    except Exception as exc:
        logger.warning("Event subscriber stop failed (%s)", exc)
