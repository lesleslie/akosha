"""The retention sweep trigger — the substrate's one mutating operation.

``akosha_search_spans`` and ``akosha_spans_summary`` read the substrate; this writes to it,
by asking the receiver's sweep loop to run one cycle now instead of waiting
out the interval (6h by default).

**Why this is a tool and not a CLI.** The substrate is an IN-MEMORY DuckDB
(``HotStore(database_path=":memory:")``) living inside the running akosha
process, and the receiver is a process-local singleton published to the MCP
server by ``set_shared_otel_receiver``. A CLI subprocess is a different
process: it has neither the rows nor the receiver. So "force a sweep" has to
be executed ON the server that owns the data — which is what an MCP tool
does, and what a CLI could not do without first becoming an HTTP client.

**Why this SIGNALS instead of sweeping.** The sweep loop owns the single
DuckDB connection. A caller that ran the sweep itself would put a second
``DELETE`` against that connection, and two coroutines deleting on one
DuckDB handle is a race, not a feature. So the tool sets an event and
returns; the loop — the sole writer — wakes and sweeps.

**Why it returns before the sweep lands.** The sweep is a ``DELETE`` over an
in-memory table: fast, but not instant, and the tool has no business
blocking an MCP call on it. The operator confirms it landed by watching
``akosha_otlp_sweep_last_completed_unix_nano`` on the metrics route; the
response names that gauge so they do not have to remember it.
"""

from __future__ import annotations

from typing import Any

__all__ = ["register_otel_retention_tools"]


def register_otel_retention_tools(app: Any) -> None:
    """Register the retention sweep trigger.

    Mirrors ``otel_span_tools.register_otel_span_tools``: ``@app.tool(name=...)``
    inside a ``register_*_tools(app)`` function, no module-level ``mcp``.
    """

    @app.tool(name="akosha_force_retention_sweep")
    async def akosha_force_retention_sweep() -> dict[str, Any]:
        """Ask the receiver to run a retention sweep now.

        The sweep removes rows older than ``spans_substrate.retention_days``
        (30 by default) from ``spans_substrate``, plus canary rows older
        than 24 hours and ``_dlq_rows`` older than 24 hours.

        Use this when the substrate is under pressure — the table is
        IN-MEMORY with a 1GB limit — and reclaiming space now matters more
        than waiting out the sweep interval.

        The call RETURNS IMMEDIATELY. It queues the request; it does not
        wait for the sweep to finish. Confirm it landed by watching
        ``akosha_otlp_sweep_last_completed_unix_nano`` on
        ``http://localhost:8682/metrics`` — it advances when the sweep
        completes.

        The request is idempotent while one is pending: calling this twice
        in a row queues ONE sweep, and the second call reports
        ``already_pending: true``. The next automatic sweep still runs a
        full interval after the requested one.

        Returns:
            ``requested`` — True when the request was queued, False when it
            could not be (the receiver is not running).

            ``already_pending`` — True when a request was already queued, so
            this call did not add a second sweep.

            ``interval_s`` — the interval the running sweep loop is on, in
            seconds. This is the receiver's ACTUAL interval, not a re-read of
            the settings: if the settings changed since boot, this is the one
            the operator would have waited out.

            ``watch`` — the gauge that confirms the sweep landed.
        """
        # Imported here, not at module scope: ``akosha.mcp.server`` reaches
        # this module through the profile dispatch (profiles ->
        # group_registers -> here), so a top-level import closes the cycle.
        from akosha.mcp.server import get_shared_otel_receiver

        receiver = get_shared_otel_receiver()
        if receiver is None:
            # No receiver means no sweep loop to wake. The empty contract
            # here is "not requested", with the reason -- not a silent
            # success, which would read as "the sweep is coming".
            return {
                "requested": False,
                "reason": "receiver_not_running",
                "detail": (
                    "The OTLP receiver is not running, so there is no sweep "
                    "loop to ask. The receiver starts with the MCP server's "
                    "lifespan; check /health on http://localhost:8682."
                ),
                "watch": "akosha_otlp_sweep_last_completed_unix_nano",
            }

        already_pending = not receiver.request_retention_sweep()
        return {
            "requested": True,
            "already_pending": already_pending,
            "interval_s": receiver.sweep_interval_s,
            "watch": "akosha_otlp_sweep_last_completed_unix_nano",
        }
