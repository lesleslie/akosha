---
name: search-insights
description: Use ONLY when the user explicitly types `/akosha:search-insights` or selects this Skill from the picker to run hybrid semantic + lexical search across the indexed Bodai memory corpus. Do not auto-trigger. Routes through `mcp__akosha__akosha_search_all_systems` to find semantically similar reflections across all ingested systems. Useful for questions like "what did we do last time X happened?", "find past conversations about Y", or "what does the corpus say about Z?"
allowed-tools: mcp__akosha__akosha_search_all_systems, Read, Bash(echo:*)
---

# search-insights

## When to use

This Skill is the right entry point when the user is asking a question
whose answer exists somewhere in the indexed Bodai memory corpus —
across all systems ingested by Akosha's HotStore. Common cases:

- "What did we do last time we hit a path-traversal regression?"
- "Find past conversations about FastMCP lifespan wiring."
- "Search the corpus for anything mentioning `mcp_common`."
- "Are there any reflections on Phase 1.5 signer issues?"

The query is a natural-language question. The Skill will:

1. Take the user's query verbatim.
2. Call `mcp__akosha__akosha_search_all_systems` with `query=<the text>`,
   `limit=10`, `threshold=0.7`.
3. Read back the top results.
4. Synthesize an answer with citations to the source
   `system_id:conversation_id` pairs.

## What the tool does

`mcp__akosha__akosha_search_all_systems` performs **hybrid semantic +
lexical** retrieval:

- **Semantic**: encodes the query with the local all-MiniLM-L6-v2
  embedding model (384-dimensional) and runs a vector similarity
  search against the HotStore corpus.
- **Lexical**: the same call also tokenizes the query and ranks by
  token overlap. The two signals are fused before ranking.

The query MUST be 1-500 characters. Empty queries are rejected.
`limit` is clamped to 1-100; `threshold` is a 0.0-1.0 similarity floor.

## When NOT to use

- For raw keyword search across one specific system, prefer
  `mcp__akosha__akosha_query_knowledge_graph` (entity-keyed) instead.
- For OTel trace queries, use `mcp__akosha__akosha_query_local_traces`.
- For code-pattern search, use `mcp__akosha__akosha_search_code_patterns`.

## Failure modes and how to handle them

- **No results**: `mode` will be `"fallback"` and you'll see a single
  informational row. Surface this to the user; do not hallucinate
  content.
- **Embedding service down**: same fallback row. Surface the
  degraded mode.
- **Rate limited / auth failed**: `PermissionError` raised; surface
  the error verbatim.

## Example flow

User: "What did we do last time crackerjack broke on httpx2 migration?"

Skill action:

```python
result = await mcp__akosha__akosha_search_all_systems(
    query="crackerjack httpx2 migration regression",
    limit=10,
    threshold=0.7,
)
for r in result["results"]:
    print(r["system_id"], r["conversation_id"], r["content"][:200])
```

Synthesize the answer with citations to the top 3 results.
