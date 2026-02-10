# Akosha Service Dependencies

## Overview

Akosha is designed as a **standalone service** that can operate independently or integrate with other ecosystem services. It requires no external services for basic operation but provides enhanced capabilities when connected to the broader ecosystem.

## Required Services

**None** - Akosha is fully self-contained and can run without any external dependencies.

### Core Dependencies (Installed via pip)

- **Python 3.13+** - Required for modern type hints
- **DuckDB** - Embedded database for hot/warm storage (automatically installed)
- **FastAPI** - API framework
- **Pydantic** - Configuration and validation
- **FastMCP** - MCP protocol implementation

### Optional Dependencies

- **sentence-transformers** - Real semantic embeddings (fallback available)
- **onnxruntime** - ONNX model inference (fallback available)

## Optional Integrations

### Mahavishnu (Orchestrator)

**Purpose**: Coordinate workflows and manage cross-repository operations

**Integration Type**: MCP Client/Server

**Benefits**:

- Mahavishnu can query Akosha for cross-system insights
- Akosha can aggregate data from Mahavishnu-managed systems
- Automated workflow orchestration for analytics tasks

**Configuration**:

```yaml
# In Mahavishnu settings
akosha:
  url: "http://localhost:8682/mcp"
  enabled: true
```

**Status**: Optional - Akosha provides value without Mahavishnu

### Session-Buddy (Session Manager)

**Purpose**: Provide session data and context for aggregation

**Integration Type**: MCP Client

**Benefits**:

- Real-time session data ingestion
- Cross-system session correlation
- Memory aggregation across all Session-Buddy instances

**Configuration**:

```python
# Akosha automatically discovers Session-Buddy instances via MCP
# No manual configuration required
```

**Status**: Optional but recommended for full functionality

### OpenSearch (Vector Search)

**Purpose**: Distributed vector search at scale

**Integration Type**: External Service

**Benefits**:

- Horizontal scaling for vector search
- Multi-node deployment
- Advanced vector similarity algorithms

**Configuration**:

```bash
# Environment variables
AKOSHA_OPENSEARCH_ENABLED=true
AKOSHA_OPENSEARCH_URL=https://opensearch.example.com:9200
AKOSHA_OPENSEARCH_INDEX=akosha_memories
```

**Status**: Optional - DuckDB provides vector search for small/medium deployments

### Cloudflare R2 (Cold Storage)

**Purpose**: Cost-effective long-term storage for archived data

**Integration Type**: Cloud Storage

**Benefits**:

- 90+ day data retention at low cost
- Automatic data tiering
- Parquet format for efficient querying

**Configuration**:

```bash
# Environment variables
AKOSHA_COLD_BUCKET=your-bucket-name
AKOSHA_COLD_ENDPOINT=https://your-account.r2.cloudflarestorage.com
AKOSHA_COLD_REGION=auto
AKOSHA_COLD_ACCESS_KEY_ID=your-access-key
AKOSHA_COLD_SECRET_ACCESS_KEY=your-secret-key
```

**Status**: Optional - Data remains in warm storage if not configured

## Service Health Dependencies

### When Akosha is Down

- **Mahavishnu**: Continues operating but loses cross-system analytics
- **Session-Buddy**: Continues operating but data is not aggregated
- **Other Services**: No impact

### When Mahavishnu is Down

- **Akosha**: Continues operating but loses orchestrated workflow integration
- **Session-Buddy**: Continues operating normally
- **Other Services**: No impact

### When Session-Buddy is Down

- **Akosha**: Continues operating but cannot ingest new session data
- **Mahavishnu**: Continues operating but loses session tracking
- **Other Services**: No impact

## Network Requirements

### Inbound Connections

- **MCP Server Port**: Default 8682 (configurable via `AKOSHA_PORT`)
- **HTTP API Port**: Default 8000 (configurable via `AKOSHA_API_PORT`)
- **Metrics Endpoint**: Default 8000/metrics (Prometheus scraping)

### Outbound Connections

- **OpenSearch**: TCP 9200 (if enabled)
- **Cloudflare R2**: HTTPS 443 (if enabled)
- **Session-Buddy instances**: MCP protocol (auto-discovered)

## Startup Order

### Standalone Deployment

```bash
# 1. Start Akosha (no dependencies)
akosha start --host 0.0.0.0 --port 8000
```

### Full Ecosystem Deployment

```bash
# 1. Start Session-Buddy (provides session data)
session-buddy start

# 2. Start Akosha (connects to Session-Buddy via MCP)
akosha start --mcp

# 3. Start Mahavishnu (connects to both Akosha and Session-Buddy)
mahavishnu start
```

## Resource Requirements

### Minimum (Development)

- **CPU**: 2 cores
- **Memory**: 4 GB RAM
- **Storage**: 10 GB
- **Network**: Localhost only

### Recommended (Production - 100 Systems)

- **CPU**: 4-8 cores
- **Memory**: 16-32 GB RAM
- **Storage**: 500 GB SSD + Cloudflare R2
- **Network**: 1 Gbps

### Large Scale (Production - 100,000 Systems)

- **CPU**: 32+ cores (distributed)
- **Memory**: 128+ GB RAM (distributed)
- **Storage**: 10 TB SSD + OpenSearch + Cloudflare R2
- **Network**: 10 Gbps

## Monitoring Dependencies

### Prometheus (Optional)

**Purpose**: Metrics collection and alerting

**Configuration**:

```yaml
# Prometheus scrape config
scrape_configs:
  - job_name: 'akosha'
    static_configs:
      - targets: ['akosha:8000']
    metrics_path: '/metrics'
```

### Grafana (Optional)

**Purpose**: Dashboard visualization

**Status**: Pre-built dashboards provided in `k8s/monitoring/`

## Security Considerations

### Authentication

- **JWT Authentication**: Optional, enabled via `AKOSHA_AUTH_ENABLED=true`
- **Secret**: Required for JWT signing (generate with `secrets.token_urlsafe(32)`)

### Network Security

- **Local Only**: Default configuration binds to 127.0.0.1
- **TLS/SSL**: Recommended for production deployments
- **Firewall**: Restrict inbound ports to trusted networks

### Secrets Management

- **Environment Variables**: All secrets via environment variables
- **HashiCorp Vault**: Optional integration for enterprise deployments
- **AWS Secrets Manager**: Optional integration for AWS deployments

## Troubleshooting

### Akosha Cannot Connect to Session-Buddy

```bash
# Check Session-Buddy status
session-buddy health

# Check Akosha MCP configuration
akosha info

# Verify MCP server is running
ps aux | grep akosha
```

### Akosha Cannot Connect to OpenSearch

```bash
# Test OpenSearch connectivity
curl https://opensearch.example.com:9200/_cluster/health

# Check Akosha configuration
akosha info | grep opensearch
```

### Cold Storage Uploads Failing

```bash
# Verify Cloudflare R2 credentials
aws s3 ls \
  --endpoint-url=https://your-account.r2.cloudflarestorage.com \
  --bucket your-bucket-name

# Check Akosha logs
akosha logs --tail 100
```

## Next Steps

- **Architecture**: See [ARCHITECTURE.md](../ARCHITECTURE.md) for complete system architecture
- **Deployment**: See [DEPLOYMENT_GUIDE.md](../DEPLOYMENT_GUIDE.md) for production deployment
- **API Reference**: See [USER_GUIDE.md](../USER_GUIDE.md) for complete API documentation
