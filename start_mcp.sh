#!/bin/bash
# Akosha MCP Server Startup Script
# This script starts the Akosha MCP server with authentication disabled for local development

set -e

# Change to script directory
cd "$(dirname "$0")"

# Disable authentication for local development
export AUTH_ENABLED=false

# Activate virtual environment
source .venv/bin/activate

# Start the MCP server
echo "Starting Akosha MCP server..."
python -m akosha.mcp
