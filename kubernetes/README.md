# Akosha Kubernetes Manifests

**Version**: 0.3.0
**Namespace**: `akosha`
**Target Scale**: 100 Session-Buddy systems

## Overview

This directory contains Kubernetes manifests for deploying Akosha to a production cluster. The manifests are organized by component and follow Kubernetes best practices.

## Components

| Component | Type | Replicas | CPU | Memory | Storage |
|-----------|------|----------|-----|--------|---------|
| **Hot Store** | StatefulSet | 1 | 2-4 | 8-16 GiB | 500 GB |
| **Warm Store** | StatefulSet | 1 | 1-2 | 4-8 GiB | 2 TB |
| **Ingestion** | Deployment | 2-20 (HPA) | 1-2 | 2-4 GiB | - |
| **Query** | Deployment | 2-10 (HPA) | 0.5-2 | 1-4 GiB | - |
| **Aging** | CronJob | - | 1-2 | 2-4 GiB | - |

## Quick Start

### 1. Prerequisites

- Kubernetes cluster 1.28+
- kubectl configured
- Storage classes: `fast-ssd` (NVMe), `ssd` (standard)
- cert-manager installed (for TLS)
- nginx-ingress controller

### 2. Update Secrets

Edit `secrets.yaml` and update with your actual values:

```bash
# Edit secrets
nano kubernetes/secrets.yaml

# Replace placeholders:
# - YOUR_ACCESS_KEY_ID
# - YOUR_SECRET_ACCESS_KEY
# - YOUR_JWT_SECRET_HERE
# - YOUR_REDIS_PASSWORD
# - YOUR_MAHAVISHNU_API_KEY
```

### 3. Deploy All Components

```bash
# Create namespace and secrets
kubectl apply -f kubernetes/namespace.yaml
kubectl apply -f kubernetes/secrets.yaml

# Deploy configmap
kubectl apply -f kubernetes/configmap.yaml

# Deploy storage (StatefulSets)
kubectl apply -f kubernetes/hot-store.yaml
kubectl apply -f kubernetes/warm-store.yaml

# Wait for stores to be ready
kubectl wait --for=condition=ready pod -l app=akosha-hot-store -n akosha --timeout=300s
kubectl wait --for=condition=ready pod -l app=akosha-warm-store -n akosha --timeout=300s

# Deploy services
kubectl apply -f kubernetes/services.yaml

# Deploy applications
kubectl apply -f kubernetes/ingestion.yaml
kubectl apply -f kubernetes/query.yaml
kubectl apply -f kubernetes/aging.yaml

# Deploy ingress and network policies
kubectl apply -f kubernetes/ingress.yaml
kubectl apply -f kubernetes/network-policy.yaml
```

### 4. Verify Deployment

```bash
# Check all pods
kubectl get pods -n akosha

# Expected output:
# NAME                                  READY   STATUS    RESTARTS   AGE
# akosha-hot-store-0                    1/1     Running   0          2m
# akosha-warm-store-0                   1/1     Running   0          2m
# akosha-ingestion-xxx-xxx              1/1     Running   0          1m
# akosha-query-xxx-xxx                  1/1     Running   0          1m

# Check services
kubectl get services -n akosha

# Check HPA
kubectl get hpa -n akosha

# Check ingress
kubectl get ingress -n akosha
```

### 5. Test API

```bash
# Port-forward to test locally
kubectl port-forward -n akosha svc/akosha-api 8000:8000

# Test health endpoint
curl http://localhost:8000/health

# Test metrics endpoint
curl http://localhost:8000/metrics
```

## Configuration

### ConfigMap Options

Edit `configmap.yaml` to customize:

- **Storage paths**: Hot/Warm tier locations
- **Ingestion settings**: Max concurrent uploads, timeout
- **Query settings**: Max results, timeout
- **Cache settings**: L1/L2 cache sizes and TTLs
- **Tier migration**: Aging schedule, retention days
- **Performance**: Shard count, HNSW parameters

### Scaling

**Horizontal Pod Autoscaling (HPA)**:

```bash
# Check current HPA status
kubectl get hpa -n akosha

# Manually scale (if needed)
kubectl scale deployment akosha-ingestion --replicas=5 -n akosha

# Edit HPA settings
kubectl edit hpa akosha-ingestion-hpa -n akosha
```

**Vertical Scaling**:

Edit deployment manifests to adjust resource requests/limits:

```yaml
resources:
  requests:
    cpu: "2"      # Increase for more CPU
    memory: "4Gi" # Increase for more memory
  limits:
    cpu: "4"
    memory: "8Gi"
```

### Storage

**Resize PVCs** (if storage class supports expansion):

```bash
# Edit PVC
kubectl edit pvc akosha-hot-store-pvc -n akosha

# Change spec.resources.requests.storage
# Wait for resize to complete
kubectl get pvc -n akosha
```

## Monitoring

### Prometheus Scrape Config

Add to your Prometheus scrape config:

```yaml
scrape_configs:
  - job_name: 'akosha'
    kubernetes_namespaces:
      names:
        - akosha
    relabel_configs:
      - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape]
        action: keep
        regex: true
      - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_path]
        action: replace
        target_label: __metrics_path__
        regex: (.+)
      - source_labels: [__address__, __meta_kubernetes_pod_annotation_prometheus_io_port]
        action: replace
        regex: ([^:]+)(?::\d+)?;(\d+)
        replacement: $1:$2
        target_label: __address__
```

### Grafana Dashboards

Import dashboards from `monitoring/dashboards/`:

1. Login to Grafana
1. Go to Dashboards → Import
1. Upload JSON files:
   - `ingestion.json`
   - `query.json`
   - `storage.json`

## Troubleshooting

### Pod Not Starting

```bash
# Describe pod
kubectl describe pod akosha-ingestion-xxx -n akosha

# Check logs
kubectl logs akosha-ingestion-xxx -n akosha

# Check previous logs (if crashed)
kubectl logs akosha-ingestion-xxx -n akosha --previous
```

### Storage Issues

```bash
# Check PVC status
kubectl get pvc -n akosha

# Check PV status
kubectl get pv

# Describe storage class
kubectl describe storageclass fast-ssd
```

### HPA Not Scaling

```bash
# Check HPA status
kubectl describe hpa akosha-ingestion-hpa -n akosha

# Check metrics server
kubectl top pods -n akosha

# Check if custom metrics are available
kubectl get --raw /apis/custom.metrics.k8s.io/v1beta1 | jq .
```

### Network Issues

```bash
# Check network policies
kubectl get networkpolicies -n akosha

# Test pod-to-pod connectivity
kubectl exec -n akosha akosha-query-xxx -- curl http://akosha-hot-store:8002/health

# Check DNS resolution
kubectl exec -n akosha akosha-query-xxx -- nslookup akosha-hot-store
```

## Upgrade Procedure

### Rolling Update

```bash
# Update image tag in deployments
kubectl set image deployment/akosha-ingestion ingestion=akosha:0.4.0 -n akosha
kubectl set image deployment/akosha-query query=akosha:0.4.0 -n akosha

# Watch rollout status
kubectl rollout status deployment/akosha-ingestion -n akosha
kubectl rollout status deployment/akosha-query -n akosha
```

### Rollback

```bash
# Check rollout history
kubectl rollout history deployment/akosha-ingestion -n akosha

# Rollback to previous version
kubectl rollout undo deployment/akosha-ingestion -n akosha

# Rollback to specific revision
kubectl rollout undo deployment/akosha-ingestion --to-revision=2 -n akosha
```

## Security

### Network Policies

Network policies restrict pod-to-pod communication:

- **Query pods**: Only accept from ingress controller, monitoring
- **Ingestion pods**: Only accept from monitoring
- **All pods**: Egress only to DNS, stores, Redis, S3

### Pod Security

All pods run with:

- Non-root user (UID 1000)
- No privilege escalation
- Dropped capabilities (all)
- Read-only root filesystem (except where needed)

### Secrets

- S3 credentials in `akosha-storage-credentials`
- JWT config in `akosha-auth-config`
- Redis password in `akosha-redis-config`
- Mahavishnu API key in `akosha-mahavishnu-config`

**Never commit secrets to git!**

## Backup and Restore

### Backup Hot Store

```bash
# kubectl exec to run backup
kubectl exec -n akosha akosha-hot-store-0 -- \
  python -c "
from akosha.storage.hot_store import HotStore
import asyncio

async def backup():
    store = HotStore()
    await backup_to_s3('akosha-backups', f'hot-{datetime.now().isoformat()}.db')

asyncio.run(backup())
"
```

### Restore Hot Store

```bash
# Download backup
kubectl cp akosha-backups/hot-backup.db \
  akosha/akosha-hot-store-0:/tmp/hot-backup.db

# Restore from backup
kubectl exec -n akosha akosha-hot-store-0 -- \
  duckdb /data/hot/hot.db "IMPORT DATABASE '/tmp/hot-backup.db';"
```

## Cleanup

```bash
# Delete all Akosha resources
kubectl delete namespace akosha

# Or delete specific components
kubectl delete -f kubernetes/
```

## Production Checklist

Before deploying to production:

- [ ] Update all secrets with production values
- [ ] Configure TLS certificates (cert-manager or manual)
- [ ] Set up monitoring (Prometheus + Grafana)
- [ ] Configure alerting rules
- [ ] Test disaster recovery procedures
- [ ] Set up log aggregation (ELK/Loki)
- [ ] Configure backup schedules
- [ ] Run load tests to validate capacity
- [ ] Document runbook and escalation procedures
- [ ] Set up on-call rotations

## Support

For issues or questions:

- **Documentation**: [../docs/DEPLOYMENT_GUIDE.md](../docs/DEPLOYMENT_GUIDE.md)
- **Issues**: https://github.com/yourusername/akosha/issues
- **Email**: akosha@example.com

______________________________________________________________________

**Last Updated**: 2025-02-08
