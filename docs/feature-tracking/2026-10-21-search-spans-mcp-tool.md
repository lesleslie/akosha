---
built: 2026-10-21
wired: 2026-10-21
adopted: null
phase: bodai-otel-substrate-read-path
topic: substrate-otel
plan: docs/plans/2026-10-20-bodai-otel-substrate-read-path-implementation.md
---

# `search_spans` MCP tool (read-path R-2)

## What

`akosha/mcp/tools/otel_span_tools.py` exposes R-1's
`SpanQuery.search` over MCP as `search_spans`. It is the browse surface for
`spans_substrate` — no aggregate (that is R-3's `spans_summary`, same
registrar).

Four things happen at this boundary that R-1 deliberately does not do:

1. **Shared store, not a second one.** `search_spans` reads the lifespan's
   `HotStore` via `get_shared_hot_store()` and never constructs one. The
   registration wrapper in `group_registers.py` deliberately does *not* call
   `_try_create_hot_store()` — that helper falls back to constructing a store
   when the lifespan has not published one, which is exactly the second
   connection spec §4.1 forbids.
2. **Empty is not an error.** No store, or a store whose `spans_substrate`
   table does not exist yet, returns `[]`. The receiver owns the DDL
   (v7 PR-A2, not built), so "not started" and "nothing matched" are the same
   observable state.
3. **Bounded pages.** `limit` clamps to `[1, 500]`.
4. **Cursor wire format.** The keyset cursor `(received_at_unix_nano,
   span_id)` crosses MCP as `"<int>:<span_id>"`, split on the *first* colon —
   the integer side is colon-free, the `span_id` is opaque. A malformed cursor
   raises rather than degrading to page one, which would loop forever.

## Lifecycle

`built` is done and verified: 20 tests in
`tests/unit/mcp/test_search_spans_tool.py`, real `FastMCP` rather than a stub,
plus R-1's 10 in `tests/unit/storage/test_span_query.py`.

`wired` landed in the same change, via the four `profiles.py` surfaces:

- `FULL_REGISTRATIONS` — `"register_otel_span_tools"`
- `REGISTRATION_DESCRIPTIONS` — describes the group
- `REGISTRATION_TOOLS` — `["search_spans"]`
- `REGISTRATION_MAP` — imports and maps `register_otel_span_group`

Note the wiring surface is *not* `akosha/mcp/server.py`. The plan's sketch said
to call the registrar "alongside the existing `register_*_tools`"; grepping
`server.py` for `register_.*_tools(` returns nothing — Akosha dispatches
through `PROFILE_REGISTRATIONS` → `REGISTRATION_MAP` → `group_registers`, and a
`server.py` call site would have registered nothing while importing cleanly.

Three guard tests caught the README/count drift that follows from registering a
tool — `test_mcp_tool_inventory.py` (twice) and `test_mcp_tools_profiles.py`.
FULL profile is now 32 tools.

## Integration Contract (R-2)

- **Triggered from**: any MCP client of akosha, at the FULL profile tier.
- **Returns to / updates**: `akosha/mcp/tools/otel_span_tools.py` (new),
  `akosha/mcp/tools/group_registers.py`, `akosha/mcp/tools/profiles.py`,
  `README.md`, `tests/_support/spans_ddl.py` (new).
- **Demonstrable by**: `pytest tests/unit/mcp/test_search_spans_tool.py -v`;
  a live `search_spans` over the canary's rows returns one hit.
- **Rollback signal**: `test_no_store_returns_empty_not_error` raises → §5.2's
  empty contract was inverted into an error, which pages on a healthy service.
- **Observability added**: none. §5.3's latency budget stays unverified until a
  histogram exists (spec §7 Q7) — open, not closed.