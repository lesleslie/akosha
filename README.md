# Akosha (आकाश) - Universal Memory Aggregation System

**आकाश (Akosha) - The sky has no limits** 🚀

![Production Ready](https://img.shields.io/badge/status-production%20ready-brightgreen)
![Phase 1 Complete](https://img.shields.io/badge/phase%201-complete-success)
![Tests Passing](https://img.shields.io/badge/tests-32%2F32%20passing-brightgreen)

**Version**: 0.3.0 (Phase 1: Production Pilot Ready)
**Status**: Production Ready for 100-System Pilot

> "Akosha" (आकाश) means "sky" or "space" in Sanskrit - representing infinite, boundless memory aggregation across all your Session-Buddy instances.

______________________________________________________________________

## 🚀 What is Akosha?

Akosha is a **universal memory aggregation system** that collects, processes, and analyzes memories from multiple Session-Buddy instances (100-100,000 systems). It provides:

- **Semantic Search**: Find relevant conversations across all systems using vector embeddings
- **Time-Series Analytics**: Detect trends, anomalies, and correlations
- **Knowledge Graph**: Cross-system entity relationships and path finding
- **Three-Tier Storage**: Hot (in-memory) → Warm (on-disk) → Cold (Cloudflare R2)

### Key Capabilities

✅ **Privacy-First**: Local ONNX embeddings, no external API calls required
✅ **Scalable**: Handles 100 to 100,000+ Session-Buddy instances
✅ **Real-Time Analytics**: Trend detection, anomaly spotting, cross-system correlation
✅ **MCP Protocol**: Exposes all capabilities via Model Context Protocol
✅ **Production Ready**: Comprehensive tests, graceful degradation, type-safe code

______________________________________________________________________

## ⚡ Quick Start

### Prerequisites

- **Python 3.13+** (required for modern type hints)
- **UV** package manager (recommended) or pip
- **DuckDB** (automatically installed)
- **Optional**: sentence-transformers for real embeddings (fallback available)

### 5-Minute Setup

```bash
# 1. Clone repository
git clone https://github.com/yourusername/akosha.git
cd akosha

# 2. Install dependencies
uv sync --group dev

# 3. Start Akosha MCP server
uv run python -m akosha.mcp

# 4. Verify installation
uv run python -c "from akosha.processing.embeddings import get_embedding_service; print('✅ Akosha ready!')"
```

That's it! Akosha is now running and ready to aggregate memories.

### 🚀 Production Deployment

For production deployment with Kubernetes, monitoring, and security:

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

## 🔧 Installation

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

For **real semantic embeddings** (recommended):

```bash
# Using UV
uv add --optional embeddings sentence-transformers onnxruntime

# Using pip
pip install "akosha[embeddings]"
```

**Note**: Akosha works without these dependencies using deterministic fallback embeddings. Real embeddings provide better semantic search.

______________________________________________________________________

## ⚙️ Configuration

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

## 🔌 MCP Server Setup

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

### Project-Level Configuration

Create `.mcp.json` in Akosha directory:

```json
{
  "mcpServers": {
    "akosha": {
      "command": "uv",
      "args": ["run", "python", "-m", "akosha.mcp"],
      "cwd": "."
    }
  }
}
```

______________________________________________________________________

## 💡 Usage Examples

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


## 🛠️ CLI Reference

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


## 🏗️ Architecture

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

### MCP Tools (11 Total)

**Search Tools (3):**

- `generate_embedding` - Generate semantic embeddings
- `generate_batch_embeddings` - Batch embedding generation
- `search_all_systems` - Semantic search across all systems

**Analytics Tools (4):**

- `get_system_metrics` - Get metrics and statistics
- `analyze_trends` - Detect trends (increasing/decreasing/stable)
- `detect_anomalies` - Find statistical outliers
- `correlate_systems` - Cross-system correlation analysis

**Graph Tools (3):**

- `query_knowledge_graph` - Query entities and relationships
- `find_path` - Shortest path between entities
- `get_graph_statistics` - Graph metrics and statistics

**System Tools (1):**

- `get_storage_status` - Storage tier status

______________________________________________________________________

## 🧪 Development

### Code Quality Standards

- **Type Hints**: Required for all functions (modern Python 3.13+ syntax)
- **Docstrings**: Comprehensive Google-style docstrings
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

## ✅ Testing

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

## 🗺️ Roadmap

### ✅ Phase 1: Foundation (COMPLETE)

- Three-tier storage architecture
- Basic ingestion pipeline
- Knowledge graph construction
- MCP server framework

### ✅ Phase 2: Advanced Features (COMPLETE)

- ONNX embedding service
- Time-series analytics
- Cross-system correlation
- 11 MCP tools integrated

### ✅ Phase 3: Production Hardening (COMPLETE)

- ✅ Integration test suite (end-to-end testing)
- ✅ Load testing framework (Locust-based)
- ✅ Authentication & authorization (JWT + RBAC)
- ✅ Prometheus metrics collection
- ✅ Grafana dashboards (ingestion, query, storage)
- ✅ Prometheus alerting rules
- ✅ Kubernetes deployment manifests
- ✅ Security scanning pipeline

### 🚀 Phase 4: 100-System Pilot (READY TO START)

- [ ] Deploy to production Kubernetes cluster
- [ ] Onboard 10 pilot systems
- [ ] Monitor SLO compliance (P50 <500ms, P99 <2s)
- [ ] Scale to 100 systems
- [ ] Validate cost projections

**Timeline**: 12 weeks total (Phase 1-3 complete, Phase 4 ready to begin)

See [`docs/ROADMAP.md`](docs/ROADMAP.md) for complete details.

______________________________________________________________________

## 🤝 Contributing

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

## 📄 License

______________________________________________________________________

## 🙏 Acknowledgments

- **Session-Buddy**: For the excellent MCP server patterns
- **Oneiric**: For universal storage adapter framework
- **FastMCP**: For elegant MCP protocol implementation
- **Sentence-Transformers**: For all-MiniLM-L6-v2 model

______________________________________________________________________

**Made with ❤️ by the Akosha team**

*आकाश (Akosha) - The sky has no limits*
