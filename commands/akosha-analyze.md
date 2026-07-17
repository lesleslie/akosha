---
description: Analyze Akosha-indexed repository imports for structural issues and patterns.
---

# /akosha:analyze

Analyze repository imports to identify unused imports, circular dependencies, or recurring patterns.

## Usage

```
/akosha:analyze
```

## What This Command Does

1. **Selects an analysis** — determines whether to inspect unused imports, cycles, or patterns.
2. **Scopes the repository** — applies an optional repository path and result limit.
3. **Explains findings** — reports import relationships and actionable structural concerns.

## Technical Implementation

This command uses the `mcp__akosha__analyze_imports` MCP tool which:
- supports `unused`, `circular`, and `patterns` analysis modes
- can filter findings by repository path and cap the number of results

## When to Use

- investigating circular dependencies or unused imports
- reviewing import architecture before a refactor
