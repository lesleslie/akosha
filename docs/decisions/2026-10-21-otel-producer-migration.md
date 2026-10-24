---
status: active
kind: decision
date: 2026-10-21
topic: substrate-otel
plan: docs/plans/2026-10-20-bodai-otel-substrate-read-path-implementation.md
spec: docs/specs/2026-10-20-bodai-otel-substrate-read-path.md
---

# Migrating a producer onto the OTel substrate

Status: **written, not yet executable.** The read path exists (R-1/R-2/R-3 are
landed). The *writer* does not. Section "Preconditions" below is the honest
state as of 2026-10-21, and it is why this runbook is documentation rather
than a set of instructions anyone can follow end-to-end today.

## What you are migrating

Producers today export OpenTelemetry spans over OTLP to a collector. The
substrate wants them in `spans_substrate`, a DuckDB table in akosha, read back
through the `search_spans` / `spans_summary` MCP tools. Migration means
repointing one exporter and restarting the producer.

## Preconditions — none of these are met yet

**The substrate has no writer.** No code in any of the six Bodai repos
`INSERT`s into `spans_substrate`. Two v7 plan tasks are unbuilt:

| Missing | v7 task | Where it would live |
|---|---|---|
| `spans_substrate` DDL | PR-0a | `oneiric/adapters/vector/duckdb_hot_store.py` |
| OTLP receiver (gRPC :4317 / HTTP :4318) | PR-A3 | `akosha/observability/otlp_receiver.py` |

Until both land, `search_spans` returns `[]` no matter what any producer does.

**Ports :4317 and :4318 are already taken.** `lsof` shows **Grafana Alloy**
(PID 942) holding both on 127.0.0.1. This is the trap in this migration: the
receiver's intended ports are occupied by a collector that *accepts* OTLP and
writes to Grafana's own backend. Repointing mahavishnu at :4317 today would
**succeed at the TCP level, send every span into Alloy, and leave
`spans_summary` still returning zeros.** An operator would conclude the
substrate is broken. It is not — it has no writer. Settle the port before
anything else: the receiver moves, Alloy moves, or Alloy is repointed.

**Mahavishnu's spans carry no pair attributes today.**
`ObservabilityConfig.tool_enrichment_enabled` defaults `False`
(`mahavishnu/core/config.py:1435-1444`), so the enrichment middleware that
attaches `selector`, `task_class`, `outcome`, and `duration_ms` is off. Without
it, `spans_summary` matches on a NULL pair and returns
`latency_p99_ms: None` forever. **Turn enrichment on as part of the migration,
or the aggregate is hollow** — and a NULL-pair aggregate is exactly the
false-negative §5.7 warns about.

**Namespace mismatch.** Spans are stamped `service.namespace = "mcp-ecosystem"`
(mcp-common `server/telemetry.py:52`). The DDL has `service_namespace NOT NULL`,
and the namespace flip is v7 PR-A8. Confirm the receiver accepts
`mcp-ecosystem` before repointing, or land the flip first.

**The span processor is synchronous.** `SimpleSpanProcessor`
(`mahavishnu/core/observability.py:140`) exports on every span end, inline.
Repointing from a network collector to a database writer puts write latency on
the MCP tool-call hot path. If that shows up, the fix is a batching processor —
not a repoint back.

## The procedure, once the preconditions are met

**1. Baseline first.** Record what the tool returns *before* changing anything:

```bash
akosha call search_spans --service_name mahavishnu
```

Non-empty means a producer already has a path you did not know about — stop
and reconcile. Do not proceed on the assumption that the only writer is the
canary.

**2. Repoint.** A producer repoints by changing its OTLP exporter endpoint and
restarting. **There is no flag on the PRODUCER side.** The v7 plan deleted
`otel.receiver.trace_ingester_poller` rather than flipping it, because a
settings key that maps to deleted code is a documentation lie that outlives the
code.

**Superseded 2026-10-23 (PR-A8).** That reasoning applies to the PRODUCER
repoint path, which has no flag and never did. The RECEIVER side is
different: `otel.receiver.trace_ingester_poller` now exists and gates the
legacy `OtelTraceIngester`, because the class is retained behind the flag
rather than deleted. The objection above ("a settings key that maps to deleted
code") does not apply when the code is still there. Rollback is flipping the
key back to true and restarting, never a code revert. The retired
`AKOSHA_SKIP_OTEL_INGESTER` env var appears nowhere in `akosha/mcp/server.py`.

For mahavishnu specifically, the knob is an env var and **not** the file the
plan names:

```bash
export MAHAVISHNU_OBSERVABILITY__OTLP_ENDPOINT=http://localhost:4317
```

> The implementation plan's R-5 names `mahavishnu/config/otel-collector-config.yaml`
> as the file to edit. **That is the wrong file** — it is stale docker-compose-era
> config (`endpoint: jaeger:4317`, `0.0.0.0` binds) and nothing reads it. The
> live path is `ObservabilityConfig.otlp_endpoint` via the env var above.
> Editing the YAML would appear to work and change nothing.

**3. Verify — at the FULL profile.** `search_spans` and `spans_summary` are
FULL-profile-only tools. Under `AKOSHA_TOOL_PROFILE=standard` they are absent
from the tool list and the check "fails" against an empty registry rather than
empty data. Set `AKOSHA_TOOL_PROFILE=full` before verifying.

What "working" looks like:

```bash
# rows arrive, with the operator-facing service name — not just a row count
akosha call search_spans --service_name mahavishnu --limit 5

# and the pair aggregate is real, not a NULL-pair zero
akosha call spans_summary --task_class mcp_tool_call --cutoff_unix_nano <now-1h>
```

Confirm **the service name an operator would recognise**. A count of 40 rows
labelled `unknown-service` is not a successful migration.

**4. Rollback.** Unset the env var and restart. That is the whole procedure,
because there is exactly one exporter — a repoint, not a dual-write.

## No dual-write, deliberately

Do not run a producer into both paths during the cutover. Spans written twice
dedup invisibly at `ON CONFLICT` on `uq_span_identity`, so a "rollback" that
left the old path running would leave **no trace that it ever happened** and
the two sources could not be reconciled after the fact. The cutover is a
restart; the old path is a config line away and nothing else.

## Until a producer repoints, empty is correct

`search_spans` returning `[]` and `spans_summary` returning zeros is the
designed state, not a fault. The receiver creates the DDL
(`_ensure_operational_tables`); until it runs, no table exists, and
`SpanQuery` returns empty rather than raising. Do not add a workaround to make
the tools return something before then — that is how a real bug gets hidden
behind a fake signal.

## What this means for `fitness_analyzer` (R-6)

The plan sequences R-6 after R-5 so the analyzer cannot derive its inputs from
a table holding only synthetic-canary rows. That reasoning holds and is worth
keeping.

But the plan's stated R-6 risk is wrong. It assumes the HTTP fan-out is
currently producing coverage that a migration would narrow. **`FitnessAnalyzer.add_component`
has zero call sites fleet-wide.** Both construction sites
(`akosha/mcp/tools/group_registers.py:246`, `akosha/mcp/tools/__init__.py:170`)
call `FitnessAnalyzer()` with no arguments, so `_component_endpoints` is empty,
`_analyze_and_persist` short-circuits, and `run_fitness_analysis` returns `{}`
every cycle. The poll loop still starts and burns a 60-second timer doing
nothing.

So there is no working coverage to narrow — but there is also **no baseline**.
That makes R-6's real risk sharper than the plan states: a rewrite can silently
change the failure-rate denominator (`error_count / len(traces)`, over all rows)
or the p99 population with nothing to diff the result against. Before R-6
touches `_compute_signal`, pin its current arithmetic with golden-value tests.
Do not infer that the analyzer works today because its tests pass.

## Related

- Plan: `docs/plans/2026-10-20-bodai-otel-substrate-read-path-implementation.md`
- v7 plan (PR-0a DDL, PR-A3 receiver, PR-A8 namespace flip):
  `docs/plans/2026-10-19-bodai-substrate-otel-v7-implementation.md`
- Feature tracking: `docs/feature-tracking/2026-10-21-search-spans-mcp-tool.md`