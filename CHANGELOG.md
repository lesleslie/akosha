# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.12.2] - 2026-08-28

### Changed

- akosha: Use project_root= instead of path= in load_settings()

## [0.12.1] - 2026-08-28

### Added

- akosha: Adopt BodaiCLIBase + real doctor/health (Phase 3 Task 4.4)
- akosha: Bodai.apps entry-point (Phase 5.1)

### Fixed

- akosha: Anchor load_settings() path at package install location
- akosha: Anchor load_settings() project_root at package install location

### Documentation

- readme: Bump Python badge from 3.13+ to 3.14+

### Internal

- build: Standardize akosha on hatchling backend
- deps: Bump oneiric floor to >=0.19.1

## [0.12.0] - 2026-08-24

### Internal

- akosha: Uv python pin 3.14
- Bump requires-python to >=3.14
- claude-md: Add oneiric action-kit discovery breadcrumb

## [0.11.0] - 2026-08-22

### Added

- w3: Adopt workflow.audit + data.sanitize action kits

### Fixed

- akosha: Migrate embeddings.py to delegate to oneiric
- mcp: Wire services through register_akosha_group + initialize hot store
- w3: Close case-sensitive redaction gap for HTTP headers

## [0.10.0] - 2026-08-21

### Added

- akosha: Apply ToolProfile dispatch via mcp-common 0.18.0

### Fixed

- akosha: Mcp_server_lifespan + changepoint_analytics + graceful_shutdown — 22 tests
- akosha: Sync port defaults (2026-08-19)
- akosha: Sync version stamps (2026-08-19)
- akosha: Test_mcp_tool_inventory.py — 3 tests
- akosha: Wire fitness Dhara populate + profile subsets + W0 schema
- docs+code(akosha): fix MCP-tool-hallucination audit findings (2026-08-19)

### Documentation

- akosha: Fix documented-but-not-wired audit findings (2026-08-19)

### Testing

- akosha: Add doc-drift CI guard (2026-08-19)

## [0.9.5] - 2026-08-17

### Added

- akosha: Mirror wave-11 mermaid CI guard from crackerjack

### Fixed

- akosha: Align CLAUDE.md Core Components with actual files
- akosha: Annotate L1 cache as in-process DuckDB in query flow
- akosha: Delete dead sentence-transformers runtime from EmbeddingService
- akosha: Mark real-embedding row as not-used in this process
- akosha: Remove dead embeddings install path from README
- akosha: Remove Redis L2 sidecar from K8s deployment diagram
- akosha: Rename EMB participant to EmbeddingService (mock)
- akosha: Update embedding table to reflect mock-only EmbeddingService
- Rename wave-11 mirror references from 'crackerjack' to 'akosha'

### Documentation

- Add SB push path to Session-Buddy → Akosha ASCII flow
- akosha: Add Contract 5.2 note to Phase 0 sequence diagram
- akosha: Fix 3 README contradictions from wave-1 verifier
- audit: Apply 2026-08-12 drift fixes

### Internal

- gitignore: Add .coverage\* + untrack .coverage-ratchet.json (bodai 2026-08-17)
- gitignore: Ignore docs/archive/test-artifacts/, untrack coverage dumps

## [0.9.4] - 2026-08-12

### Internal

- Adopt coverage-ratchet at 87.62% (baseline)

## [0.9.3] - 2026-08-11

### Fixed

- akosha: Unblock test suite + layered config precedence

## [0.9.2] - 2026-07-27

### Fixed

- storage: Add missing datetime import for pydantic annotations

### Documentation

- readme: Add Bodai Ecosystem Role section

### Internal

- Bump oneiric dep to >=0.16.0
- deps: Bump crackerjack>=0.70.0; remove duplicated validator script
- Normalize LICENSE attribution to Robert Leslie and Wedgwood Web Works
- pyproject: Add [project.scripts] entry for akosha CLI

## [0.9.1] - 2026-07-21

### Added

- Initial akosha plugin manifest + starter commands

### Fixed

- akosha: Move datetime import out of TYPE_CHECKING (Pydantic v2 forward-ref resolution)

### Documentation

- akosha: Apply plan-lifecycle-unification playbook (P7.B)
- plans: Reconcile stale-done items and module-rename drift
- plans: Reconcile stale-done items and module-rename drift
- plans: Reconcile stale-done items and module-rename drift
- plans: Reconcile stale-done items and module-rename drift
- plans: Reconcile stale-done items and module-rename drift
- plans: Tick shipped checkboxes in akosha eventbridge-publisher

### Internal

- akosha: Remove LICENSE (consolidated to root-level LICENSE)
- akosha: Sync uv.lock to pyproject.toml (0.9.0)

## [0.9.0] - 2026-07-14

### Added

- Add EventBridgeConfig Pydantic model
- Add EventBridgePublisher adapter
- eventbridge: Add Akosha analytics-event publisher
- Expose publish_to_eventbridge MCP tool
- settings: Add eventbridge block to akosha.yaml
- Wire EventBridgePublisher at akosha app startup
- Wire publish\_\* into AkoshaWebSocketServer.broadcast\_\*

### Changed

- settings: Migrate AkoshaConfig to OneiricMCPConfig

### Fixed

- mcp: Re-read eventbridge.enabled per call instead of closure capture
- mcp: Return no_publisher status when publisher unwired

### Testing

- eventbridge: Add end-to-end round-trip integration tests
- eventbridge: Drop brittle private-attr assertion in Akosha adapter test
- eventbridge: Fix mid-flight coroutine test to actually drive failure path
- eventbridge: Real Oneiric transport round-trip integration tests
- eventbridge: Resolve ty complaints in Akosha unit tests

### Internal

- lint: Fix ruff complaints introduced by eventbridge module

## [0.8.4] - 2026-07-05

### Fixed

- mcp: Gate analytics tools when service is None + add changepoint analytics

### Internal

- akosha: Migrate [project.optional-dependencies] → [dependency-groups]
- gitignore: Untrack .lycheecache + add \*.backup.json rule

## [0.8.3] - 2026-06-15

### Internal

- gitignore: Add backup file patterns to silence checkpoint tool artifacts
- Untrack and delete 62 historical *.backup/*.bak files

## [0.7.0] - 2026-05-31

### Changed

- Akosha (quality: 66/100) - 2026-05-31 03:53:44

## [0.4.2] - 2026-05-02

### Added

- Delegate MCP auth to mcp_common.auth, keep MCPAuthError backward compat

### Fixed

- Address code quality issues in Akosha MCP auth wrapper
- auth: Remove \_reset_config from __all__ — private helpers not exported

## [0.4.1] - 2026-04-14

### Internal

- repo: Ignore coverage artifacts

## [0.4.0] - 2026-04-03

### Changed

- Update config, core, deps, docs
- Update configuration

### Internal

- Bump version to 0.3.2
- Bump version to 0.3.3

## [0.3.2] - 2026-04-03

### Added

- Add health check tools using mcp-common
- Add PyCharm MCP tools for cross-repo code analysis

### Changed

- Update core, deps

### Internal

- Add archive/backup directories to gitignore
- Update LICENSE copyright to 2026
- Update mcp-common to 0.9.5

## [0.3.1] - 2026-02-17

### Fixed

- **BREAKING:** Default AUTH_ENABLED to false and clean git cache

### Internal

- Remove remaining oneiric_cache file from git

## [0.3.0] - 2026-02-12

### Added

- Add JWT authentication to Akosha WebSocket
- Add TLS/WSS support to Akosha WebSocket server

### Changed

- Update config, core, deps, docs
