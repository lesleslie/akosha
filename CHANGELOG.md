# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
