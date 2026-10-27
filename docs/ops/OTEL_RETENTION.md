---
status: active
role: operations
date: 2026-10-27
last_reviewed: 2026-10-27
topic: otel-retention
superseded_by: null
related_decisions:
  - mahavishnu/docs/specs/2026-10-19-bodai-substrate-otel-v7.md#114a-operator-interface-for-the-substrate-v9--rewritten-2026-10-27
audited_by: []
---

# OTel substrate retention

How expired spans are removed from `spans_substrate`, and how to make room
now instead of at the next interval.

## The model

The receiver owns three tables (all DuckDB, DDL owned by oneiric):

| Table | Horizon | What a row is |
|---|---|---|
| `spans_substrate` | `spans_substrate.retention_days` (30) | a received span |
| `spans_substrate` (canary rows) | flat 24h | the receiver's own liveness marker |
| `_dlq_rows` | flat 24h | a span the validator rejected |

One sweep runs both horizons. It fires every
`spans_substrate.retention_sweep_interval_hours` — **6 hours by default** —
so the worst-case wait for space to be reclaimed is six hours.

**The substrate is IN-MEMORY** (`HotStore(database_path=":memory:")`) with a
1GB limit. A restart does not lose the rows, but a pod that OOMs does.
Rows are liveness evidence, not history, which is why the canary and the
DLQ get a flat 24h rather than the 30-day horizon.

## The cardinal rule

Retention filters on **`received_at_unix_nano`** — the receiver's clock.
Never on `start_time_unix_nano`, which the producer supplies. A producer
with a skewed clock would otherwise pin rows past their expiry, or pin
live rows past a horizon they should not have reached.

## Forcing a sweep now

```
mcp__akosha__force_retention_sweep()
  -> {"requested": true, "already_pending": false,
      "interval_s": 21600,
      "watch": "akosha_otlp_sweep_last_completed_unix_nano"}
```

The call **queues** the request and returns. The sweep runs on the loop's
next turn. Confirm it landed:

```
curl -fsS http://localhost:8682/metrics \
  | grep "^akosha_otlp_sweep_last_completed_unix_nano"
```

### Why this is a tool and not a CLI command

Not a preference — an architectural fact. The substrate lives in the
running akosha process's memory, and the receiver is a process-local
singleton published to the MCP server by `set_shared_otel_receiver`. A CLI
subprocess is a *different process*: it has neither the rows nor the
receiver. The operation has to be triggered on the server that owns the
data, which is what an MCP tool does.

### Why it signals instead of deleting

The sweep loop owns the single DuckDB connection to the substrate. A
caller that ran the sweep itself would put a second `DELETE` against that
connection, and two coroutines deleting on one DuckDB handle is a race,
not a feature. So the tool sets an event; the loop — the sole writer —
wakes and sweeps.

### Other things it deliberately does not do

- **It does not queue a second sweep.** A second call while one is pending
  reports `already_pending: true`. Two DELETEs over the same rows is pure
  cost.
- **It does not reset the interval clock.** The next automatic sweep still
  runs a full interval after the requested one.
- **It does not report "done".** The call returns before the sweep lands.
  The response names the gauge to watch.

## Reading the sweep's state

```
curl -fsS http://localhost:8682/metrics \
  | grep -E "^akosha_otlp_sweep_(first_started|last_completed)_unix_nano"
```

| first_started | last_completed | Meaning |
|---|---|---|
| 0 | 0 | has not come due yet (unless an operator asked for one) |
| >0 | 0 | the last cycle FAILED |
| >0 | >0 | the last cycle completed cleanly |

`sweep_retention_late` is gated on `first_started > 0`, so a pod whose
sweep has simply not come due yet cannot page anyone.

`akosha_otlp_retention_sweep_failed_total{table}` says WHICH table failed.
The two sweeps on `spans_substrate` are labelled separately —
`spans_substrate` and `spans_substrate_canary` — so the canary's own
sweep is distinguishable from the substrate's.

## See also

- `mahavishnu/docs/specs/2026-10-19-bodai-substrate-otel-v7.md` §11.1.5
  (sweep failure runbook), §11.4a (operator interface table)
- `akosha/docs/ops/OTEL_TEMPO_HOOK.md` — the Alloy/Tempo pipeline
