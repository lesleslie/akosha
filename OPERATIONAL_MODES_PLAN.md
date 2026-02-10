# Akosha Operational Modes - Implementation Plan

## Context

Akosha is already quite lean compared to the initial assessment. The main external dependency is **Redis** (optional cache layer). The core storage engine (DuckDB) is embedded and requires no external services.

## Current Architecture Analysis

### External Dependencies

| Component | Status | Required? | Purpose |
|-----------|--------|-----------|---------|
| **DuckDB** | ✅ Embedded | Yes | Core storage (hot/warm tiers) |
| **Redis** | ⚠️ External | Optional | L2 cache layer |
| **Oneiric** | ✅ Library | Yes | Storage abstraction |
| **Cloud Storage** | ⚠️ External | Optional | Cold tier (S3/Azure/GCS) |

### Key Findings

1. **DuckDB is embedded** - Runs in-process, no separate service needed
2. **Redis is optional** - Used for caching, graceful degradation if unavailable
3. **Oneiric provides abstraction** - Already has adapter pattern for storage backends
4. **No vector database service** - DuckDB handles vector search with HNSW

## Mode System Design

### Mode Comparison Matrix

| Feature | Lite Mode | Standard Mode |
|---------|-----------|---------------|
| **Setup Time** | 2 min | 5 min |
| **Services** | 1 (Akosha only) | 2 (Akosha + Redis) |
| **Hot Storage** | DuckDB in-memory | DuckDB in-memory |
| **Warm Storage** | DuckDB on-disk | DuckDB on-disk |
| **Cold Storage** | Disabled (optional) | Oneiric cloud storage |
| **Cache Layer** | In-memory only | Redis L2 cache |
| **Dependencies** | DuckDB only | DuckDB + Redis |
| **Ideal For** | Development, testing | Production, scale |

### Mode Behaviors

#### Lite Mode (`--mode=lite`)
- Disable Redis cache
- Use in-memory caching only
- Disable cold storage (or use local file system)
- Simplified configuration
- Faster startup

#### Standard Mode (`--mode=standard`)
- Enable Redis cache (if available)
- Enable Oneiric cloud storage for cold tier
- Full production configuration
- Optimized for scale

## Implementation Plan

### Phase 1: Create Mode System (2 days)

#### 1.1 Create `akosha/modes/` Directory Structure

```
akosha/modes/
├── __init__.py
├── base.py          # Base mode interface
├── lite.py          # Lite mode implementation
└── standard.py      # Standard mode implementation
```

#### 1.2 Base Mode Interface (`base.py`)

```python
"""Base mode interface for Akosha operational modes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class ModeConfig(BaseModel):
    """Configuration for a specific mode."""

    name: str
    description: str
    redis_enabled: bool
    cold_storage_enabled: bool
    cache_backend: str  # "memory" or "redis"


class BaseMode(ABC):
    """Base class for operational modes."""

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize mode with configuration."""
        self.config = config
        self.mode_config = self.get_mode_config()

    @abstractmethod
    def get_mode_config(self) -> ModeConfig:
        """Get mode-specific configuration."""
        pass

    @abstractmethod
    async def initialize_cache(self) -> Any:
        """Initialize cache layer for this mode."""
        pass

    @abstractmethod
    async def initialize_cold_storage(self) -> Any:
        """Initialize cold storage for this mode."""
        pass

    @property
    @abstractmethod
    def requires_external_services(self) -> bool:
        """Check if mode requires external services."""
        pass
```

#### 1.3 Lite Mode Implementation (`lite.py`)

```python
"""Lite mode: In-memory only, no external dependencies."""

from __future__ import annotations

from typing import Any

from akosha.modes.base import BaseMode, ModeConfig


class LiteMode(BaseMode):
    """Lite mode with zero external dependencies."""

    def get_mode_config(self) -> ModeConfig:
        """Get lite mode configuration."""
        return ModeConfig(
            name="lite",
            description="Lite mode: In-memory only, zero dependencies",
            redis_enabled=False,
            cold_storage_enabled=False,
            cache_backend="memory",
        )

    async def initialize_cache(self) -> Any:
        """Initialize in-memory cache (no Redis)."""
        # Return None or simple in-memory cache
        return None

    async def initialize_cold_storage(self) -> Any:
        """Initialize cold storage (disabled in lite mode)."""
        # Return None - cold storage disabled
        return None

    @property
    def requires_external_services(self) -> bool:
        """Lite mode requires no external services."""
        return False
```

#### 1.4 Standard Mode Implementation (`standard.py`)

```python
"""Standard mode: Full production configuration with Redis and cloud storage."""

from __future__ import annotations

import logging

from akosha.modes.base import BaseMode, ModeConfig

logger = logging.getLogger(__name__)


class StandardMode(BaseMode):
    """Standard mode with full production features."""

    def get_mode_config(self) -> ModeConfig:
        """Get standard mode configuration."""
        return ModeConfig(
            name="standard",
            description="Standard mode: Full production configuration",
            redis_enabled=True,
            cold_storage_enabled=True,
            cache_backend="redis",
        )

    async def initialize_cache(self) -> Any:
        """Initialize Redis cache (with graceful fallback)."""
        try:
            # Try to connect to Redis
            import redis

            redis_client = redis.Redis(
                host=self.config.get("redis_host", "localhost"),
                port=self.config.get("redis_port", 6379),
                db=self.config.get("redis_db", 0),
                decode_responses=True,
            )
            redis_client.ping()
            logger.info("Redis cache initialized successfully")
            return redis_client
        except Exception as e:
            logger.warning(f"Redis unavailable, using in-memory cache: {e}")
            return None

    async def initialize_cold_storage(self) -> Any:
        """Initialize Oneiric cold storage."""
        try:
            # Initialize Oneiric storage adapter
            from oneiric.adapters import StorageAdapter

            cold_storage = await StorageAdapter.create(
                backend=self.config.get("cold_storage_backend", "s3"),
                bucket=self.config.get("cold_bucket", "akosha-cold-data"),
            )
            logger.info("Cold storage initialized successfully")
            return cold_storage
        except Exception as e:
            logger.warning(f"Cold storage unavailable: {e}")
            return None

    @property
    def requires_external_services(self) -> bool:
        """Standard mode requires external services."""
        return True
```

### Phase 2: Create Configuration Files (1 day)

#### 2.1 Create `config/lite.yaml`

```yaml
# Akosha Lite Mode Configuration
# Minimal dependencies, in-memory only

mode: lite

# Storage
storage:
  hot:
    backend: duckdb-memory
    path: ":memory:"

  warm:
    backend: duckdb-ssd
    path: "/tmp/akosha/warm"

  cold:
    enabled: false
    backend: null

# Cache
cache:
  backend: memory  # No Redis
  ttl_seconds: 60

# API
api:
  port: 8682
  mcp_port: 3002

# Processing
processing:
  ingestion_workers: 1
  max_concurrent_ingests: 10
```

#### 2.2 Create `config/standard.yaml`

```yaml
# Akosha Standard Mode Configuration
# Full production configuration with Redis and cloud storage

mode: standard

# Storage
storage:
  hot:
    backend: duckdb-memory
    path: ":memory:"

  warm:
    backend: duckdb-ssd
    path: "/data/akosha/warm"

  cold:
    enabled: true
    backend: s3  # or azure, gcs
    bucket: akosha-cold-data
    prefix: "conversations/"

# Cache
cache:
  backend: redis
  host: localhost
  port: 6379
  db: 0
  local_ttl_seconds: 60
  redis_ttl_seconds: 3600

# API
api:
  port: 8682
  mcp_port: 3002

# Processing
processing:
  ingestion_workers: 3
  max_concurrent_ingests: 100
```

### Phase 3: CLI Integration (1 day)

#### 3.1 Update `akosha/cli.py`

Add mode parameter to `start` command:

```python
@app.command()
def start(
    host: Annotated[str, typer.Option("--host", "-h", help="Host to bind to")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="Port to bind to")] = 8682,
    mode: Annotated[str, typer.Option("--mode", "-m", help="Operational mode (lite|standard)")] = "lite",
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable verbose output")] = False,
) -> None:
    """Start Akosha MCP server in the specified mode."""
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    logger.info(f"Starting Akosha MCP server in {mode} mode on {host}:{port}")

    # Import the MCP app
    from akosha.mcp import create_app
    from akosha.modes import get_mode

    # Initialize mode
    mode_instance = get_mode(mode, config={})
    asyncio.run(mode_instance.initialize())

    # Create and run the MCP server
    app = create_app(mode=mode_instance)
    app.run(transport="streamable-http", host=host, port=port, path="/mcp")
```

#### 3.2 Update `akosha/main.py`

Add mode support to application initialization:

```python
class AkoshaApplication:
    """Akosha application with lifecycle management."""

    def __init__(self, mode: str = "lite") -> None:
        """Initialize application with specified mode."""
        self.shutdown_event = asyncio.Event()
        self.ingestion_workers: list[Any] = []
        self.mode = mode

        # Initialize mode-specific components
        from akosha.modes import get_mode

        self.mode_instance = get_mode(mode, config={})
```

### Phase 4: Create Startup Script (1 day)

#### 4.1 Create `scripts/dev-start.sh`

```bash
#!/usr/bin/env bash
# Akosha development startup script

set -euo pipefail

MODE=${1:-lite}

case $MODE in
  lite)
    echo "🚀 Starting Akosha in LITE mode..."
    echo "✓ No external services required"
    echo "✓ In-memory storage only"
    exec uv run akosha start --mode=lite
    ;;

  standard)
    echo "🚀 Starting Akosha in STANDARD mode..."
    echo "✓ Checking Redis connection..."

    if ! redis-cli ping >/dev/null 2>&1; then
      echo "⚠️  WARNING: Redis not available"
      echo "   Start with: docker run -d -p 6379:6379 redis"
      echo "   Falling back to in-memory cache..."
    fi

    echo "✓ Starting with full configuration"
    exec uv run akosha start --mode=standard
    ;;

  *)
    echo "❌ Invalid mode: $MODE"
    echo "   Valid modes: lite, standard"
    exit 1
    ;;
esac
```

#### 4.2 Make script executable

```bash
chmod +x scripts/dev-start.sh
```

### Phase 5: Documentation (1 day)

#### 5.1 Create `docs/guides/operational-modes.md`

```markdown
# Akosha Operational Modes

Akosha supports two operational modes to balance ease of development with production scalability.

## Lite Mode

**Best for:** Development, testing, local experimentation

### Characteristics

- ✅ Zero external dependencies
- ✅ 2-minute setup time
- ✅ In-memory storage only
- ⚠️ Data lost on restart
- ⚠️ Limited scalability

### Quick Start

```bash
# Start in lite mode (default)
akosha start --mode=lite

# Or use the shortcut script
./scripts/dev-start.sh lite
```

### Configuration

Lite mode uses `config/lite.yaml`:

```yaml
mode: lite
cache:
  backend: memory
storage:
  cold:
    enabled: false
```

## Standard Mode

**Best for:** Production, scaling, persistent storage

### Characteristics

- ✅ Redis caching layer
- ✅ Cloud storage for cold tier
- ✅ Production-ready scalability
- ⚠️ Requires external services

### Quick Start

```bash
# Start Redis (if not running)
docker run -d -p 6379:6379 redis

# Start Akosha in standard mode
akosha start --mode=standard

# Or use the shortcut script
./scripts/dev-start.sh standard
```

### Configuration

Standard mode uses `config/standard.yaml`:

```yaml
mode: standard
cache:
  backend: redis
  host: localhost
  port: 6379
storage:
  cold:
    enabled: true
    backend: s3
```

## Mode Comparison

| Feature | Lite | Standard |
|---------|------|----------|
| **Setup Time** | 2 min | 5 min |
| **Services** | 1 (Akosha) | 2 (Akosha + Redis) |
| **Cache** | In-memory | Redis + in-memory |
| **Cold Storage** | Disabled | S3/Azure/GCS |
| **Data Persistence** | No | Yes |
| **Scalability** | Limited | High |

## Migration Guide

### Lite → Standard

1. **Install Redis**:
   ```bash
   docker run -d -p 6379:6379 --name redis redis:alpine
   ```

2. **Configure cloud storage** (optional):
   ```bash
   export AWS_S3_BUCKET=akosha-cold-data
   export AWS_S3_REGION=us-west-2
   ```

3. **Start in standard mode**:
   ```bash
   akosha start --mode=standard
   ```

### Standard → Lite

Simply change the mode flag:

```bash
akosha start --mode=lite
```

Note: Data in Redis and cloud storage will not be accessible in lite mode.

## Troubleshooting

### Lite Mode Issues

**Problem**: Data lost on restart

**Solution**: Use standard mode for persistent storage

**Problem**: Out of memory errors

**Solution**: Reduce data volume or switch to standard mode

### Standard Mode Issues

**Problem**: Redis connection refused

**Solution**: Start Redis or fall back to lite mode

```bash
# Check Redis status
redis-cli ping

# Start Redis if needed
docker start redis
```

**Problem**: Cloud storage permissions error

**Solution**: Verify credentials and bucket permissions

```bash
# Check AWS credentials
aws sts get-caller-identity

# Verify bucket access
aws s3 ls s3://akosha-cold-data
```

## Best Practices

### Development Workflow

1. **Start with lite mode** for rapid prototyping
2. **Switch to standard mode** when you need persistence
3. **Use environment variables** for mode-specific config

```bash
# Development
export AKOSHA_MODE=lite
akosha start

# Production
export AKOSHA_MODE=standard
akosha start
```

### Production Deployment

For production, always use **standard mode** with:

- Redis cluster for high availability
- Cloud storage for cold tier
- Environment-based configuration
- Health checks and monitoring

```yaml
# config/production.yaml
mode: standard
cache:
  backend: redis
  host: redis.production.internal
  port: 6379
storage:
  cold:
    enabled: true
    backend: s3
    bucket: akosha-production-data
```
```

## Success Criteria

- ✅ Lite mode works with zero external dependencies
- ✅ Standard mode enables Redis and cloud storage
- ✅ CLI integration complete with `--mode` flag
- ✅ Startup script created (`scripts/dev-start.sh`)
- ✅ Documentation complete
- ✅ Graceful degradation when services unavailable
- ✅ All tests pass in both modes

## Testing Strategy

### Unit Tests

```python
# tests/unit/test_modes.py
import pytest
from akosha.modes import get_mode, LiteMode, StandardMode

def test_lite_mode_config():
    """Test lite mode configuration."""
    mode = get_mode("lite", config={})
    assert mode.mode_config.name == "lite"
    assert not mode.mode_config.redis_enabled
    assert not mode.requires_external_services

def test_standard_mode_config():
    """Test standard mode configuration."""
    mode = get_mode("standard", config={})
    assert mode.mode_config.name == "standard"
    assert mode.mode_config.redis_enabled
    assert mode.requires_external_services

@pytest.mark.asyncio
async def test_lite_mode_no_redis():
    """Test that lite mode doesn't require Redis."""
    mode = LiteMode(config={})
    cache = await mode.initialize_cache()
    assert cache is None

@pytest.mark.asyncio
async def test_standard_mode_redis_fallback():
    """Test standard mode graceful fallback."""
    mode = StandardMode(config={"redis_host": "invalid"})
    cache = await mode.initialize_cache()
    # Should return None but not raise exception
    assert cache is None
```

### Integration Tests

```python
# tests/integration/test_mode_integration.py
import pytest
from akosha.main import AkoshaApplication

@pytest.mark.asyncio
async def test_lite_mode_startup():
    """Test lite mode startup without external services."""
    app = AkoshaApplication(mode="lite")
    await app.start()
    # Verify no external service connections
    await app.stop()

@pytest.mark.asyncio
@pytest.mark.integration
async def test_standard_mode_startup():
    """Test standard mode startup with Redis."""
    app = AkoshaApplication(mode="standard")
    await app.start()
    # Verify Redis connection established
    await app.stop()
```

## Timeline

| Phase | Duration | Deliverables |
|-------|----------|--------------|
| **Phase 1** | 2 days | Mode system (base, lite, standard) |
| **Phase 2** | 1 day | Configuration files (lite.yaml, standard.yaml) |
| **Phase 3** | 1 day | CLI integration (--mode flag) |
| **Phase 4** | 1 day | Startup script (dev-start.sh) |
| **Phase 5** | 1 day | Documentation (operational-modes.md) |
| **Total** | 6 days | Complete operational modes feature |

## Next Steps

1. Review and approve this plan
2. Create implementation branch
3. Execute Phase 1 (Mode System)
4. Continue through remaining phases
5. Test thoroughly in both modes
6. Update main README with mode information
