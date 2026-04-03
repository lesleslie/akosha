# Repository Guidelines

## Project Structure & Module Organization

- `akosha/` contains the main package, with storage backends in `storage/`, ingestion logic in `ingestion/`, processing services in `processing/`, query execution in `query/`, caching in `cache/`, and API or MCP surfaces in `api/` and related entrypoints.
- Keep tiered storage concerns separated: hot, warm, and cold path logic should stay in their respective modules instead of accumulating in shared utility files.
- Tests live in `tests/` and should mirror the package structure across unit, integration, and performance scenarios.
- Long-form architecture and operator guidance belong in `README.md`, `CLAUDE.md`, and focused docs under `docs/` as features mature.

## Build, Test, and Development Commands

- `uv sync --group dev` installs development and runtime dependencies.
- `uv run pytest` runs the full suite; `uv run pytest -m "not slow"` is the default fast iteration path.
- `uv run pytest tests/unit/` and `uv run pytest tests/integration/` split local debugging by test type.
- `uv run pytest --cov=akosha --cov-report=term-missing` checks coverage while you iterate.
- `uv run crackerjack lint`, `uv run crackerjack typecheck`, `uv run crackerjack security`, and `uv run crackerjack analyze` cover the standard quality workflow.
- `uv run python -m akosha.mcp` is the primary MCP server smoke test.

## Coding Style & Naming Conventions

- Use explicit typing, clear Pydantic or DTO-style models, and small service boundaries for ingestion, enrichment, deduplication, vector indexing, and query aggregation.
- Keep module names snake_case, classes PascalCase, and storage or processing abstractions explicit about ownership and performance assumptions.
- Preserve the existing pull-based ingestion and tiered-storage architecture instead of introducing shortcut code paths that bypass sharding, caching, or lifecycle rules.

## Testing Guidelines

- Add tests with every substantive change, especially for ingestion correctness, deduplication behavior, query ranking, and tier migration logic.
- Prefer deterministic fixtures and synthetic datasets for vector, cache, and aggregation tests so behavior remains stable across environments.
- Use performance markers only where justified and keep correctness tests separate from scale or throughput measurements.
- Review coverage output after larger changes to catch missed branches in storage movement, shard routing, and API error handling.

## Commit & Pull Request Guidelines

- Use focused commits such as `feat(query): add shard-aware reranking` or `fix(ingestion): handle duplicate upload manifests`.
- PRs should describe the affected tier or subsystem, commands run for validation, and any operator-visible behavior changes.
- Include benchmark notes, trace snippets, or example queries when changing performance-sensitive paths.

## Ecosystem Notes

- Akosha is the intelligence and memory aggregation layer in the Bodai ecosystem and commonly interacts with Session-Buddy, Mahavishnu, Oneiric, and downstream storage systems.
- Workflow scheduling belongs outside Akosha; preserve the current boundary where orchestration is handled by Mahavishnu and storage abstraction by Oneiric or underlying services.

## Security & Configuration Tips

- Never hard-code buckets, credentials, Redis endpoints, or internal service URLs.
- Validate ingestion metadata, query filters, and API inputs strictly to avoid malformed cross-system data poisoning the index or analytics layers.
