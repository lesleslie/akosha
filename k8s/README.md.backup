# Akosha Kubernetes Deployment Guide

Complete guide for deploying Akosha on Kubernetes.

______________________________________________________________________

## 📋 Overview

This directory contains Kubernetes manifests for deploying Akosha MCP server with:

- **High Availability**: 2+ replicas with pod anti-affinity
- **Auto-scaling**: HorizontalPodAutoscaler (2-10 replicas)
- **Resilience**: PodDisruptionBudget, health checks, resource limits
- **Security**: Network policies, RBAC, security contexts
- **Observability**: Prometheus scraping, OpenTelemetry integration

______________________________________________________________________

## 🚀 Quick Start

### Prerequisites

- Kubernetes cluster (v1.24+)
- kubectl configured
- Storage class named "fast-ssd" (or update PVC)
- Docker image built and available

### 1. Build Docker Image

```bash
# From project root
docker build -t akosha:latest .

# Or use UV to build
uv pip install .
# Then use your preferred container build method
```

### 2. Apply Kubernetes Manifests

```bash
# Apply all manifests
kubectl apply -f k8s/

# Or apply individually
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secret.yaml
kubectl apply -f k8s/serviceaccount.yaml
kubectl apply -f k8s/pvc.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/hpa.yaml
kubectl apply -f k8s/pdb.yaml
kubectl apply -f k8s/networkpolicy.yaml
kubectl apply -f k8s/resourcequota.yaml
```

### 3. Verify Deployment

```bash
# Check pods are running
kubectl get pods -n akosha

# Check deployment status
kubectl rollout status deployment/akosha-mcp -n akosha

# Check services
kubectl get svc -n akosha

# Check HPA
kubectl get hpa -n akosha

# Port forward to test locally
kubectl port-forward svc/akosha-mcp 3002:3002 -n akosha
```

______________________________________________________________________

## 📁 Manifests Breakdown

### Core Resources

#### 1. Namespace (`namespace.yaml`)

- Creates dedicated namespace: `akosha`
- Isolates Akosha resources

#### 2. ConfigMap (`configmap.yaml`)

- Environment configuration
- Feature flags
- Resource limits
- **Edit this** to customize configuration

#### 3. Secret (`secret.yaml`)

- Sensitive data (API keys, tokens)
- Placeholder values (update for production)
- **IMPORTANT**: Update before production use!

#### 4. Deployment (`deployment.yaml`)

- 2 replicas (scales via HPA)
- Resource requests/limits:
  - Request: 250m CPU, 256Mi memory
  - Limit: 1000m CPU, 1Gi memory
- Health checks:
  - Liveness probe: `/health` (every 10s)
  - Readiness probe: `/ready` (every 5s)
  - Startup probe: `/health` (first 30s)
- Security:
  - Non-root user (UID 1000)
  - Seccomp profile
  - Read-only root filesystem
- Storage:
  - PVC for data (/data/akosha)
  - EmptyDir for cache (/tmp/akosha)
- Affinity:
  - Pod anti-affinity (spread across nodes)

#### 5. Service (`service.yaml`)

- **ClusterIP**: Internal access (port 3002)
- **NodePort**: External access (port 30302)
- Both services expose:
  - MCP protocol (port 3002)
  - Prometheus metrics (port 3002)

#### 6. HPA (`hpa.yaml`)

- Min replicas: 2
- Max replicas: 10
- Scaling metrics:
  - CPU: Scale at 70% utilization
  - Memory: Scale at 80% utilization
- Scaling behavior:
  - Scale down slowly (5min stabilization)
  - Scale up quickly (60s stabilization)

#### 7. PDB (`pdb.yaml`)

- Ensures minimum availability during updates
- Min available: 1 pod
- Allows 1 pod to be down during rolling updates

#### 8. PVC (`pvc.yaml`)

- Data PVC: 10Gi (for embeddings and storage)
- Cache PVC: 5Gi (for temporary data)
- Uses storage class "fast-ssd" (customize as needed)

#### 9. ServiceAccount + RBAC (`serviceaccount.yaml`)

- Service account: `akosha-sa`
- Role: Read ConfigMaps/Secrets
- RoleBinding: Binds role to service account

#### 10. NetworkPolicy (`networkpolicy.yaml`)

- Ingress: Allow TCP on port 3002
- Egress:
  - DNS (UDP 53)
  - HTTPS (443)
  - HTTP (80)
- **Customize** for your security requirements

#### 11. ResourceQuota (`resourcequota.yaml`)

- Namespace limits:
  - CPU: 4 requests, 10 limits
  - Memory: 8Gi requests, 20Gi limits
  - PVCs: 5 total
  - Pods: 20 total
- LimitRange:
  - Container defaults: 500m CPU, 512Mi memory
  - Min: 100m CPU, 128Mi memory
  - Max: 2000m CPU, 2Gi memory

______________________________________________________________________

## 🔧 Configuration

### Customizing ConfigMap

Edit `k8s/configmap.yaml` to change:

```yaml
ENVIRONMENT: "production"          # or "development"
LOG_LEVEL: "INFO"                  # DEBUG, INFO, WARNING, ERROR
STORAGE_PATH: "/data/akosha"       # Data directory
EMBEDDING_MODEL: "all-MiniLM-L6-v2" # Model name
MCP_PORT: "3002"                    # Server port
```

### Customizing Resources

Edit `k8s/deployment.yaml` to adjust:

```yaml
resources:
  requests:
    memory: "256Mi"   # Increase for higher load
    cpu: "250m"
  limits:
    memory: "1Gi"     # Adjust based on testing
    cpu: "1000m"
```

### Customizing HPA

Edit `k8s/hpa.yaml` to adjust:

```yaml
minReplicas: 2        # Minimum pods
maxReplicas: 10      # Maximum pods
```

### Customizing Storage

Edit `k8s/pvc.yaml` to adjust:

```yaml
resources:
  requests:
    storage: 10Gi    # Increase for more data
```

______________________________________________________________________

## 📊 Scaling Behavior

### Horizontal Pod Autoscaler

The HPA automatically scales based on CPU/memory usage:

| Metric | Target | Action |
|--------|--------|--------|
| CPU > 70% | Scale up | Add pods (max +2 per 30s) |
| CPU < 35% | Scale down | Remove pods (max -1 per 5min) |
| Memory > 80% | Scale up | Add pods |
| Memory < 40% | Scale down | Remove pods |

### Manual Scaling

```bash
# Scale to 5 replicas
kubectl scale deployment/akosha-mcp --replicas=5 -n akosha

# Edit HPA min/max
kubectl edit hpa akosha-mcp-hpa -n akosha
```

______________________________________________________________________

## 🔍 Monitoring

### Prometheus Integration

The deployment includes Prometheus scraping:

```yaml
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "3002"
  prometheus.io/path: "/metrics"
```

**Prometheus targets**:

- `http://akosha-mcp.akosha.svc:3002/metrics`

**Available metrics**:

- Embedding generation rate
- Circuit breaker states
- Analytics operations
- Knowledge graph size
- See `monitoring/README.md` for full list

### Logs

```bash
# View logs
kubectl logs -f deployment/akosha-mcp -n akosha

# View logs from specific pod
kubectl logs -f pod/akosha-mcp-xxxxx -n akosha
```

### Health Checks

```bash
# Check pod health
kubectl describe pod -n akosha

# Check endpoints
kubectl get endpoints akosha-mcp -n akosha
```

______________________________________________________________________

## 🛡️ Security

### Network Policies

The default network policy allows:

- ✅ Inbound TCP on port 3002
- ✅ DNS resolution
- ✅ HTTPS/HTTP outbound

**To restrict access**, edit `k8s/networkpolicy.yaml`:

```yaml
ingress:
  - from:
    - podSelector:
        matchLabels:
          app: allowed-app
    ports:
    - protocol: TCP
      port: 3002
```

### RBAC

The deployment includes:

- Service account: `akosha-sa`
- Role: Read ConfigMaps/Secrets
- RoleBinding: Binds role to SA

**To add permissions**, edit `k8s/serviceaccount.yaml`:

```yaml
rules:
- apiGroups: [""]
  resources: ["configmaps", "secrets", "pods"]
  verbs: ["get", "list", "watch"]
```

### Security Contexts

Pod security features:

- ✅ Run as non-root user (UID 1000)
- ✅ Read-only root filesystem
- ✅ Seccomp profile (RuntimeDefault)
- ✅ Drop all capabilities (minimal container)

______________________________________________________________________

## 📈 Production Checklist

### Before Deploying to Production

- [ ] Update `secret.yaml` with actual values
- [ ] Configure appropriate storage class
- [ ] Set resource limits based on load testing
- [ ] Configure network policies for your environment
- [ ] Set up monitoring and alerting
- [ ] Configure backup strategy (PVC snapshots)
- [ ] Test disaster recovery procedures

### Post-Deployment Verification

- [ ] Verify all pods are Running
- [ ] Verify HPA is functioning
- [ ] Test rolling update: `kubectl rollout status deployment/akosha-mcp -n akosha`
- [ ] Test pod disruption budget
- [ ] Verify metrics are being scraped
- [ ] Test scaling behavior
- [ ] Verify log aggregation

______________________________________________________________________

## 🚀 Deployment Strategies

### Rolling Update (Zero Downtime)

```bash
# Update image
kubectl set image deployment/akosha-mcp akosha-mcp=akosha:v2.0 -n akosha

# Watch rollout status
kubectl rollout status deployment/akosha-mcp -n akosha

# Rollback if needed
kubectl rollout undo deployment/akosha-mcp -n akosha
```

### Canary Deployment

```bash
# Create canary deployment
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/deployment-canary.yaml

# Split traffic between deployments
# (requires service mesh orIstio)
```

### Blue-Green Deployment

```bash
# Deploy green environment
kubectl apply -f k8s/deployment-green.yaml

# Switch service to green
kubectl patch svc akosha-mcp -n akosha -p '{"spec":{"selector":{"app":"akosha-mcp-green"}}}'
```

______________________________________________________________________

## 🔧 Troubleshooting

### Pods Not Starting

```bash
# Describe pod for details
kubectl describe pod -n akosha

# Check logs
kubectl logs -n akosha <pod-name>

# Check events
kubectl get events -n akosha --sort-by='.lastTimestamp'
```

### HPA Not Scaling

```bash
# Check HPA status
kubectl describe hpa akosha-mcp-hpa -n akosha

# Check resource usage
kubectl top pods -n akosha

# Verify metrics server is running
kubectl get apiservice | grep metrics
```

### Storage Issues

```bash
# Check PVC status
kubectl get pvc -n akosha

# Check PV binding
kubectl describe pvc akosha-data-pvc -n akosha

# Verify storage class
kubectl get storageclass
```

### Network Issues

```bash
# Test network policy
kubectl run -it --rm debug --image=nicolaka/netshoot -n akosha

# Check service endpoints
kubectl get endpoints akosha-mcp -n akosha

# Test connectivity
kubectl run -it --rm debug --image=curlimages/curl -n akosha -- curl http://akosha-mcp:3002/health
```

______________________________________________________________________

## 📚 Additional Resources

- [Kubernetes Documentation](https://kubernetes.io/docs/)
- [Prometheus on Kubernetes](https://prometheus.io/docs/prometheus/latest/installation/)
- [OpenTelemetry Kubernetes](https://opentelemetry.io/docs/instrumentation/kubernetes/)
- [HPA Guidelines](https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/)

______________________________________________________________________

## 🎯 Quick Reference

### Essential Commands

```bash
# Deploy everything
kubectl apply -f k8s/

# Check status
kubectl get all -n akosha

# Scale deployment
kubectl scale deployment/akosha-mcp --replicas=5 -n akosha

# View logs
kubectl logs -f deployment/akosha-mcp -n akosha

# Port forward for testing
kubectl port-forward svc/akosha-mcp 3002:3002 -n akosha

# Delete deployment
kubectl delete -f k8s/
```

### Accessing the Service

```bash
# From within cluster
http://akosha-mcp.akosha.svc:3002

# From outside (via NodePort)
http://<node-ip>:30302

# Via port forward
http://localhost:3002
```

______________________________________________________________________

**Deploy Akosha on Kubernetes with confidence!** 🚀
