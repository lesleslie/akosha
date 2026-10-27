"""A started receiver must report a cycle, or /health 503s the server.

This file pins a live crash-loop.

``mcp_tool_call_feed``'s ``cycles_total`` was the receiver's BATCH count —
the number of Export RPCs received. That is zero from boot until a
producer actually exports something.

The health aggregator maps "producer alive but never cycled" to
``feed_never_populated``, which is DEGRADED, and severity 3 is a **503 on
the /health route**. And that 503 is not cosmetic: the launchd wrapper
polls ``/health`` with ``curl -fsS``, and ``-f`` fails on any 4xx/5xx, so
the wrapper treats the degraded server it just started as a failure,
kills it, and exits 1. launchd restarts it; it comes up with the same
zero count; /health is 503 again.

The service was crash-looping on that loop.

The fix: the receiver counts its startup — bind the port, assert the
substrate — as its first cycle, and the feed reads THAT count rather than
the batch count. The batch count stays on the wire as
``otel_receiver_batches_total``, where "batches received" is the honest
semantic and zero at boot is correct.

Liveness is not this counter's job. "Is the producer ALIVE" is the
heartbeat watcher's (PR-A5), and "is the receiver WORKING" is the canary's
(PR-A4). The feed's cycle count only has to answer "has it started".
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from mcp_common.health.feed import HealthFeedState, is_healthy

from akosha.observability.otlp_receiver import OTLPReceiver
from akosha.storage.hot_store import HotStore


@pytest.fixture
async def store() -> HotStore:
    hs = HotStore(database_path=":memory:", embedding_dim=384)
    await hs.initialize()
    return hs


@pytest.fixture
def receiver(store: HotStore) -> OTLPReceiver:
    return OTLPReceiver(store, grpc_port=0, http_port=0)


async def test_a_started_receiver_counts_its_startup_as_a_cycle(
    receiver: OTLPReceiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binding the listeners is a completed producer cycle.

    The bind methods are stubbed so the test does not need real ports —
    the assertion is about the bookkeeping, not the sockets.
    """
    monkeypatch.setattr(receiver, "_start_grpc", AsyncMock())
    monkeypatch.setattr(receiver, "_start_http", AsyncMock())

    assert receiver.cycles_total == 0, "not started yet"
    await receiver.start()

    assert receiver.cycles_total == 1, "the startup cycle must be counted"
    assert receiver.batches_total == 0, "no batch has been received yet"


async def test_a_batch_advances_both_counts(receiver: OTLPReceiver) -> None:
    """Receiving a batch is a cycle AND a batch."""
    await receiver._handle_batch([])

    assert receiver.batches_total == 1
    assert receiver.cycles_total == 1


def test_a_zero_cycle_count_is_what_breaks_the_health_route() -> None:
    """The aggregator's verdict, for both counts.

    This is the contract the fix rests on: ``cycles_total=0`` with the
    producer alive is FAILED (a 503), and ``cycles_total>=1`` is
    warming_up (a 200). If mcp-common ever changes this mapping, this
    test says so.
    """
    never_cycled = HealthFeedState(
        entities_count=0, cycles_total=0, ingester_running=True
    )
    started = HealthFeedState(
        entities_count=0, cycles_total=1, ingester_running=True
    )

    healthy, status, _codes = is_healthy(never_cycled)
    assert not healthy, "a never-cycled producer must not read as healthy"
    # DEGRADED, not FAILED: the producer is alive and has simply not
    # cycled. Both are 503 on the /health route -- the route is 200 iff
    # every feed is healthy-or-warming_up -- so the distinction does not
    # change the crash-loop, only the reason code an operator sees.
    assert status.value == "degraded", status.value

    healthy, status, _codes = is_healthy(started)
    # ``healthy`` is the LEGACY boolean ("is this feed healthy"), and an
    # empty-but-started feed is legitimately not "healthy" -- it is warming
    # up. What the /health route keys on is the STATUS: warming_up is a
    # 200, degraded is a 503.
    assert not healthy, "an empty feed is not 'healthy'; it is warming up"
    assert status.value == "warming_up", status.value
