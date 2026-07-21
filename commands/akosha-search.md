______________________________________________________________________

## description: Search memories across Bodai systems using Akosha semantic search.

# /akosha:search

Search indexed memories across Bodai systems and return the most semantically relevant results.

## Usage

```
/akosha:search
```

## What This Command Does

1. **Collects the query** — identifies the concept, topic, or prior work to find.
1. **Searches system memories** — retrieves ranked matches from Akosha's cross-system index.
1. **Summarizes results** — presents the most relevant findings with their source context.

## Technical Implementation

This command uses the `mcp__akosha__search_all_systems` MCP tool which:

- performs semantic similarity search across indexed system memories
- supports result limits, similarity thresholds, and optional system filtering

## When to Use

- finding prior decisions, investigations, or implementation context
- discovering related knowledge stored by other Bodai components
