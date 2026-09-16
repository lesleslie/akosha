# Akosha

[![Code style: crackerjack](https://img.shields.io/badge/code%20style-crackerjack-000042)](https://github.com/lesleslie/crackerjack)
[![Runtime: oneiric](https://img.shields.io/badge/runtime-oneiric-6e5494)](https://github.com/lesleslie/oneiric)
[![Framework: FastMCP](https://img.shields.io/badge/framework-FastMCP-0ea5e9)](https://github.com/jlowin/fastmcp)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![Python: 3.14+](https://img.shields.io/badge/python-3.14%2B-green)](https://www.python.org/downloads/)

Universal memory aggregation and cross-system analytics for distributed systems.

**Version:** 0.17.5

## Quick Links

- [Overview](#what-is-akosha)
- [Quick Start](#quick-start)
- [Installation](#installation)
- [Usage Examples](#usage-examples)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [MCP Tools](#mcp-tools-profile-gated-inventory)
- [Quality Checks](#quality-checks)

## Quality Checks

Crackerjack is the standard quality gate for Akosha changes. Run the checks
that match the scope of your change:

```bash
uv run crackerjack lint
uv run crackerjack typecheck
uv run crackerjack security
uv run crackerjack analyze
uv run crackerjack run --run-tests
```

______________________________________________________________________

## What is Akosha?

Akosha is a universal memory aggregation system that collects, processes, and analyzes memories from multiple Session-Buddy instances. It provides:

- **Semantic Search**: Find relevant conversations across all systems using vector embeddings
- **Time-Series Analytics**: Detect trends, anomalies, and correlations
- **Knowledge Graph**: Cross-system entity relationships and path finding
- **Three-Tier Storage**: Hot (DuckDB or pgvector) → Warm (DuckDB on disk) → Cold (Parquet through a configured local or cloud backend)

### Key Capabilities

✅ **Privacy-First**: Deterministic mock embeddings; real backends delegated to MCP-side providers (Ollama, OpenAI)
✅ **Real-Time Analytics**: Trend detection, anomaly spotting, cross-system correlation
✅ **MCP Protocol**: Exposes all capabilities via Model Context Protocol
✅ **Operational Baseline**: Health probes, graceful degradation, metrics, and type-safe code

______________________________________________________________________

## Quick Start

### Prerequisites

- **Python 3.14+**
- **UV** package manager (recommended) or pip
- **Optional**: PostgreSQL with the pgvector extension for persistent hot-store storage across restarts

> **Note on embeddings**: Akosha generates deterministic mock embeddings
> in-process (see `akosha/processing/embeddings.py`); real embeddings
> are delegated to MCP-side providers (Ollama, OpenAI). The historical
> `embeddings` optional dependency group was emptied in 2026-08 when
> `onnxruntime` was dropped, so there is no native ONNX / sentence-
> transformers install path from this repo.

> **pgvector note**: If using pgvector-backed storage, your PostgreSQL instance must have the `vector` extension enabled: `CREATE EXTENSION vector;`.

### 5-Minute Setup

```bash
# 1. Clone repository
git clone https://github.com/lesleslie/akosha.git
cd akosha

# 2. Install dependencies
uv sync --group dev

# 3. Verify the CLI and configuration
uv run akosha health --json

# 4. Start Akosha in the default lite mode
uv run akosha start
```

That's it! Akosha is now running and ready to aggregate memories.

### Service Operation

Run the MCP server directly with Akosha’s CLI:

```bash
uv run akosha start --host 0.0.0.0 --mode standard
```

The default port is `8682`. Once running, use `/health` for the aggregate
readiness probe and `/metrics` for Prometheus exposition.

______________________________________________________________________

## Installation

### Using UV (Recommended)

```bash
# Install all dependencies (development + production)
uv sync --group dev

# Install minimal runtime dependencies only
uv sync
```

### Using Pip

```bash
# Create virtual environment
python3.14 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -e .
```

### Optional Dependencies

Akosha has no optional dependency groups for in-process embeddings — the
`embeddings` PEP 735 group in `pyproject.toml` is intentionally empty
(see its inline comment). Real semantic embeddings, when you need them,
are produced by MCP-side providers (Ollama, OpenAI) configured in your
Bodai deployment; this package does not ship a native ONNX / sentence-
transformers runtime.

For **persistent hot-store storage across restarts**:

```bash
# Serverless / production: pgvector-backed hot store
uv sync --group vector-pg
```

The canonical runtime settings live in [`settings/akosha.yaml`](settings/akosha.yaml).

______________________________________________________________________

## Configuration

### Layered Settings

Akosha uses Oneiric’s layered configuration loader. For `project_name="akosha"`,
the file layers are applied in this order:

1. Defaults in the configuration models
1. `settings/akosha.yaml` — committed project defaults
1. `settings/local.yaml` — gitignored project-local overrides
1. `${XDG_CONFIG_HOME:-~/.config}/akosha/config.yaml` — user configuration
1. `${XDG_CONFIG_HOME:-~/.config}/akosha/local.yaml` — user-local overrides
1. `AKOSHA_*` environment variables, including nested `__` overrides
1. An explicit configuration path when supplied

Missing files are ignored. Set `XDG_CONFIG_HOME` to relocate the user-level
configuration directory. The CLI also accepts `--config /path/to/config.yaml`.

Common environment variables include:

```bash
# Cold storage
AKOSHA_COLD_BUCKET=your-bucket-name
AKOSHA_COLD_REGION=auto
AKOSHA_COLD_BACKEND=local

# Runtime
AKOSHA_MODE=lite
AKOSHA_MCP_PORT=8682
AKOSHA_INGESTION_WORKERS=3

# Nested hot-store override
AKOSHA__STORAGE__HOT__BACKEND=pgvector
AKOSHA__STORAGE__HOT__PG_URL=postgresql://user@localhost:5432/akosha
```

______________________________________________________________________

## MCP Server Setup

For an HTTP MCP server, add this entry to `~/.claude/.mcp.json`:

```json
{
  "mcpServers": {
    "akosha": {
      "command": "uv",
      "args": ["run", "python", "-m", "akosha.mcp"],
      "cwd": "/path/to/akosha",
      "env": {
        "PYTHONPATH": "/path/to/akosha"
      }
    }
  }
}
```

> Replace `/path/to/akosha` with the actual path to your Akosha checkout. If
> Akosha is installed on `PATH`, `command: "akosha"` with arguments
> `["mcp", "start"]` is also supported.

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
        timestamp=now - timedelta(hours=20 - i),
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

# Show system information and available modes
akosha info
akosha modes

# Run the Oneiric health and diagnostic probes
akosha health --json
akosha doctor --json

# Start Akosha server
akosha start --host 0.0.0.0 --port 8682
```

## Architecture

### Three-Tier Storage

```
┌─────────────────────────────────────────────────────────┐
│                    Akosha System                         │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  Hot Store                                               │
│  ├─ DuckDB in-memory by default                         │
│  ├─ pgvector for persistent deployments                 │
│  └─ Full-precision embeddings and recent queries        │
│                                                          │
│  Warm Store                                              │
│  ├─ DuckDB on-disk                                      │
│  ├─ Quantized embeddings and summaries                  │
│  └─ Date-based partitioning                             │
│                                                          │
│  Cold Store                                              │
│  ├─ Parquet files                                       │
│  ├─ Local, S3/R2, GCS, or Azure backend                 │
│  └─ Compressed archival summaries                       │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

### MCP Tools (Profile-Gated Inventory)

Akosha exposes its tools via the `AKOSHA_TOOL_PROFILE` environment variable.
The list below is the **FULL profile** (31 tools), which is the default.
Profiles: **MINIMAL** (9 tools, health plus published-agent and ecosystem-skill
discovery) → **STANDARD** (19 tools, adds core memory aggregation and the
signed skill catalog) → **FULL** (31 tools, adds Session-Buddy, PyCharm, OTel,
fitness, EventBridge, and cross-repo integrations).

Source of truth: [`akosha/mcp/tools/profiles.py`](akosha/mcp/tools/profiles.py),
`REGISTRATION_TOOLS`.

**Health & Dependency Probes (6):**

- `get_liveness` - Liveness check
- `get_readiness` - Readiness check
- `health_check_service` - Check a single dependency
- `health_check_all` - Check all dependencies
- `wait_for_dependency` - Block until a dependency is healthy
- `wait_for_all_dependencies` - Block until every dependency is healthy

**Core Memory Aggregation (8):**

- `akosha_generate_embedding` - Generate semantic embedding for one text
- `akosha_generate_batch_embeddings` - Batch embedding generation
- `akosha_search_all_systems` - Semantic search across systems
- `akosha_detect_anomalies` - Statistical anomaly detection
- `akosha_analyze_trends` - Time-series trend analysis (increasing/decreasing/stable)
- `akosha_correlate_systems` - Cross-system correlation analysis
- `akosha_query_knowledge_graph` - Entity and relationship queries
- `akosha_get_system_metrics` - Aggregate system metrics

**Session-Buddy Integration (2):**

- `akosha_store_memory` - Store a memory directly from Session-Buddy (in-process hot store)
- `akosha_batch_store_memories` - Bulk-store memory entries from Session-Buddy

**PyCharm / IDE Integration (5):**

- `akosha_get_code_problems` - Pull file-level diagnostics from PyCharm
- `akosha_search_code_patterns` - Project-wide regex search across indexed repos
- `akosha_find_function_usage` - Find usages of a function symbol
- `akosha_analyze_imports` - Analyze imports for a file
- `akosha_pycharm_health` - PyCharm MCP connectivity

**OpenTelemetry Trace Queries (1):**

- `akosha_query_local_traces` - Query OTel traces by task class + time window

**Fitness Analyzer (2):**

- `akosha_run_fitness_analysis` - On-demand fitness signal computation
- `akosha_get_fitness_analyzer_status` - Fitness analyzer status

**EventBridge Publisher (1):**

- `akosha_publish_to_eventbridge` - Emit analytics events to the Bodai EventBridge

**Cross-Repo Capability Search (1):**

- `akosha_cross_repo_capability_search` - Search the Bodai capability catalog

**Published Skill Catalog (2):**

- `akosha_list_skills` - List signed metadata for server-published skills
- `akosha_get_skill` - Return signed metadata and the body for one skill

**Published Agent Catalog (2):**

- `akosha_list_agents` - List signed metadata for server-published agents
- `akosha_get_agent` - Return signed metadata and the body for one agent

**Ecosystem Skill Federation (1):**

- `akosha_list_ecosystem_skills` - Aggregate skill metadata across Bodai MCP servers

______________________________________________________________________

## Bodai Integration

When deployed inside the [Bodai ecosystem](https://github.com/lesleslie/bodai),
Akosha serves as the **seer** — the cross-system intelligence layer that
aggregates embeddings, semantic search, and pattern detection across the other
Bodai components (Session-Buddy, Mahavishnu, Crackerjack, Oneiric). The
standalone install is identical: Bodai adds no special-case overrides; the
PyArrow / DuckDB / pgvector backend, the Watcher + Eval pipeline, and the
MCP tool surface behave the same in any other Python deployment. Bodai
plugins wire their ingesters into `akosha.search_all_systems` to make their
locally stored memories discoverable across the cluster.

## Contributing

Contributions should include focused changes, relevant documentation, and
validation using the [Crackerjack](https://github.com/lesleslie/crackerjack)
commands in [Quality Checks](#quality-checks).

## License

BSD 3-Clause License. See [`LICENSE`](./LICENSE).

______________________________________________________________________

## Acknowledgments

Akosha is built on open-source foundations including [FastMCP](https://github.com/jlowin/fastmcp),
[DuckDB](https://duckdb.org/), [PyArrow](https://arrow.apache.org/docs/python/),
Redis, PostgreSQL/pgvector, and the configured Ollama or OpenAI embedding
providers.

______________________________________________________________________

**Made with ❤️ by the Akosha team**

*आकाश (Akosha) - The sky has no limits*
