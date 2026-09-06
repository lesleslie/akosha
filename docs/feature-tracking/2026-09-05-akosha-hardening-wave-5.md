---
built: 2026-09-05
wired: 2026-09-05
adopted: null
phase: akosha-hardening-wave-5
topic: live-mcp-backend-wiring
---

# Akosha Hardening — Wave 5 (Live MCP Backend Wiring)

## What

Wave 5 closes the **mcp-surface-health-illusion** failure mode the
audit caught: the MCP server registered 30+ tools and returned 200
from ``/health``, but every data feed
(``get_graph_statistics``, ``query_local_traces``,
``search_code_patterns``) returned empty because no producer was ever
started.

Three deliverables (per the plan's Tasks 5.2 + 5.4 + 5.5 minus OTel
ingester):

1. **CodeGraphIngester wired in lifespan** (Task 5.4). The class at
   ``akosha/ingestion/code_graph_ingester.py`` was fully built
   (start / stop / polling loop / discover / ingest) but never
   instantiated. The lifespan now constructs one bound to the
   shared ``HotStore``, starts its 60-second polling loop, and stops
   it on shutdown.

2. **KnowledgeGraphBuilder populated in lifespan** (Task 5.2). The
   builder is constructed once in the lifespan, published as a
   singleton, and fed by a periodic-refresh task that pulls recent
   traces from ``hot_store.query_traces()`` and runs
   ``extract_entities`` → ``extract_relationships`` → ``add_to_graph``
   on each row.

3. **/health probe surfaces per-feed state** (Task 5.5 partial). The
   default probe now reports
   ``code_graphs_feed``, ``knowledge_graph_feed``, ``local_traces_feed``
   with ``feed_entities_count``, ``edges_count``,
   ``refresh_task_running``, ``ingester_running``, and source metadata
   per feed. Closes the wire-up-drift visibility gap from
   spec Appendix A finding A.5.

## Why

The W4 followups and earlier waves fixed the audit findings (empty
tests, missing coverage, P0-P2 bugs) but never touched the boot
lifecycle. The MCP server's lifespan was hard-coded to construct its
own ``hot_store`` + ``KnowledgeGraphBuilder`` once per lifespan but the
per-group tool wrappers (``register_akosha_group``,
``register_session_buddy_group``, ...) each call
``create_hot_store()`` themselves — producing fresh in-memory DuckDB
databases when the default backend is ``duckdb-memory``. Data
written by any lifespan-owned worker (such as the new
``CodeGraphIngester``) is invisible to the tool handlers.

The shared-singleton pattern (``set_shared_hot_store`` /
``set_shared_kg_builder``) resolves this: the lifespan publishes its
initialised instances; tool-group wrappers read from the singletons
first and fall back to per-call construction when unset (preserves
pre-Wave-5 behaviour for tests bypassing the full lifespan).

## Plan

``/Users/les/Projects/akosha/docs/superpowers/plans/2026-09-05-akosha-hardening-impl.md``
(Tasks 5.1–5.5; Wave 5 of 5)

## Test coverage

| File | Tests added |
|---|---|
| ``tests/unit/test_wave5_lifespan_wiring.py`` (new) | 10 — singletons, ingester lifecycle, kg refresh cycles + cancellation, health probe aggregates, group_registers singleton reuse |
| ``tests/unit/test_mcp_server_lifespan.py`` | 0 — added env vars so the existing lifespan test skips the new ingester/refresh paths (not designed for them) |
| ``tests/unit/test_main_entrypoints.py`` | 0 — added a ``marker`` parameter to ``_run()`` so the akosha.mcp smoke test SIGKILLs the subprocess the moment the lifespan reports ``tools registered``, instead of waiting for uvicorn to exit cleanly |

## Commits (Wave 5)

| Commit | What |
|---|---|
| ``0590444`` | Task 5.1 — investigation report in spec Appendix A (7 findings: AkoshaApplication not on MCP path; kg_builder empty; CodeGraphIngester orphan; no otel_ingester.py; /health misses feed counts; default profile hides empty feeds; 3 priority gaps) |
| ``3876d44`` | Tasks 5.2 + 5.4 + 5.5 — wire CodeGraphIngester + kg population + /health per-feed aggregates. 5 files changed, 873 insertions, 17 deletions |
| (this commit) | Wave 5 end-state + spec marked complete |

## Deviations from the plan

- **OTel trace ingester skipped.** The plan's Task 5.3 expected to wire
  ``akosha/ingestion/otel_ingester.py``, but that file does not exist.
  Trace data only enters the system via the existing
  ``BodaiToolInvocationSubscriber`` Redis XREADGROUP path. Building a
  from-scratch OTel collector/ingester pipeline needs an OTel HTTP
  source contract — deferred to a future hardening wave. Documented
  in spec Appendix A finding A.4 and below in followups.

- **Plan targeted the wrong file.** Task 5.2 said "modify
  ``akosha/main.py``", but the standalone ``AkoshaApplication`` class
  is not on the MCP request path. The actual boot lifecycle for the
  user-facing MCP tools is the ``lifespan`` function in
  ``akosha/mcp/server.py``. Investigation finding A.1 documented
  this; the wiring landed in ``server.py`` instead.

- **Shared-singleton pattern added.** Not in the plan but required to
  close the gap. The default backend is ``duckdb-memory`` which
  produces fresh per-process databases; without sharing the lifespan's
  ``hot_store``, the CodeGraphIngester's writes are invisible to the
  tool handlers. Pattern matches the existing
  ``get_embedding_service()`` singleton that's already in place.

- **No version bump in this wave.** Per user direction on 2026-09-05:
  "don't bump version. we will do that through cj". The version
  sync (``APP_VERSION``, ``SERVICE_VERSION``, README, pyproject,
  ``__version__``) will be handled by a crackerjack run, not by a
  hand-edit. 4 ``test_version_sync.py`` failures are pre-existing
  (Wave 4 missed the ``APP_VERSION`` / ``SERVICE_VERSION``
  constants); they will resolve when cj bumps the version uniformly.

## Followups

### Closed

- [x] **Replace remaining ``assert True`` cases with meaningful assertions.**
      W4.5 closed 7; the 2026-09-06 followup sweep (commit ``b9c0d84``)
      closed the remaining 8 across 4 files: ``test_mcp_akosha_tools_simple.py``,
      ``test_mcp_health_tools.py``, ``test_security_logging.py``,
      ``test_shell.py``. Each replacement asserts a file-specific
      post-condition (AttributeError contract, return-None contract,
      elapsed-time budget, label-dict pins, exit-code assertion).

- [x] **Default profile = full in dev.** Investigation showed
      ``ToolProfile.from_env('AKOSHA_TOOL_PROFILE')`` already returns
      ``ToolProfile.FULL`` when the env var is unset — the
      ``standard``-as-default claim in the original Wave 5 spec was
      wrong. The actual fix was the misleading static hint string in
      ``discover_tools`` (commit ``17499d7``); replaced with three
      profile-aware variants (full/standard/minimal).

### Open

- [ ] **OTel trace ingester.** Build ``akosha/ingestion/otel_ingester.py``
      that polls an OTel collector HTTP endpoint and writes spans to
      ``hot_store``. Needed for ``query_local_traces`` to return data
      independent of the BodaiToolInvocationSubscriber's Redis feed.
      Out of scope for Wave 5; needs an architectural design pass with
      an OTel HTTP source contract. Brainstorm session pending.

- [ ] **Crackerjack version bump to 0.15.0.** Will resolve the 4
      pre-existing ``test_version_sync.py`` failures and the
      ``APP_VERSION`` / ``SERVICE_VERSION`` drift from Wave 4.
      User-deferred to a crackerjack run per the 2026-09-05
      "don't bump version. we will do that through cj" directive.

- [ ] **Live MCP server smoke test in CI.** The marker-based
      ``_run()`` approach works for the smoke test but a true
      end-to-end test would spin up the server, wait 60+ seconds for
      the CodeGraphIngester's first poll, and assert
      ``hot_store.list_code_graphs()`` returns ingested data from a
      mock Session-Buddy. Out of scope for Wave 5 (needs a mock
      Session-Buddy MCP server). Brainstorm session pending; the mock
      MCP server is its own architectural surface.
