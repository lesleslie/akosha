"""Entry point for running Akosha as a module.

This keeps Akosha aligned with the rest of the ecosystem MCP services,
which are launched via `python -m <package> mcp start`.
"""

from akosha.cli import main


if __name__ == "__main__":
    main()
