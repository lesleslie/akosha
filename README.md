# Akosha

[![Code style: crackerjack](https://img.shields.io/badge/code%20style-crackerjack-000042)](https://github.com/lesleslie/crackerjack)
[![Runtime: oneiric](https://img.shields.io/badge/runtime-oneiric-6e5494)](https://github.com/lesleslie/oneiric)
[![Framework: FastMCP](https://img.shields.io/badge/framework-FastMCP-0ea5e9)](https://github.com/jlowin/fastmcp)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![Python: 3.13+](https://img.shields.io/badge/python-3.13%2B-green)](https://www.python.org/downloads/)

Universal memory aggregation and cross-system analytics for the Bodai ecosystem.

**Version:** 0.9.4
**Status:** Active pilot deployment for the current phase

## Bodai Ecosystem Role

Akosha is the **seer** of the [Bodai ecosystem](https://github.com/lesleslie/bodai) — the cross-system intelligence layer that aggregates embeddings, semantic search, and pattern detection across all other Bodai repos (Mahavishnu, Dhara, Session-Buddy, Crackerjack, Oneiric).

Standalone, Akosha is a universal memory aggregation and analytics platform — useful for any team that needs to search semantically across multiple codebases or knowledge sources. See [bodai/docs](https://github.com/lesleslie/bodai) for integration patterns.

## Quick Links

- [Overview](#what-is-akosha)
- [Quick Start](#quick-start)
- [Installation](#installation)
- [Usage Examples](#usage-examples)
- [Architecture](#architecture)
- [Development](#development)

## Quality & CI

Crackerjack is the standard quality-control and CI/CD gate for Akosha changes. Local verification should mirror the Crackerjack workflow used across the ecosystem.

______________________________________________________________________

## What is Akosha?

Akosha is a universal memory aggregation system that collects, processes, and analyzes memories from multiple Session Buddy instances. It provides:

- **Semantic Search**: Find relevant conversations across all systems using vector embeddings
- **Time-Series Analytics**: Detect trends, anomalies, and correlations
- **Knowledge Graph**: Cross-system entity relationships and path finding
- **Three-Tier Storage**: Hot (in-memory) → Warm (on-disk) → Cold (Cloudflare R2)

### Key Capabilities

✅ **Privacy-First**: Deterministic mock embeddings; real backends delegated to MCP-side providers (Ollama, OpenAI)
✅ **Scalable**: Handles 100 to 100,000+ Session-Buddy instances
✅ **Real-Time Analytics**: Trend detection, anomaly spotting, cross-system correlation
✅ **MCP Protocol**: Exposes all capabilities via Model Context Protocol
✅ **Operational Baseline**: Tests, graceful degradation, and type-safe code

______________________________________________________________________

## Quick Start

### Prerequisites

- **Python 3.13+** (required for modern type hints)
- **UV** package manager (recommended) or pip
- **DuckDB** (automatically installed)
- **Optional (serverless/production)**: PostgreSQL + pgvector extension for persistent hot-store storage across cold-starts

> **Note on embeddings**: Akosha generates deterministic mock embeddings
> in-process (see `akosha/processing/embeddings.py`); real embeddings
> are delegated to MCP-side providers (Ollama, OpenAI). The historical
> `embeddings` optional dependency group was emptied in 2026-08 when
> `onnxruntime` was dropped, so there is no native ONNX / sentence-
> transformers install path from this repo.

> **pgvector note**: If using pgvector-backed storage, your PostgreSQL instance must have the `vector` extension enabled: `CREATE EXTENSION vector;`. See [Deployment Guide](docs/DEPLOYMENT_GUIDE.md) for full serverless setup instructions.

### 5-Minute Setup

```bash
# 1. Clone repository
git clone https://github.com/lesleslie/akosha.git
cd akosha

# 2. Install dependencies
uv sync --group dev

# 3. Start Akosha MCP server
uv run python -m akosha.mcp

# 4. Verify installation
uv run python -c "from akosha.processing.embeddings import get_embedding_service; print('✅ Akosha ready!')"
```

That's it! Akosha is now running and ready to aggregate memories.

### Production Deployment

For deployment details, operational setup, and metrics configuration:

```bash
# 1. Review deployment guide
cat docs/DEPLOYMENT_GUIDE.md

# 2. Deploy to Kubernetes
kubectl apply -f kubernetes/

# 3. Verify deployment
kubectl get pods -n akosha
kubectl port-forward -n akosha svc/akosha-api 8000:8000

# 4. Check metrics
curl http://localhost:8000/metrics
```

See [Deployment Guide](docs/DEPLOYMENT_GUIDE.md) for complete production setup.

______________________________________________________________________

## Installation

### Using UV (Recommended)

```bash
# Install all dependencies (development + production)
uv sync --group dev

# Install minimal dependencies only (production)
uv sync

# Verify installation
uv run pytest tests/unit/ -v
```

### Using Pip

```bash
# Create virtual environment
python3.13 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -e ".[dev]"

# Verify installation
pytest tests/unit/ -v
```

### Optional Dependencies

Akosha has no optional dependency groups for in-process embeddings — the
`embeddings` PEP 735 group in `pyproject.toml` is intentionally empty
(see its inline comment). Real semantic embeddings, when you need them,
are produced by MCP-side providers (Ollama, OpenAI) configured in your
Bodai deployment; this package does not ship a native ONNX / sentence-
transformers runtime.

For **persistent hot-store storage across cold-starts**:

```bash
# Serverless / production: pgvector-backed hot store
uv sync --group storage-pg
```

See the [Deployment Guide](docs/DEPLOYMENT_GUIDE.md) for the full
serverless setup instructions, including the `CREATE EXTENSION vector;`
prerequisite.

______________________________________________________________________

## Configuration

### Environment Variables

Create a `.env` file in the Akosha directory:

```bash
# Cloudflare R2 Configuration (Cold Storage)
AKOSHA_COLD_BUCKET=your-bucket-name
AKOSHA_COLD_ENDPOINT=https://your-account.r2.cloudflarestorage.com
AKOSHA_COLD_REGION=auto

# Optional: Embedding Model
AKOSHA_EMBEDDING_MODEL=all-MiniLM-L6-v2

# Optional: Storage Paths
AKOSHA_HOT_PATH=/tmp/akosha/hot
AKOSHA_WARM_PATH=/tmp/akosha/warm
```

______________________________________________________________________

## MCP Server Setup

### Global Configuration (Recommended)

Add to `~/.claude/.mcp.json`:

```json
{
  "mcpServers": {
    "akosha": {
      "command": "python",
      "args": ["-m", "akosha.mcp"],
      "cwd": "/path/to/akosha",
      "env": {
        "PYTHONPATH": "/path/to/akosha"
      }
    }
  }
}
```

> **Note**: Replace `/path/to/akosha` with the actual path to your Akosha installation.

### Project-Level Configuration (Alternative)

You can also create a project-level `.mcp.json` in the Akosha directory for development:

```bash
# Create .mcp.json in Akosha directory
cat > .mcp.json << 'EOF'
{
  "mcpServers": {
    "akosha": {
      "command": "uv",
      "args": ["run", "python", "-m", "akosha.mcp"],
      "cwd": "."
    }
  }
}
EOF
```

> **Note**: Project-level configuration is optional. Use either global or project-level config, not both.

______________________________________________________________________

## Usage Examples

### 1. Generate Semantic Embeddings

```python
from akosha.processing.embeddings import get_embedding_service

# Get singleton instance
embedding_service = get_embedding_service()
await embedding_service.initialize()

# Generate embedding
text = "How to implement JWT authentication in FastAPI"
embedding = await embedding_service.generate_embedding(text)

print(f"Embedding dimension: {len(embedding)}")  # 384
print(f"Mode: {'real' if embedding_service.is_available() else 'fallback'}")
```

### 2. Detect Trends in Metrics

```python
from akosha.processing.analytics import TimeSeriesAnalytics
from datetime import datetime, timedelta, UTC

analytics = TimeSeriesAnalytics()

# Add metric data
now = datetime.now(UTC)
for i in range(20):
    await analytics.add_metric(
        metric_name="conversation_count",
        value=100 + i * 5,  # Increasing trend
        system_id="system-1",
        timestamp=now - timedelta(hours=20-i),
    )

# Analyze trend
trend = await analytics.analyze_trend(
    metric_name="conversation_count",
    system_id="system-1",
    time_window=timedelta(days=7),
)

print(f"Trend: {trend.trend_direction}")  # "increasing"
print(f"Strength: {trend.trend_strength:.2f}")  # 0.85+
print(f"Change: {trend.percent_change:.1f}%")  # +95%
```

### 3. Detect Anomalies

```python
# Add normal data + anomalies
await analytics.add_metric("error_rate", 5.0, "system-1")
await analytics.add_metric("error_rate", 5.2, "system-1")
await analytics.add_metric("error_rate", 95.0, "system-1")  # Anomaly!
await analytics.add_metric("error_rate", 4.8, "system-1")

# Detect anomalies
anomalies = await analytics.detect_anomalies(
    metric_name="error_rate",
    system_id="system-1",
    threshold_std=2.5,
)

print(f"Found {anomalies.anomaly_count} anomalies")
for anomaly in anomalies.anomalies:
    print(f"  - Value: {anomaly['value']}, Z-score: {anomaly['z_score']:.2f}")
```

### 4. Cross-System Correlation

```python
# Add correlated data for two systems
for i in range(20):
    base_value = 50.0 + i
    await analytics.add_metric("quality_score", base_value, "system-1")
    await analytics.add_metric("quality_score", base_value + 5, "system-2")

# Analyze correlations
correlation = await analytics.correlate_systems(
    metric_name="quality_score",
    time_window=timedelta(days=7),
)

print(f"Significant correlations: {len(correlation.system_pairs)}")
for pair in correlation.system_pairs:
    print(f"  {pair['system_1']} ↔ {pair['system_2']}: {pair['correlation']:.3f}")
```

______________________________________________________________________

## CLI Reference

### Admin Shell

Launch the interactive admin shell for distributed intelligence operations:

```bash
akosha shell
```

The admin shell provides:

- **Intelligence Commands**:

  - `aggregate()` - Aggregate across systems
  - `search()` - Search distributed memory
  - `detect()` - Detect anomalies
  - `graph()` - Query knowledge graph
  - `trends()` - Analyze trends

- **Session Tracking**: Automatic tracking via Session-Buddy MCP

- **IPython Features**: Tab completion, magic commands, rich output

See [Admin Shell Documentation](docs/ADMIN_SHELL.md) for details.

### Other Commands

```bash
# Show version
akosha version

# Show system information
akosha info

# Start Akosha server
akosha start --host 0.0.0.0 --port 8000
```

## Architecture

### Three-Tier Storage

```
┌─────────────────────────────────────────────────────────┐
│                    Akosha System                         │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  Hot Store (< 7 days)                                   │
│  ├─ DuckDB in-memory                                    │
│  ├─ FLOAT[384] embeddings (full precision)             │
│  └─ Sub-second queries                                  │
│                                                          │
│  Warm Store (7-90 days)                                 │
│  ├─ DuckDB on-disk                                      │
│  ├─ INT8[384] embeddings (75% size reduction)          │
│  └─ Date-based partitioning                             │
│                                                          │
│  Cold Store (> 90 days)                                 │
│  ├─ Parquet files on Cloudflare R2                     │
│  ├─ Extractive summaries (3 sentences)                 │
│  └─ Cost-effective long-term storage                    │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

### MCP Tools (Profile-Gated Inventory)

Akosha exposes its tools via the `AKOSHA_TOOL_PROFILE` environment variable.
The list below is the **FULL profile** (25 tools), which is the default.
Profiles: **MINIMAL** (6 tools, health probes only) → **STANDARD** (14 tools,
adds core memory aggregation) → **FULL** (25 tools, adds Session-Buddy,
PyCharm, OTel, fitness, and EventBridge integrations).

Source of truth: `akosha/mcp/tools/profiles.py:48-81`
(`REGISTRATION_TOOLS`).

**Health & Dependency Probes (6):**

- `get_liveness` - Liveness check
- `get_readiness` - Readiness check
- `health_check_service` - Check a single dependency
- `health_check_all` - Check all dependencies
- `wait_for_dependency` - Block until a dependency is healthy
- `wait_for_all_dependencies` - Block until every dependency is healthy

**Core Memory Aggregation (8):**

- `generate_embedding` - Generate semantic embedding for one text
- `generate_batch_embeddings` - Batch embedding generation
- `search_all_systems` - Semantic search across systems
- `detect_anomalies` - Statistical anomaly detection
- `analyze_trends` - Time-series trend analysis (increasing/decreasing/stable)
- `correlate_systems` - Cross-system correlation analysis
- `query_knowledge_graph` - Entity and relationship queries
- `get_system_metrics` - Aggregate system metrics

**Session-Buddy Integration (2):**

- `ingest_session_memory` - Direct HTTP memory ingestion from Session-Buddy
- `get_cross_system_summary` - Cross-system memory summary

**PyCharm / IDE Integration (5):**

- `get_ide_diagnostics` - Pull file-level diagnostics from PyCharm
- `search_code` - Project-wide code search via PyCharm index
- `get_symbol_info` - Symbol metadata
- `find_usages` - Symbol usage lookup
- `pycharm_health` - PyCharm MCP connectivity

**OpenTelemetry Trace Queries (1):**

- `query_local_traces` - Query OTel traces by task class + time window

**Fitness Analyzer (2):**

- `run_fitness_analysis` - On-demand fitness signal computation
- `get_fitness_analyzer_status` - Fitness analyzer status

**EventBridge Publisher (1):**

- `publish_to_eventbridge` - Emit analytics events to the Bodai EventBridge

______________________________________________________________________

## Development

### Code Quality Standards

- **Type Hints**: Required for all functions (modern Python 3.13+ syntax)
- **Docstrings**: Google-style docstrings
- **Testing**: 85%+ code coverage required
- **Linting**: Ruff with strict settings
- **Complexity**: Maximum 15 (Ruff default)

### Running Development Commands

```bash
# Run linter
uv run ruff check akosha/

# Run type checker
uv run mypy akosha/

# Run tests
uv run pytest

# Run tests with coverage
uv run pytest --cov=akosha --cov-report=term-missing

# Run specific test file
uv run pytest tests/unit/test_embeddings.py -v
```

______________________________________________________________________

## Testing

### Current Test Results

```
tests/unit/test_embeddings.py ............ (10 passing, 4 skipped)
tests/unit/test_analytics.py ............ (14 passing)
tests/integration/test_mcp_integration.py ........ (8 passing)

Total: 32/32 passing (100% pass rate)
```

### Test Categories

- **Unit Tests** (24 tests): Core functionality testing
- **Integration Tests** (8 tests): End-to-end MCP workflows
- **Coverage**: 76-97% for Phase 2 components

______________________________________________________________________

## Roadmap

### Phase 1: Foundation

- Three-tier storage architecture
- Basic ingestion pipeline
- Knowledge graph construction
- MCP server framework

### Phase 2: Advanced Features

- Mock embedding service
- Time-series analytics
- Cross-system correlation
- 25 MCP tools integrated (FULL profile)

### Phase 3: Production Hardening

- ✅ Integration test suite (end-to-end testing)
- ✅ Load testing framework (Locust-based)
- ✅ Authentication & authorization (JWT + RBAC)
- ✅ Prometheus metrics collection
- ✅ Grafana dashboards (ingestion, query, storage)
- ✅ Prometheus alerting rules
- ✅ Kubernetes deployment manifests
- ✅ Security scanning pipeline

### Phase 4: 100-System Pilot

- [ ] Deploy to production Kubernetes cluster
- [ ] Onboard 10 pilot systems
- [ ] Monitor SLO compliance (P50 \<500ms, P99 \<2s)
- [ ] Scale to 100 systems
- [ ] Validate cost projections

**Timeline**: 12 weeks total (Phase 1-3 complete, Phase 4 ready to begin)

See [`docs/ROADMAP.md`](docs/ROADMAP.md) for complete details.

______________________________________________________________________

## Contributing

We welcome contributions! Please follow these guidelines:

### Development Workflow

1. **Fork and clone** the repository
1. **Create a feature branch**: `git checkout -b feature/your-feature`
1. **Install dependencies**: `uv sync --group dev`
1. **Make your changes** following our code standards
1. **Run tests**: `pytest`
1. **Run linter**: `ruff check akosha/`
1. **Commit with conventional commits**: `git commit -m "feat: add new feature"`
1. **Push and create PR**: `git push origin feature/your-feature`

### Code Standards

- **Type hints required** on all functions
- **Docstrings required** on all public APIs
- **Tests required** for new features
- **Maximum complexity**: 15 (Ruff)
- **Coverage**: Maintain 85%+

______________________________________________________________________

## License

______________________________________________________________________

## Acknowledgments

- **Session-Buddy**: For the excellent MCP server patterns
- **Oneiric**: For universal storage adapter framework
- **FastMCP**: For elegant MCP protocol implementation
- **Ollama / OpenAI**: Real embeddings are delegated to MCP-side providers running in the configured Bodai ecosystem

______________________________________________________________________

**Made with ❤️ by the Akosha team**

*आकाश (Akosha) - The sky has no limits*
