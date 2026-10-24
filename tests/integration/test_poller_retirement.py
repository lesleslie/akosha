"""PR-A8: the legacy OtelTraceIngester poller is retired behind a flag.

Spec §14 requires this file. Four things are asserted, and each one is a
distinct failure mode:

1. The poller does not run by default. It POSTs an empty JSON envelope to
   an OTLP/HTTP endpoint every 60s and expects spans back; no standard
   receiver answers that, so it has ingested nothing since the OTLP receiver
   took :4318.

2. The retired env var appears nowhere in ``akosha/mcp/server.py``. It gates
   nothing, and an operator who set it would believe the poller was off when
   the settings key is what actually controls it. The literal string is
   checked rather than "no reference to the variable", because a stale
   mention in a comment still teaches an operator the wrong knob.

3. The class is STILL THERE. It is retained behind the flag precisely so
   rollback is a flag flip plus a restart and never a code revert. Deleting
   it would turn the flag into "a settings key that maps to deleted code" --
   a documentation lie that outlives the code.

4. The health feeds are sourced from the OTLP receiver, not the retired
   poller. This is the regression that made /health return 503: with the
   poller gone, ``mcp_tool_call_feed`` had entities_count=0,
   cycles_total=0 and ingester_running=False, which
   ``mcp_common.health.feed.is_healthy`` maps to FAILED, and severity 3 is
   a 503 on the /health route.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from akosha.config import get_config

SERVER_PY = Path(__file__).resolve().parents[2] / "akosha" / "mcp" / "server.py"


def test_the_poller_does_not_run_by_default() -> None:
    """The shipped setting is off."""
    assert get_config().otel.receiver.trace_ingester_poller is False


def test_the_retired_env_var_appears_nowhere_in_the_lifespan() -> None:
    """The string must be gone, not merely unread.

    A comment that still names it teaches an operator the wrong knob, which
    is the specific failure the retirement was meant to prevent.
    """
    source = SERVER_PY.read_text()
    assert "AKOSHA_SKIP_OTEL_INGESTER" not in source, (
        "the retired skip env var still appears in akosha/mcp/server.py; an "
        "operator who set it would believe it controls something"
    )


def test_the_poller_class_is_retained_for_rollback() -> None:
    """Rollback is a flag flip plus a restart, never a code revert.

    If the class is deleted, flipping the flag to true would do nothing and
    the only way back would be a git revert -- which is exactly what the
    spec forbids.
    """
    from akosha.ingestion.otel_ingester import OtelTraceIngester

    assert hasattr(OtelTraceIngester, "start")
    assert hasattr(OtelTraceIngester, "stop")


@pytest.mark.asyncio
async def test_the_lifespan_gates_the_poller_on_the_settings_key() -> None:
    """The gate is the config key, not an env var.

    Checked against the source rather than by running the lifespan: the
    lifespan's full dependency fixture is heavy, and this is a property of
    the CODE SHAPE -- which is exactly what the retirement changed.
    """
    source = SERVER_PY.read_text()
    assert "trace_ingester_poller" in source, (
        "the lifespan must gate the poller on "
        "otel.receiver.trace_ingester_poller"
    )
    # The gate must actually be consulted -- a mention in a comment would
    # pass the check above.
    assert "if get_config().otel.receiver.trace_ingester_poller:" in source


def test_the_health_feeds_are_sourced_from_the_receiver() -> None:
    """``/health`` must not 503 because the poller stopped.

    With the poller gone, ``mcp_tool_call_feed`` would have
    entities_count=0, cycles_total=0 and ingester_running=False, which
    ``mcp_common.health.feed.is_healthy`` maps to FAILED -- and severity 3
    is a 503 on the /health route. The feed read warming_up only because
    the poller's cycles_total kept climbing while it ingested nothing.

    The dynamic coverage of this lives in the unit tests (the lifespan and
    health-probe tests); what matters here is that the feed no longer reads
    the retired producer at all.
    """
    source = SERVER_PY.read_text()
    assert "mcp_tool_call_cycles" not in source, (
        "mcp_tool_call_feed must not source its cycles from the retired "
        "poller's counters"
    )
    assert "receiver_running" in source
    assert "otel_receiver_running" in source
