"""The health surface must not lie about whether the producer is running.

``/health`` reports ``source_producer_running`` and
``event_subscriber_running`` for the knowledge-graph and local-traces
feeds. Those flags come from ``start_event_subscriber()``, which asks
the subscriber stack for its ``running`` attribute.

The stack is a ``WebSocketInvocationsSubscriber``. It sets
``self._running`` when it starts, but it never exposed a ``running``
PROPERTY -- so ``getattr(stack, "running", False)`` was always
``False``, for a producer that was running.

The live proof, on 2026-11-20: the akosha subscriber's read loop was
running and logging ``xreadgroup error: Timeout reading from
localhost:6379`` every 35 seconds, while ``GET /health`` reported
``source_producer_running: false``. The health surface was reporting
the opposite of the truth about the one producer that feeds the
knowledge graph -- which is how an empty feed reads as "nothing is
producing" when something IS producing.

This is the same class as the rest of the findings in this work: a
signal that cannot be acted on, because it says the opposite of what
is happening.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from akosha.ingestion.subscriber_wiring import (
    build_event_subscribers,
    start_event_subscriber,
)
from akosha.ingestion.websocket_invocations_subscriber import (
    WebSocketInvocationsSubscriber,
)


def _config(enabled: bool = True) -> Any:
    """A subscriber config block with the shape the wiring reads."""
    return MagicMock(
        enabled=enabled,
        redis_url="redis://localhost:6379/0",
        consumer_group="akosha-tool-invocation-indexers",
        xreadgroup_block_ms=1500,
        per_event_timeout_seconds=30.0,
        poll_interval_seconds=5,
    )


class TestTheStackExposesItsRunningState:
    """The stack's running state must be readable, not just private."""

    async def test_running_is_true_after_a_successful_start(self) -> None:
        stack = WebSocketInvocationsSubscriber(
            hot_store=MagicMock(),
            bodai_subscriber=MagicMock(running=True, start=AsyncMock()),
        )

        await stack.start()

        assert stack.running is True

    async def test_running_is_false_before_the_start(self) -> None:
        stack = WebSocketInvocationsSubscriber(
            hot_store=MagicMock(),
            bodai_subscriber=MagicMock(running=True, start=AsyncMock()),
        )

        assert stack.running is False

    async def test_running_is_false_after_the_stop(self) -> None:
        stack = WebSocketInvocationsSubscriber(
            hot_store=MagicMock(),
            bodai_subscriber=MagicMock(running=True, start=AsyncMock(), stop=AsyncMock()),
        )
        await stack.start()

        await stack.stop()

        assert stack.running is False

    async def test_running_is_false_when_the_bodai_subscriber_did_not_start(
        self,
    ) -> None:
        stack = WebSocketInvocationsSubscriber(
            hot_store=MagicMock(),
            bodai_subscriber=MagicMock(running=False, start=AsyncMock()),
        )

        await stack.start()

        assert stack.running is False


class TestTheStackProxiesEveryProducerCounter:
    """``/health`` reads the producer's counters off the STACK, not off
    the push subscriber.

    The health payload holds the object the lifespan built -- the
    ``WebSocketInvocationsSubscriber`` stack -- and reads the
    producer's four counters off it with ``getattr``. The stack proxied
    ``entries_consumed`` and NOTHING else, so the other three read 0 on
    a producer that was actively indexing and actively failing to read.

    The live proof, 2026-11-21: after the event-pipeline recovery run,
    ``/health`` reported ``entries_total: 14741`` (proxied, real),
    ``entries_candidate: 0``, ``entries_indexed: 0`` and
    ``read_errors_total: 0`` -- while the same process's log recorded
    170 duplicate-key indexing failures, 9 indexing timeouts and 31
    ``xreadgroup`` timeouts. The feed reported ``healthy`` with "no
    read errors" on a pipeline that was failing, and the stall signal
    (which gates on the candidate count) could never fire.

    ``getattr`` on a missing attribute is silent, so a missing proxy
    reads as a counter of zero. That is the failure mode this pins:
    a counter that cannot be absent.
    """

    @staticmethod
    def _stack_with_counters(**counters: int) -> WebSocketInvocationsSubscriber:
        return WebSocketInvocationsSubscriber(
            hot_store=MagicMock(),
            bodai_subscriber=MagicMock(
                running=True,
                start=AsyncMock(),
                entries_consumed=counters.get("entries_consumed", 0),
                entries_candidate=counters.get("entries_candidate", 0),
                entries_indexed=counters.get("entries_indexed", 0),
                read_errors=counters.get("read_errors", 0),
            ),
        )

    def test_it_proxies_entries_consumed(self) -> None:
        stack = self._stack_with_counters(entries_consumed=14741)

        assert stack.entries_consumed == 14741

    def test_it_proxies_entries_candidate(self) -> None:
        stack = self._stack_with_counters(entries_candidate=4099)

        assert stack.entries_candidate == 4099

    def test_it_proxies_entries_indexed(self) -> None:
        stack = self._stack_with_counters(entries_indexed=37)

        assert stack.entries_indexed == 37

    def test_it_proxies_read_errors(self) -> None:
        stack = self._stack_with_counters(read_errors=31)

        assert stack.read_errors == 31

    def test_every_counter_is_zero_without_a_bodai_subscriber(self) -> None:
        """No push subscriber means no producer, so no counts."""
        stack = WebSocketInvocationsSubscriber(hot_store=MagicMock())

        assert stack.entries_consumed == 0
        assert stack.entries_candidate == 0
        assert stack.entries_indexed == 0
        assert stack.read_errors == 0

    def test_a_counter_missing_from_the_subscriber_reads_as_zero(self) -> None:
        """A producer that has no counter yet is zero, not an error."""
        # ``spec`` matters: a bare MagicMock auto-creates every
        # attribute, so a missing counter would read as 1 (MagicMock's
        # ``__int__``) instead of exercising the ``getattr`` default.
        stack = WebSocketInvocationsSubscriber(
            hot_store=MagicMock(),
            bodai_subscriber=MagicMock(spec=["running", "start"]),
        )

        assert stack.entries_candidate == 0
        assert stack.entries_indexed == 0
        assert stack.read_errors == 0


class TestStartReportsTheTruth:
    """``start_event_subscriber`` is what the health flag is built from."""

    async def test_a_running_stack_is_reported_as_running(self) -> None:
        stack = WebSocketInvocationsSubscriber(
            hot_store=MagicMock(),
            bodai_subscriber=MagicMock(running=True, start=AsyncMock()),
        )

        assert await start_event_subscriber(stack) is True

    async def test_a_stack_that_did_not_start_is_reported_as_not_running(self) -> None:
        stack = WebSocketInvocationsSubscriber(
            hot_store=MagicMock(),
            bodai_subscriber=MagicMock(running=False, start=AsyncMock()),
        )

        assert await start_event_subscriber(stack) is False

    async def test_no_subscriber_is_reported_as_not_running(self) -> None:
        assert await start_event_subscriber(None) is False


class TestTheBodaiSubscriberIsNotGatedOnTheWebsocketOne:
    """The two subscriber blocks are independent flags.

    ``build_event_subscribers`` returned ``None`` when the WEBSOCKET
    subscriber was disabled, even when the BODAI subscriber -- the only
    producer of the ``conversations`` table, and therefore the only
    producer of the knowledge graph -- was enabled. An operator who
    enabled the producer was told, by the health surface, that nothing
    was producing.
    """

    def test_the_bodai_subscriber_alone_is_enough(self) -> None:
        stack = build_event_subscribers(
            MagicMock(),
            websocket_cfg=_config(enabled=False),
            bodai_cfg=_config(enabled=True),
        )

        assert stack is not None
        assert stack._bodai_subscriber is not None

    def test_neither_enabled_means_no_stack(self) -> None:
        stack = build_event_subscribers(
            MagicMock(),
            websocket_cfg=_config(enabled=False),
            bodai_cfg=_config(enabled=False),
        )

        assert stack is None

    def test_the_websocket_subscriber_alone_still_builds(self) -> None:
        """The websocket block alone is not enough to produce anything.

        The stack's start path needs the bodai subscriber, so with only
        the websocket block enabled the stack is built but starts
        nothing -- and must report that honestly.
        """
        stack = build_event_subscribers(
            MagicMock(),
            websocket_cfg=_config(enabled=True),
            bodai_cfg=_config(enabled=False),
        )

        assert stack is not None
        assert stack._bodai_subscriber is None


@pytest.mark.parametrize("bodai_enabled", [True, False])
def test_the_stack_is_built_whenever_a_producer_is_enabled(bodai_enabled: bool) -> None:
    """Whichever producer is enabled, the stack reflects it."""
    stack = build_event_subscribers(
        MagicMock(),
        websocket_cfg=_config(enabled=True),
        bodai_cfg=_config(enabled=bodai_enabled),
    )

    assert stack is not None
    assert (stack._bodai_subscriber is not None) is bodai_enabled


# ---------------------------------------------------------------------------
# The wiring from the running flag to the health payload
# ---------------------------------------------------------------------------


def _server_source() -> str:
    return (Path(__file__).resolve().parents[2] / "akosha" / "mcp" / "server.py").read_text()


def test_the_running_flag_comes_from_the_start_helper() -> None:
    """``_event_subscriber_running`` must BE the start helper's answer.

    The behavioural tests above prove the helper answers correctly.
    This proves the lifespan uses that answer -- the link between
    "the subscriber is running" and "the health payload says so".
    """
    tree = ast.parse(_server_source())
    from_helper = False
    assigned = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "_event_subscriber_running" not in targets:
            continue
        assigned = True
        value = node.value
        if isinstance(value, ast.Await):
            value = value.value
        if isinstance(value, ast.Call) and (
            getattr(value.func, "id", None) == "start_event_subscriber"
        ):
            from_helper = True
    assert assigned, "_event_subscriber_running is never assigned in akosha/mcp/server.py"
    assert from_helper, (
        "_event_subscriber_running must be assigned from "
        "start_event_subscriber(...) somewhere in akosha/mcp/server.py"
    )


def test_the_health_payload_publishes_the_flag() -> None:
    """``source_producer_running`` must be the flag, not a constant.

    A literal ``False`` in the health builder is what made the surface
    say "not producing" for a producer that was running.
    """
    tree = ast.parse(_server_source())
    published = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Subscript):
                continue
            key = target.slice
            if not isinstance(key, ast.Constant) or key.value not in {
                "source_producer_running",
                "event_subscriber_running",
            }:
                continue
            value = node.value
            assert isinstance(value, ast.Name) and value.id == "_event_subscriber_running", (
                f"the health payload's {key.value!r} must publish "
                "_event_subscriber_running, not a literal"
            )
            published = True
    assert published, (
        "akosha/mcp/server.py must publish _event_subscriber_running as "
        "source_producer_running / event_subscriber_running"
    )
