# Akosha Admin Shell

`akosha.shell.adapter.AkoshaShell` extends `oneiric.shell.AdminShell`
with Akosha-specific helpers for distributed-intelligence inspection.

## Alpha commands (gated)

The 5 commands below are alpha-quality and **disabled by default**:
`aggregate`, `search`, `detect`, `graph`, `trends`.

To enable, set `AikoshaConfig.alpha_shell_commands_enabled: bool = True`
(or via `AKOSHA_ALPHA_SHELL_COMMANDS_ENABLED=true` env var). When the
flag is false, akosha shell prints a one-line banner on startup and
omits the 5 commands from the IPython namespace.

## Always-available helpers

- `adapters()` — list Akosha adapter names (vector_db, graph_db,
  analytics, alerting).
- `version()` — Akosha component version from package metadata.

## Cross-component admin shells

AkoshaShell shares the `AdminShell` base class with the 5 other
Core 7 admin shells. See `oneiric/docs/ONEIRIC_ADMIN_SHELL.md` for
the canonical base-class doc and the full subclass layout.