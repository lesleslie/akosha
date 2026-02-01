# Akosha Monitoring Setup

This directory contains Grafana dashboards and Prometheus alerting rules for monitoring Akosha.

## Overview

- **Grafana Dashboards**: Visualizations for embedding performance, circuit breaker status, and system health
- **Prometheus Alerts**: Automated alerting rules for detecting issues

## Grafana Dashboards

### 1. Embedding Performance Dashboard

**File**: `grafana/embedding-performance.json`

**Metrics**:

- Embedding generation rate (embeddings/sec)
- Text length distribution (p50, p95, p99)
- Real vs Fallback mode ratio
- Total embeddings generated
- Average text length
- Batch processing metrics

**Use Cases**:

- Monitor embedding service performance
- Detect when fallback mode is being used
- Track text processing patterns

### 2. Circuit Breaker Status Dashboard

**File**: `grafana/circuit-breaker-status.json`

**Metrics**:

- Circuit breaker states (OPEN, CLOSED, HALF_OPEN)
- Service success rates
- Total calls by service
- Success/Failure/Reject ratios
- Consecutive failures by service
- Recent circuit breaker trips

**Use Cases**:

- Monitor service health and resilience
- Detect failing external services
- Track circuit breaker state transitions

### 3. System Health Dashboard

**File**: `grafana/system-health.json`

**Metrics**:

- Analytics operations rate
- Knowledge graph growth
- Trend analysis strength
- Anomaly detection rate
- Knowledge graph operations
- System health score
- Active traces

**Use Cases**:

- Overall system health monitoring
- Analytics performance tracking
- Knowledge graph growth trends

## Prometheus Alerting Rules

**File**: `prometheus/alerts.yml`

### Alert Categories

#### Circuit Breaker Alerts

| Alert | Severity | Trigger | Description |
|-------|----------|---------|-------------|
| `CircuitBreakerOpen` | Warning | Circuit in OPEN state for >1min | Circuit breaker is blocking calls |
| `CircuitBreakerHighFailureRate` | Warning | >50% failure rate for 2min | Service experiencing high failure rate |
| `CircuitBreakerConsecutiveFailures` | Critical | ≥4 consecutive failures | Multiple consecutive failures detected |

#### Embedding Service Alerts

| Alert | Severity | Trigger | Description |
|-------|----------|---------|-------------|
| `HighFallbackModeUsage` | Warning | >50% fallback mode for 5min | Embedding service using fallback mode |
| `EmbeddingServiceDown` | Critical | No real embeddings for 5min | Embedding service not working |

#### Analytics Alerts

| Alert | Severity | Trigger | Description |
|-------|----------|---------|-------------|
| `HighAnomalyRate` | Warning | >20% anomaly rate for 10min | Unusual system behavior detected |
| `AnalyticsProcessingFailure` | Warning | Failed trend analysis operations | Analytics processing issues |

#### System Health Alerts

| Alert | Severity | Trigger | Description |
|-------|----------|---------|-------------|
| `HighRejectionRate` | Warning | >10% rejection rate for 5min | Many calls being rejected |
| `KnowledgeGraphGrowthStalled` | Info | No growth for 15min | Knowledge graph not growing |

## Deployment

### Docker Compose

```yaml
version: '3.8'

services:
  prometheus:
    image: prom/prometheus:latest
    ports:
      - "9090:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
      - ./monitoring/prometheus/alerts.yml:/etc/prometheus/alerts.yml
      - prometheus-data:/prometheus
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--storage.tsdb.path=/prometheus'
      - '--web.console.libraries=/etc/prometheus/console_libraries'
      - '--web.console.templates=/etc/prometheus/consoles'
      - '--web.enable-lifecycle'
    networks:
      - monitoring

  grafana:
    image: grafana/grafana:latest
    ports:
      - "3001:3000"
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=admin
      - GF_USERS_ALLOW_SIGN_UP=false
    volumes:
      - grafana-data:/var/lib/grafana
      - ./monitoring/grafana:/etc/grafana/provisioning/dashboards
    networks:
      - monitoring
    depends_on:
      - prometheus

  alertmanager:
    image: prom/alertmanager:latest
    ports:
      - "9093:9093"
    volumes:
      - ./alertmanager.yml:/etc/alertmanager/alertmanager.yml
    networks:
      - monitoring

networks:
  monitoring:
    driver: bridge

volumes:
  prometheus-data:
  grafana-data:
```

### Prometheus Configuration

Update `prometheus.yml` to include alerting rules:

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

# Alertmanager configuration
alerting:
  alertmanagers:
    - static_configs:
        - targets:
          - alertmanager:9093

# Load rules once and periodically evaluate them
rule_files:
  - "/etc/prometheus/alerts.yml"

scrape_configs:
  - job_name: 'akosha-mcp'
    static_configs:
      - targets: ['akosha:3002']
    metrics_path: /metrics

  - job_name: 'prometheus'
    static_configs:
      - targets: ['localhost:9090']
```

### AlertManager Configuration

Create `alertmanager.yml` for alert routing:

```yaml
global:
  resolve_timeout: 5m

route:
  group_by: ['alertname', 'cluster', 'service']
  group_wait: 10s
  group_interval: 10s
  repeat_interval: 12h
  receiver: 'default'

  routes:
    - match:
        severity: critical
      receiver: 'critical-alerts'
    - match:
        severity: warning
      receiver: 'warning-alerts'

receivers:
  - name: 'default'
    webhook_configs:
      - url: 'http://localhost:8000/alerts'

  - name: 'critical-alerts'
    pagerduty_configs:
      - service_key: '<PAGERDUTY_SERVICE_KEY>'
    slack_configs:
      - api_url: '<SLACK_WEBHOOK_URL>'
        channel: '#alerts-critical'

  - name: 'warning-alerts'
    slack_configs:
      - api_url: '<SLACK_WEBHOOK_URL>'
        channel: '#alerts-warning'
```

## Importing Dashboards

### Method 1: Grafana UI

1. Open Grafana at `http://localhost:3001` (default: admin/admin)
1. Navigate to **Dashboards** → **Import**
1. Upload JSON files from `monitoring/grafana/`

### Method 2: Provisioning (Docker)

Dashboards are automatically loaded when mounted to `/etc/grafana/provisioning/dashboards`

### Method 3: Grafana API

```bash
# Import embedding performance dashboard
curl -X POST \
  -H "Content-Type: application/json" \
  -d @monitoring/grafana/embedding-performance.json \
  http://admin:admin@localhost:3001/api/dashboards/db
```

## Metrics Reference

### Embedding Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `embedding_generated_total` | Counter | mode | Total embeddings generated |
| `embedding_text_length` | Histogram | mode | Text length distribution |
| `embedding_batch_generated_total` | Counter | mode | Batch operations |
| `embedding_batch_size` | Histogram | mode | Batch size distribution |

### Circuit Breaker Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `circuit_state` | Gauge | service_name, state | Circuit state (0=open, 1=closed, 2=half_open) |
| `circuit_total_calls` | Counter | service_name | Total calls attempted |
| `circuit_successful_calls` | Counter | service_name | Successful calls |
| `circuit_failed_calls` | Counter | service_name | Failed calls |
| `circuit_rejected_calls` | Counter | service_name | Rejected calls (circuit open) |
| `circuit_consecutive_failures` | Gauge | service_name | Current failure streak |
| `circuit_consecutive_successes` | Gauge | service_name | Current success streak |
| `circuit_success_rate` | Gauge | service_name | Success rate (0.0-1.0) |

### Analytics Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `analytics_metrics_added_total` | Counter | metric_name | Metrics added |
| `analytics_trend_completed_total` | Counter | direction | Trend analysis completed |
| `analytics_trend_strength` | Histogram | direction | Trend strength |
| `analytics_anomaly_detected_total` | Counter | metric_name | Anomalies detected |
| `analytics_anomaly_rate` | Gauge | metric_name | Anomaly rate |
| `analytics_correlation_completed_total` | Counter | - | Correlation analysis completed |

### Knowledge Graph Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `kg_entities_total` | Gauge | - | Total entities in graph |
| `kg_edges_total` | Gauge | - | Total edges in graph |
| `kg_entities_added_total` | Counter | - | Entities added |
| `kg_edges_added_total` | Counter | - | Edges added |
| `kg_neighbors_found` | Histogram | - | Neighbors found |
| `kg_shortest_path_found_total` | Counter | - | Paths found |
| `kg_shortest_path_length` | Histogram | - | Path length |

## Troubleshooting

### Dashboards Not Showing Data

1. **Check Prometheus targets**:

   ```bash
   curl http://localhost:9090/api/v1/targets
   ```

1. **Verify metrics are being exposed**:

   ```bash
   curl http://localhost:3002/metrics
   ```

1. **Check Grafana data source**:

   - Go to Configuration → Data Sources → Prometheus
   - Click "Test" to verify connection

### Alerts Not Firing

1. **Check Prometheus rules**:

   ```bash
   curl http://localhost:9090/api/v1/rules
   ```

1. **Verify AlertManager connectivity**:

   ```bash
   curl http://localhost:9093/api/v1/status
   ```

1. **Check alert evaluation**:

   - Go to Prometheus UI → Alerts
   - Verify alert state and evaluation time

### High Memory Usage

Prometheus can use significant memory with high cardinality metrics. Solutions:

1. **Reduce metric cardinality** by limiting label values
1. **Adjust retention** in `prometheus.yml`:
   ```yaml
   --storage.tsdb.retention.time=15d
   ```
1. **Add recording rules** to pre-aggregate data

## Next Steps

1. **Set up AlertManager**: Configure notification channels (Slack, PagerDuty, email)
1. **Customize dashboards**: Add project-specific panels and queries
1. **Tune alert thresholds**: Adjust for your environment's baselines
1. **Add recording rules**: Create pre-aggregated metrics for better performance
1. **Set up alert silences**: Configure maintenance windows

## Additional Resources

- [Prometheus Documentation](https://prometheus.io/docs/)
- [Grafana Documentation](https://grafana.com/docs/)
- [AlertManager Documentation](https://prometheus.io/docs/alerting/latest/alertmanager/)
- [OpenTelemetry Metrics](https://opentelemetry.io/docs/reference/specification/metrics/)
