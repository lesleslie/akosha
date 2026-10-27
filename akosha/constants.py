"""Constants that must be importable without dragging in the application.

``akosha.cli`` needs ``DEFAULT_MCP_PORT`` at MODULE scope: Typer evaluates
the ``Annotated[int, typer.Option(...)] = DEFAULT_MCP_PORT`` defaults when
the command function is defined, which happens while the CLI module is
being imported.

Reaching for it via ``akosha.config`` made the CLI pay for the config
module -- and, through it, the oneiric settings chain and the storage
stack -- before Typer could parse ``argv``. This module has no imports at
all, so the constant can be reached for free.

``akosha.config`` re-exports the constant, so ``akosha.config.DEFAULT_MCP_PORT``
remains a valid import path: there is still exactly ONE definition.
"""

from __future__ import annotations

#: Single source of truth for the Akosha network port. The Bodai ecosystem
#: reserves 8682 for the Akosha seer; the previous 3002 default predates the
#: port convention. Override via AKOSHA_MCP_PORT / MCP_PORT env vars or CLI
#: flag, never by editing this constant.
DEFAULT_MCP_PORT: int = 8682
