# Akosha Deployment Guide

**Version**: 0.3.0 (Phase 1: Production Pilot)
**Last Updated**: 2025-02-08
**Target Scale**: 100 Session-Buddy systems

This guide provides step-by-step instructions for deploying Akosha to a production Kubernetes cluster with monitoring, security, and scaling capabilities.

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Architecture Overview](#architecture-overview)
3. [Environment Setup](#environment-setup)
4. [Kubernetes Deployment](#kubernetes-deployment)
5. [Configuration](#configuration)
6. [Monitoring & Observability](#monitoring--observability)
7. [Security Hardening](#security-hardening)
8. [Scaling Guidelines](#scaling-guidelines)
9. [Troubleshooting](#troubleshooting)
10. [Runbook](#runbook)

---

## Prerequisites

### Infrastructure Requirements

**Minimum (10 systems pilot):**
- Kubernetes cluster: 3 nodes, 8 vCPU, 32 GB RAM
- Storage: 100 GB SSD (hot tier), 500 GB SSD (warm tier)
- Network: 1 Gbps

**Recommended (100 systems):**
- Kubernetes cluster: 6 nodes, 32 vCPU, 128 GB RAM
- Storage: 500 GB NVMe (hot), 2 TB SSD (warm), 10 TB object storage (cold)
- Network: 10 Gbps

### Software Requirements

- **Kubernetes**: 1.28+
- **Helm**: 3.12+ (optional)
- **kubectl**: Match Kubernetes version
- **Prometheus**: 2.45+ (for monitoring)
- **Grafana**: 10.0+ (for dashboards)
- **Redis**: 7.0+ (for L2 cache)

### External Services

- **S3-compatible object storage** (AWS S3, Cloudflare R2, MinIO)
- **JWT auth service** (or use built-in fallback)
- **Mahavishnu MCP** (for workflow orchestration, optional)

---

## Architecture Overview

### System Components

```
┌─────────────────────────────────────────────────────────────┐
│                      Kubernetes Cluster                      │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌──────────────────┐  ┌──────────────────┐                │
│  │  Ingestion Pods  │  │   Query Pods     │                │
│  │  (Horizontal     │  │   (Horizontal    │                │
│  │   Pod Autoscaler)│  │   Pod Autoscaler)│                │
│  └──────────────────┘  └──────────────────┘                │
│           │                      │                          │
│           └──────────┬───────────┘                          │
│                      ▼                                      │
│         ┌─────────────────────────┐                        │
│         │  Hot Store (DuckDB)     │                        │
│         │  + Redis L1/L2 Cache    │                        │
│         └─────────────────────────┘                        │
│                      │                                      │
│                      ▼                                      │
│         ┌─────────────────────────┐                        │
│         │  Warm Store (DuckDB)    │                        │
│         │  (NVMe SSD)             │                        │
│         └─────────────────────────┘                        │
│                      │                                      │
│                      ▼                                      │
│         ┌─────────────────────────┐                        │
│         │  Cold Store (S3/R2)     │                        │
│         └─────────────────────────┘                        │
│                                                               │
│  ┌──────────────────┐  ┌──────────────────┐                │
│  │  Aging Service  │  │   Metrics API    │                │
│  │  (CronJob)       │  │   (/metrics)     │                │
│  └──────────────────┘  └──────────────────┘                │
└─────────────────────────────────────────────────────────────┘
```

### Resource Allocation (100 systems)

| Component | Replicas | CPU | Memory | Storage |
|-----------|----------|-----|--------|---------|
| Ingestion pods | 10 | 2 | 4 GiB | - |
| Query pods | 6 | 2 | 4 GiB | - |
| Hot store | 1 | 4 | 16 GiB | 500 GB |
| Warm store | 1 | 2 | 8 GiB | 2 TB |
| Redis cache | 3 | 1 | 2 GiB | 50 GB |
| Metrics exporter | 1 | 0.5 | 1 GiB | - |

**Total**: ~38 vCPU, ~110 GiB RAM, ~2.5 TB storage

---

## Environment Setup

### 1. Clone Repository

```bash
git clone https://github.com/yourusername/akosha.git
cd akosha
```

### 2. Create Namespace

```bash
kubectl create namespace akosha
kubectl config set-context --current --namespace=akosha
```

### 3. Create Secrets

```bash
# S3/R2 credentials
kubectl create secret generic akosha-storage-credentials \
  --from-literal=access-key-id='YOUR_ACCESS_KEY' \
  --from-literal=secret-access-key='YOUR_SECRET_KEY' \
  --from-literal=bucket='akosha-cold-data' \
  --from-literal=endpoint='https://YOUR_ENDPOINT' \
  --from-literal=region='auto'

# JWT auth (if using external auth service)
kubectl create secret generic akosha-auth-config \
  --from-literal=auth-service-url='https://auth.example.com/verify' \
  --from-literal=jwks-url='https://auth.example.com/.well-known/jwks.json'

# Redis (if using external Redis)
kubectl create secret generic akosha-redis-config \
  --from-literal=redis-host='redis.cache.local' \
  --from-literal=redis-port='6379' \
  --from-literal=redis-password='YOUR_REDIS_PASSWORD'
```

### 4. Create ConfigMaps

```bash
kubectl apply -f kubernetes/configmap.yaml
```

---

## Kubernetes Deployment

### 1. Deploy Core Components

```bash
# Deploy all Akosha components
kubectl apply -f kubernetes/

# Verify deployment
kubectl get pods -n akosha
kubectl get services -n akosha
kubectl get deployments -n akosha
```

Expected output:
```
NAME                          READY   STATUS    RESTARTS   AGE
akosha-ingestion-xxx-xxx      1/1     Running   0          2m
akosha-query-xxx-xxx          1/1     Running   0          2m
akosha-hot-store-xxx-xxx      1/1     Running   0          2m
akosha-warm-store-xxx-xxx     1/1     Running   0          2m
akosha-aging-xxx-xxx          1/1     Running   0          2m
```

### 2. Deploy Redis (if not external)

```bash
# Deploy Redis cluster
kubectl apply -f kubernetes/redis/

# Verify
kubectl get pods -l app=redis
```

### 3. Deploy Monitoring Stack

```bash
# Deploy Prometheus
kubectl apply -f kubernetes/prometheus/

# Deploy Grafana
kubectl apply -f kubernetes/grafana/

# Import dashboards
kubectl apply -f kubernetes/grafana/dashboards/
```

### 4. Verify Services

```bash
# Check services
kubectl get services -n akosha

# Port-forward to test locally
kubectl port-forward -n akosha svc/akosha-api 8000:8000

# Test API
curl http://localhost:8000/health
curl http://localhost:8000/metrics
```

---

## Configuration

### Environment Variables

Key configuration via `kubernetes/configmap.yaml`:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: akosha-config
  namespace: akosha
data:
  # Storage Paths
  AKOSHA_HOT_PATH: "/data/hot"
  AKOSHA_WARM_PATH: "/data/warm"

  # Ingestion Configuration
  AKOSHA_MAX_CONCURRENT_INGESTS: "10"
  AKOSHA_INGESTION_TIMEOUT: "300"

  # Query Configuration
  AKOSHA_QUERY_TIMEOUT: "30"
  AKOSHA_MAX_RESULTS: "100"

  # Cache Configuration
  AKOSHA_CACHE_ENABLED: "true"
  AKOSHA_L1_CACHE_SIZE: "1000"
  AKOSHA_L2_CACHE_ENABLED: "true"

  # Tier Migration
  AKOSHA_HOT_TIER_DAYS: "7"
  AKOSHA_WARM_TIER_DAYS: "90"
  AKOSHA_AGING_SCHEDULE: "0 2 * * *"  # 2 AM daily

  # Monitoring
  AKOSHA_METRICS_ENABLED: "true"
  AKOSHA_LOG_LEVEL: "INFO"
```

### Horizontal Pod Autoscaling

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: akosha-ingestion-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: akosha-ingestion
  minReplicas: 2
  maxReplicas: 20
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
  - type: Resource
    resource:
      name: memory
      target:
        type: Utilization
        averageUtilization: 80
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 60
      policies:
      - type: Percent
        value: 50
        periodSeconds: 60
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
      - type: Percent
        value: 10
        periodSeconds: 60
```

---

## Monitoring & Observability

### Prometheus Metrics

Akosha exposes metrics at `/metrics` endpoint:

**Ingestion Metrics:**
- `akosha_ingestion_requests_total` - Total ingestion requests
- `akosha_ingestion_duration_seconds` - Request duration histogram
- `akosha_ingestion_queue_size` - Current queue size

**Query Metrics:**
- `akosha_query_requests_total` - Total query requests
- `akosha_query_duration_seconds` - Query latency (P50, P95, P99)
- `akosha_query_cache_hits_total` - Cache hits by level

**Storage Metrics:**
- `akosha_hot_store_size_bytes` - Hot store size
- `akosha_warm_store_size_bytes` - Warm store size
- `akosha_migration_records_total` - Migration records

### Grafana Dashboards

Three dashboards are provided:

1. **Ingestion Dashboard** (`monitoring/dashboards/ingestion.json`)
   - Uploads per minute
   - P50/P99 latency
   - Queue size
   - Error rate
   - Success rate

2. **Query Dashboard** (`monitoring/dashboards/query.json`)
   - Queries per second
   - Latency distribution
   - Cache hit rate
   - Average result count
   - Shard health

3. **Storage Dashboard** (`monitoring/dashboards/storage.json`)
   - Hot/Warm/Cold store sizes
   - Migration throughput
   - Storage cost estimate
   - Storage breakdown (pie chart)

### Alerting Rules

Critical alerts are configured in `monitoring/alerts.yaml`:

**Critical Alerts:**
- `HighIngestionBacklog` - Queue > 1000 for 5 min
- `HotStoreSizeCritical` - Hot store > 100 GB for 10 min
- `HighQueryLatency` - P99 > 2s for 5 min

**Warning Alerts:**
- `HotStoreSizeWarning` - Hot store > 50 GB for 15 min
- `QueryLatencyDegradation` - P99 > 1s for 5 min
- `LowIngestionSuccessRate` - Success rate < 95%
- `LowCacheHitRate` - L1 cache hit rate < 30%

---

## Security Hardening

### Authentication & Authorization

Akosha implements JWT-based authentication with RBAC:

**Roles:**
- `admin` - Full access (ingest, query, delete, migrate, settings)
- `operator` - Operational access (ingest, query, status)
- `viewer` - Read-only access (query, status)

**Setup:**

```python
from fastapi import Depends
from akosha.api.middleware import verify_token, require_permission

@app.post("/ingest/upload")
async def upload(
    claims: dict = Depends(verify_token),
    _ = Depends(require_permission("ingest:upload"))
):
    # Your upload logic
    return {"status": "success"}
```

### Network Policies

Restrict network access with Kubernetes NetworkPolicies:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: akosha-network-policy
spec:
  podSelector:
    matchLabels:
      app: akosha
  policyTypes:
  - Ingress
  - Egress
  ingress:
  - from:
    - namespaceSelector:
        matchLabels:
          name: ingress-nginx  # Allow from ingress
    ports:
    - protocol: TCP
      port: 8000
  egress:
  - to:
    - namespaceSelector:
        matchLabels:
          name: kube-system
    ports:
    - protocol: TCP
      port: 53  # DNS
  - to: []  # Allow all egress for S3, Redis, etc.
```

### Pod Security

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: akosha-ingestion
spec:
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    fsGroup: 1000
    seccompProfile:
      type: RuntimeDefault
  containers:
  - name: ingestion
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities:
        drop:
        - ALL
```

---

## Scaling Guidelines

### Vertical Scaling (Resource Limits)

**Ingestion Pods:**
```yaml
resources:
  requests:
    cpu: "1"
    memory: "2Gi"
  limits:
    cpu: "2"
    memory: "4Gi"
```

**Query Pods:**
```yaml
resources:
  requests:
    cpu: "500m"
    memory: "1Gi"
  limits:
    cpu: "2"
    memory: "4Gi"
```

### Horizontal Scaling (HPA)

**Scaling Metrics:**
- CPU utilization: Scale up at 70%, scale down at 30%
- Memory utilization: Scale up at 80%, scale down at 50%
- Custom metric: `akosha_ingestion_queue_size` (scale up at 500)

**Scale-Up Strategy:**
- Stabilization window: 60 seconds
- Max scale-up: 50% per minute
- Max pods: 20 (ingestion), 10 (query)

**Scale-Down Strategy:**
- Stabilization window: 300 seconds (5 minutes)
- Max scale-down: 10% per minute
- Min pods: 2 (ingestion), 2 (query)

### Scaling for Growth Phases

| Phase | Systems | Ingestion Pods | Query Pods | Hot Store | Warm Store |
|-------|---------|----------------|------------|-----------|------------|
| Pilot | 10 | 2 | 2 | 100 GB | 500 GB |
| Phase 1 | 100 | 10 | 6 | 500 GB | 2 TB |
| Phase 2 | 1,000 | 20 | 10 | 2 TB | 10 TB |
| Phase 3 | 10,000 | 50 | 20 | 10 TB | 50 TB |

---

## Troubleshooting

### Common Issues

#### 1. High Ingestion Backlog

**Symptoms:**
- `akosha_ingestion_queue_size` > 1000
- Uploads taking > 5 minutes

**Diagnosis:**
```bash
# Check queue size
kubectl exec -n akosha akosha-ingestion-xxx -- curl localhost:8000/metrics | grep queue_size

# Check pod resource usage
kubectl top pods -n akosha -l app=akosha-ingestion
```

**Solutions:**
1. Scale up ingestion pods: `kubectl scale deployment akosha-ingestion --replicas=10`
2. Increase `AKOSHA_MAX_CONCURRENT_INGESTS`
3. Check hot store performance (disk I/O)

#### 2. High Query Latency

**Symptoms:**
- P99 latency > 2 seconds
- Slow search responses

**Diagnosis:**
```bash
# Check cache hit rate
kubectl exec -n akosha akosha-query-xxx -- curl localhost:8000/metrics | grep cache_hit

# Check hot store size
kubectl exec -n akosha akosha-hot-store-xxx -- curl localhost:8000/metrics | grep hot_store_size
```

**Solutions:**
1. Check hot store size (> 100 GB triggers aging)
2. Enable cache warming
3. Scale up query pods
4. Check Redis connectivity

#### 3. Hot Store Too Large

**Symptoms:**
- `akosha_hot_store_size_bytes` > 100 GB
- Alert: `HotStoreSizeCritical`

**Diagnosis:**
```bash
# Check aging service status
kubectl get cronjobs -n akosha
kubectl logs -n akosha -l app=akosha-aging --tail=100
```

**Solutions:**
1. Trigger manual aging: `kubectl create job --from=cronjob/akosha-aging manual-aging-$(date +%s)`
2. Check aging service logs for errors
3. Verify warm store has sufficient space

#### 4. Pod CrashLoopBackOff

**Symptoms:**
- Pods restarting repeatedly
- Status: `CrashLoopBackOff`

**Diagnosis:**
```bash
# Check pod logs
kubectl logs -n akosha akosha-ingestion-xxx --previous

# Describe pod for events
kubectl describe pod -n akosha akosha-ingestion-xxx
```

**Common Causes:**
- Missing secrets/configmaps
- Insufficient resources (OOMKilled)
- Database connection failures
- Invalid configuration

---

## Runbook

### Daily Operations

**Morning Checklist (9 AM):**
1. Check Grafana dashboards for anomalies
2. Review Prometheus alerts (last 24 hours)
3. Verify pod health: `kubectl get pods -n akosha`
4. Check error rates in logs

**Weekly Tasks (Monday):**
1. Review storage trends (hot/warm/cold)
2. Check cache hit rates
3. Validate backup integrity
4. Review cost projections

### Incident Response

**Severity 1 (Critical):**
- Response time: < 15 minutes
- Examples: Complete outage, data loss
- Escalation: On-call engineer → Engineering manager → CTO

**Severity 2 (High):**
- Response time: < 1 hour
- Examples: High latency, partial degradation
- Escalation: On-call engineer → Tech lead

**Severity 3 (Medium):**
- Response time: < 4 hours
- Examples: Single pod failure, elevated error rate
- Escalation: On-call engineer

### Rollback Procedures

**Deployment Rollback:**
```bash
# Check rollout history
kubectl rollout history deployment/akosha-ingestion -n akosha

# Rollback to previous version
kubectl rollout undo deployment/akosha-ingestion -n akosha

# Verify rollback
kubectl rollout status deployment/akosha-ingestion -n akosha
```

**Emergency Rollback:**
```bash
# Scale to zero (emergency stop)
kubectl scale deployment/akosha-ingestion --replicas=0 -n akosha

# Restore from backup (if needed)
kubectl apply -f kubernetes/backups/akosha-ingestion-v1.2.3.yaml
```

---

## Next Steps

1. **Pilot Deployment**: Start with 10 systems for 2 weeks
2. **Monitor Closely**: Check dashboards daily, review alerts
3. **Gather Metrics**: Validate SLO compliance (P50 <500ms, P99 <2s)
4. **Scale Gradually**: Add 10 systems per week until 100
5. **Optimize**: Tune cache sizes, HPA thresholds, aging schedules

**Support**: For issues or questions, contact the Akosha team at akosha@example.com

---

**Last Updated**: 2025-02-08
**Document Version**: 1.0
