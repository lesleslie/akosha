#!/usr/bin/env bash
# Akosha development startup script
# Launches Akosha in the specified operational mode

set -euo pipefail

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_DIR="$( cd "$SCRIPT_DIR/.." && pwd )"

# Change to project directory
cd "$PROJECT_DIR"

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

MODE=${1:-lite}

case $MODE in
  lite)
    echo -e "${BLUE}🚀 Starting Akosha in LITE mode${NC}"
    echo ""
    echo -e "${GREEN}✓ Configuration:${NC}"
    echo "  - No external services required"
    echo "  - In-memory storage only"
    echo "  - Fastest startup time"
    echo "  - Ideal for development"
    echo ""
    echo -e "${YELLOW}⚠️  Data Persistence:${NC}"
    echo "  - All data lost on restart"
    echo "  - Use standard mode for persistence"
    echo ""
    echo -e "${BLUE}Starting...${NC}"
    exec uv run akosha start --mode=lite
    ;;

  standard)
    echo -e "${BLUE}🚀 Starting Akosha in STANDARD mode${NC}"
    echo ""

    # Check Redis availability
    echo -e "${GREEN}✓ Checking services...${NC}"

    if command -v redis-cli &> /dev/null; then
      if redis-cli ping >/dev/null 2>&1; then
        echo -e "  ${GREEN}✓ Redis available${NC}"
      else
        echo -e "  ${YELLOW}⚠️  Redis not responding${NC}"
        echo "     Start with: docker run -d -p 6379:6379 --name redis redis:alpine"
        echo "     Falling back to in-memory cache..."
      fi
    else
      echo -e "  ${YELLOW}⚠️  redis-cli not found${NC}"
      echo "     Install with: brew install redis  (macOS)"
      echo "     Falling back to in-memory cache..."
    fi

    # Check cloud storage configuration
    if [[ -n "${AWS_S3_BUCKET:-}" ]] || [[ -n "${AKOSHA_COLD_BUCKET:-}" ]]; then
      echo -e "  ${GREEN}✓ Cloud storage configured${NC}"
      echo "     Bucket: ${AWS_S3_BUCKET:-${AKOSHA_COLD_BUCKET:-}}"
    else
      echo -e "  ${YELLOW}⚠️  Cloud storage not configured${NC}"
      echo "     Set AWS_S3_BUCKET or AKOSHA_COLD_BUCKET for cold storage"
      echo "     Continuing without cold storage..."
    fi

    echo ""
    echo -e "${GREEN}✓ Configuration:${NC}"
    echo "  - Redis caching layer"
    echo "  - Cloud storage for cold tier"
    echo "  - Production-ready scalability"
    echo ""
    echo -e "${BLUE}Starting...${NC}"
    exec uv run akosha start --mode=standard
    ;;

  *)
    echo -e "${RED}❌ Invalid mode: $MODE${NC}"
    echo ""
    echo "Valid modes:"
    echo "  - lite: Zero dependencies, in-memory only"
    echo "  - standard: Full production with Redis and cloud storage"
    echo ""
    echo "Usage:"
    echo "  $0 [lite|standard]"
    echo ""
    echo "Examples:"
    echo "  $0 lite       # Start in lite mode (default)"
    echo "  $0 standard   # Start in standard mode"
    exit 1
    ;;
esac
