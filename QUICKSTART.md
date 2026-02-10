# Akosha Quickstart (5 minutes)

Akosha is the **universal memory aggregation system** for the Mahavishnu ecosystem. It collects, processes, and analyzes memories from multiple Session-Buddy instances to provide cross-system insights.

## Level 1: Basic Setup (1 minute) ✅

```bash
# Install dependencies
cd /path/to/akosha
uv sync --group dev

# Start Akosha MCP server
uv run python -m akosha.mcp

# Verify installation
uv run python -c "from akosha.processing.embeddings import get_embedding_service; print('✅ Akosha ready!')"
```

## Level 2: Generate Embeddings (2 minutes) 🔍

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

## Level 3: Detect Anomalies (2 minutes) 📊

```python
from akosha.processing.analytics import TimeSeriesAnalytics

analytics = TimeSeriesAnalytics()

# Add metric data
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

## Next Steps

- **Admin Shell**: Run `akosha shell` for interactive analytics
- **MCP Integration**: Configure in `~/.claude/.mcp.json` (see README.md)
- **Full Documentation**: See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for complete architecture guide

## What Can Akosha Do?

- **Semantic Search**: Find relevant conversations across all systems using vector embeddings
- **Time-Series Analytics**: Detect trends, anomalies, and correlations
- **Knowledge Graph**: Cross-system entity relationships and path finding
- **Three-Tier Storage**: Hot (in-memory) → Warm (on-disk) → Cold (Cloudflare R2)

## Configuration

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

## Production Deployment

For production deployment with Kubernetes, monitoring, and security:

```bash
# Review deployment guide
cat docs/DEPLOYMENT_GUIDE.md

# Deploy to Kubernetes
kubectl apply -f kubernetes/

# Verify deployment
kubectl get pods -n akosha
kubectl port-forward -n akosha svc/akosha-api 8000:8000

# Check metrics
curl http://localhost:8000/metrics
```

See [Deployment Guide](docs/DEPLOYMENT_GUIDE.md) for complete production setup.
