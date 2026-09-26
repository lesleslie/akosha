---
name: fitness-analyzer
description: Use ONLY when the user explicitly types `/akosha:fitness-analyzer` or selects this Skill from the picker to trigger one cycle of the Bodai fitness analyzer. Do not auto-trigger. Routes through `mcp__akosha__akosha_run_fitness_analysis` to poll every Bodai component endpoint for OTel traces and compute rolling fitness signals (failure_rate, p99 latency) per (task_class, selector) pair. Signals are returned in the tool response and cached on the analyzer instance — the historical Dhara persistence layer was removed when Dhara was decommissioned. Useful when the user asks "is the routing layer healthy?", "which selectors are failing?", or wants to confirm a recent deploy did not regress pool performance.
allowed-tools: mcp__akosha__akosha_run_fitness_analysis, mcp__akosha__akosha_get_fitness_analyzer_status, Read
---

# fitness-analyzer

## When to use

This Skill is the right entry point when the user wants to know how
the Mahavishnu routing layer is performing across the Bodai
ecosystem. The analyzer polls every registered component endpoint for
OTel traces and aggregates them by `(task_class, selector)` pair.
The historical Dhara persistence layer was removed when Dhara was
decommissioned; signals are now returned in-memory by the analyzer
and cached on the analyzer instance.

Common cases:

- "Is the pool layer healthy right now?"
- "Which selectors are failing on CODE_GENERATION tasks?"
- "Did the last deploy regress fitness?"
- "Give me a fitness snapshot before/after this change."

## What the analyzer produces

The analyzer computes a rolling-window fitness signal for every
`(task_class, selector)` pair observed in the trace corpus:

- **failure_rate**: fraction of invocations that failed in the window.
- **p99 latency**: 99th-percentile wall-clock latency (seconds).
- **sample_count**: number of traces aggregated.

Selectors include `least_loaded`, `round_robin`, `random`, `affinity`,
and any custom selectors Mahavishnu has registered.

## How to drive it

The analyzer runs as a 60-second background task by default
(`AKOSHA_KG_REFRESH_SECONDS` is unrelated; the analyzer interval is
configured inside `akosha.processing.fitness_analyzer`). The Skill
exposes two MCP tools:

- `mcp__akosha__akosha_run_fitness_analysis` — manually trigger one
  cycle. Returns counts immediately; doesn't wait for the background
  poll.
- `mcp__akosha__akosha_get_fitness_analyzer_status` — surface the
  current analyzer state: `running`, `component_endpoints`,
  `poll_interval_seconds`.

## Interpreting the response

`run_fitness_analysis` returns:

```json
{
  "status": "completed" | "no_data" | "error",
  "task_classes": ["CODE_GENERATION", "DEBUGGING", ...],
  "selectors_per_class": {"CODE_GENERATION": 4, "DEBUGGING": 3},
  "total_signals": 42
}
```

- `task_classes`: the set of task classes observed in the latest poll.
- `selectors_per_class`: how many distinct selectors wrote signals
  per task class.
- `total_signals`: total `(task_class, selector)` pairs written.

For per-pair values (failure_rate, p99), call
`akosha_run_fitness_analysis` again — signals are returned in the
tool response and cached on the analyzer instance rather than being
persisted to a backing store.

## When to flag trouble

- `status == "no_data"` for multiple cycles: the OTel pipeline is
  not delivering traces. Check `code_graphs_feed` and
  `local_traces_feed` in `/health`.
- `status == "error"`: the analyzer itself errored; surface the
  `error` field verbatim.
- `total_signals` dropping over time: a selector is silently failing
  and not emitting traces.

## Example flow

User: "Is routing healthy right now?"

Skill action:

```python
status = await mcp__akosha__akosha_get_fitness_analyzer_status()
if not status["running"]:
    return "FitnessAnalyzer is not running; cannot report."

result = await mcp__akosha__akosha_run_fitness_analysis()
```

Synthesize: total signal count, per-task-class breakdown, and any
error conditions. If `status != "completed"`, surface that first.
