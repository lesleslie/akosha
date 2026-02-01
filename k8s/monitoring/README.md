# Akosha Production Deployment with Monitoring Stack

Complete deployment including Akosha MCP server, Prometheus, Grafana, and Jaeger.

______________________________________________________________________

## 📋 Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Kubernetes Cluster                         │
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │  Akosha MCP  │  │  Prometheus   │  │   Grafana    │  │
│  │  (2-10 pods) │  │              │  │              │  │
│  │              │  │  Scrapes      │  │  Dashboards   │  │
│  │  Port: 3002  │  │  metrics      │  │              │  │
│  └──────────────┘  └──────────────┘  └──────────────┘  │
│         │                                                │
│  ┌──────────────┐  ┌──────────────┐                        │
│  │   Jaeger    │  │  AlertManager│                        │
│  │ (Tracing)    │  │  (Alerts)    │                        │
│  │  Port: 16686│  │  Port: 9093  │                        │
│  └──────────────┘  └──────────────┘                        │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

______________________________________________________________________

## 🚀 Quick Start

### 1. Deploy Complete Stack

```bash
# Deploy Akosha with monitoring
kubectl apply -f k8s/monitoring/
```

### 2. Access Services

```bash
# Port forward all services
kubectl port-forward -n akosha svc/akosha-mcp 3002:3002 &
kubectl port-forward -n akosha svc/prometheus 9090:9090 &
kubectl port-forward -n akosha svc/grafana 3001:3000 &
kubectl port-forward -n akosha svc/jaeger 16686:16686 &

# Access URLs:
# Akosha MCP: http://localhost:3002
# Prometheus: http://localhost:9090
# Grafana: http://localhost:3001 (admin/admin)
# Jaeger UI: http://localhost:16686
```

______________________________________________________________________

## 📁 Monitoring Stack Files

### Prometheus (`k8s/monitoring/prometheus.yaml`)

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: prometheus-config
  namespace: akosha
data:
  prometheus.yml: |
    global:
      scrape_interval: 15s
      evaluation_interval: 15s

    rule_files:
      - "/etc/prometheus/alerts.yml"

    scrape_configs:
      # Akosha MCP metrics
      - job_name: 'akosha-mcp'
        kubernetes_sd_configs:
          - role: pod
            namespaces:
              names:
                - akosha
        relabel_configs:
          - source_labels: [__meta_kubernetes_pod_label_app]
            regex: 'akosha-mcp'
            action: keep
          - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape]
            regex: 'true'
            action: keep
          - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_port]
            target_label: __metrics_port__
          - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_path]
            target_label: __metrics_path__

      # Prometheus self-monitoring
      - job_name: 'prometheus'
        static_configs:
          - targets: ['localhost:9090']

    alerting:
      alertmanagers:
        - static_configs:
            - targets:
              - alertmanager:9093
```

______________________________________________________________________

### Grafana (`k8s/monitoring/grafana.yaml`)

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: grafana-dashboards
  namespace: akosha
data:
  akosha-dashboard.json: |
    {
      "dashboard": {
        "title": "Akosha Overview",
        "panels": [
          {
            "title": "System Health",
            "targets": [
              {
                "expr": "up{job=\"akosha-mcp\"}"
              }
            ]
          }
        ]
      }
    }
```

**Provisioning dashboards**:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: grafana-provisioning
  namespace: akosha
data:
  provisioning.yml: |
    apiVersion: 1

    providers:
      - name: 'Prometheus'
        orgId: 1
        type: prometheus
        disableDeletion: false
        editable: true
        jsonData:
          httpMethod: POST
      - name: 'Grafana'
        type: grafana
        disableDeletion: false
        editable: false
```

______________________________________________________________________

### Jaeger (`k8s/monitoring/jaeger.yaml`)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: jaeger
  namespace: akosha
  labels:
    app: jaeger
spec:
  replicas: 1
  selector:
    matchLabels:
      app: jaeger
  template:
    metadata:
      labels:
        app: jaeger
    spec:
      containers:
      - name: jaeger
        image: jaegertracing/all-in-one:latest
        ports:
        - name: ui
          containerPort: 16686
          protocol: TCP
        - name: otlp
          containerPort: 4317
          protocol: TCP
        - name: otlp-http
          containerPort: 4318
          protocol: TCP
        env:
        - name: COLLECTOR_OTLP_ENABLED
          value: "true"
        resources:
          requests:
            memory: "512Mi"
            cpu: "250m"
          limits:
            memory: "1Gi"
            cpu: "500m"
---
apiVersion: v1
kind: Service
metadata:
  name: jaeger
  namespace: akosha
spec:
  selector:
    app: jaeger
  ports:
  - name: ui
    port: 16686
    targetPort: 16686
  - name: otlp
    port: 4317
    targetPort: 4317
  - name: otlp-http
    port: 4318
    targetPort: 4318
  type: ClusterIP
```

______________________________________________________________________

### AlertManager (`k8s/monitoring/alertmanager.yaml`)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: alertmanager
  namespace: akosha
  labels:
    app: alertmanager
spec:
  replicas: 1
  selector:
    matchLabels:
      app: alertmanager
  template:
    metadata:
      labels:
        app: alertmanager
    spec:
      containers:
      - name: alertmanager
        image: prom/alertmanager:latest
        ports:
        - containerPort: 9093
        args:
          - '--config.file=/etc/alertmanager/alertmanager.yml'
        volumeMounts:
        - name: config
          mountPath: /etc/alertmanager
        resources:
          requests:
            memory: "128Mi"
            cpu: "100m"
          limits:
            memory: "256Mi"
            cpu: "200m"
      volumes:
      - name: config
        configMap:
          name: alertmanager-config
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: alertmanager-config
  namespace: akosha
data:
  alertmanager.yml: |
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
          receiver: 'critical'
        - match:
            severity: warning
          receiver: 'warnings'

    receivers:
      - name: 'default'
      - name: 'critical'
      - name: 'warnings'
---
apiVersion: v1
kind: Service
metadata:
  name: alertmanager
  namespace: akosha
spec:
  selector:
    app: alertmanager
  ports:
  - port: 9093
    targetPort: 9093
  type: ClusterIP
```

______________________________________________________________________

### Prometheus Service (`k8s/monitoring/prometheus-service.yaml`)

```yaml
apiVersion: v1
kind: Service
metadata:
  name: prometheus
  namespace: akosha
spec:
  selector:
    app: prometheus
  ports:
  - port: 9090
    targetPort: 9090
  type: ClusterIP
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: prometheus
  namespace: akosha
  labels:
    app: prometheus
spec:
  replicas: 1
  selector:
    matchLabels:
      app: prometheus
  template:
    metadata:
      labels:
        app: prometheus
    spec:
      serviceAccountName: prometheus
      containers:
      - name: prometheus
        image: prom/prometheus:latest
        ports:
        - containerPort: 9090
        args:
          - '--config.file=/etc/prometheus/prometheus.yml'
          - '--storage.tsdb.retention.time=15d'
          - '--web.enable-lifecycle'
        volumeMounts:
        - name: config
          mountPath: /etc/prometheus
        - name: storage
          mountPath: /prometheus
        livenessProbe:
          httpGet:
            path: /-/healthy
            port: 9090
          initialDelaySeconds: 30
          periodSeconds: 15
        readinessProbe:
          httpGet:
            path: /-/ready
            port: 9090
          initialDelaySeconds 10
          periodSeconds: 10
        resources:
          requests:
            memory: "512Mi"
            cpu: "250m"
          limits:
            memory: "2Gi"
            cpu: "1000m"
      volumes:
      - name: config
        configMap:
          name: prometheus-config
      - name: storage
        persistentVolumeClaim:
          claimName: prometheus-pvc
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: prometheus-pvc
  namespace: akosha
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 20Gi
  storageClassName: standard
```

______________________________________________________________________

### Grafana Service (`k8s/monitoring/grafana-service.yaml`)

```yaml
apiVersion: v1
kind: Service
metadata:
  name: grafana
  namespace: akosha
spec:
  selector:
    app: grafana
  ports:
  - port: 3000
    targetPort: 3000
  type: LoadBalancer
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: grafana
  namespace: akosha
  labels:
    app: grafana
spec:
  replicas: 1
  selector:
    matchLabels:
      app: grafana
  template:
    metadata:
      labels:
        app: grafana
    spec:
      containers:
      - name: grafana
        image: grafana/grafana:latest
        ports:
        - containerPort: 3000
        env:
        - name: GF_SECURITY_ADMIN_PASSWORD
          value: "admin"
        - name: GF_USERS_ALLOW_SIGN_UP
          value: "false"
        - name: GF_INSTALL_PLUGINS
          value: ""
        volumeMounts:
        - name: dashboards
          mountPath: /etc/grafana/provisioning/dashboards
        - name: provisioning
          mountPath: /etc/grafana/provisioning
        resources:
          requests:
            memory: "256Mi"
            cpu: "100m"
          limits:
            memory: "512Mi"
            cpu: "500m"
      volumes:
      - name: dashboards
        configMap:
          name: grafana-dashboards
      - name: provisioning
        configMap:
          name: grafana-provisioning
```

______________________________________________________________________

## 🔧 Configuration

### Update Image Registry

Edit `k8s/kustomization.yaml`:

```yaml
images:
  - name: akosha
    newName: your-registry.com/akosha  # Update this
    newTag: v1.0.0                          # Update this
```

### Customize Environment

Edit `k8s/configmap.yaml` for production settings:

```yaml
ENVIRONMENT: "production"
LOG_LEVEL: "WARNING"  # Reduce log noise
```

### Adjust Resource Limits

Based on load testing, update `k8s/deployment.yaml`:

```yaml
resources:
  requests:
    memory: "512Mi"   # Increase for higher load
    cpu: "500m"
  limits:
    memory: "2Gi"     # Adjust limits
    cpu: "2000m"
```

______________________________________________________________________

## 📊 Monitoring Setup

### 1. Import Dashboards

```bash
# Port forward Grafana
kubectl port-forward -n akosha svc/grafana 3001:3000

# Open Grafana: http://localhost:3001 (admin/admin)

# Import dashboards from monitoring/grafana/
# - embedding-performance.json
# - circuit-breaker-status.json
# - system-health.json
```

### 2. Configure Prometheus Alerts

```bash
# Alerts are automatically loaded from k8s/monitoring/prometheus/

# Verify in Prometheus UI
kubectl port-forward -n akosha svc/prometheus 9090:9090
# Open: http://localhost:9090
# Navigate to: Alerts → akosha_alerts
```

### 3. View Traces in Jaeger

```bash
# Port forward Jaeger
kubectl port-forward -n akosha svc/jaeger 16686:16686

# Open Jaeger UI: http://localhost:16686
# Search for traces by service name, operation, or tags
```

______________________________________________________________________

## 🚀 Deployment Commands

### Deploy Everything

```bash
# Deploy Akosha
kubectl apply -f k8s/

# Deploy monitoring stack
kubectl apply -f k8s/monitoring/
```

### Update Deployment

```bash
# Update image (after building new version)
kubectl set image deployment/akosha-mcp akosha=your-registry/akosha:v2.0 -n akosha

# Rolling update
kubectl rollout status deployment/akosha-mcp -n akosha

# Rollback if needed
kubectl rollout undo deployment/akosha-mcp -n akosha
```

### Scale Deployment

```bash
# Manual scaling
kubectl scale deployment/akosha-mcp --replicas=5 -n akosha

# Or let HPA handle it automatically
kubectl get hpa -n akosha
```

### Check Status

```bash
# Overall status
kubectl get all -n akosha

# Pod status
kubectl get pods -n akosha

# Deployment status
kubectl rollout status deployment/akosha-mcp -n akosha

# HPA status
kubectl describe hpa akosha-mcp-hpa -n akosha

# Metrics
kubectl top pods -n akosha
```

______________________________________________________________________

## 🛡️ Production Considerations

### High Availability

- ✅ 2+ replicas with HPA
- ✅ Pod anti-affinity
- ✅ PDB for availability
- ✅ Rolling updates

### Resource Management

- ✅ Resource requests/limits
- ✅ ResourceQuota at namespace level
- ✅ LimitRange for container defaults
- ✅ HPA for auto-scaling

### Security

- ✅ ServiceAccount with RBAC
- ✅ Network policies
- ✅ Security contexts (non-root)
- ✅ Secrets for sensitive data

### Observability

- ✅ Prometheus metrics
- ✅ Grafana dashboards
- ✅ Jaeger tracing
- ✅ AlertManager alerts

### Persistence

- ✅ PVC for data storage
- ✅ PVC for Prometheus metrics
- ✅ Backup strategy needed

______________________________________________________________________

## 📈 Scaling Strategy

### Horizontal Scaling

**Current limits**:

- Min: 2 pods
- Max: 10 pods (via HPA)
- Per-pod limits: 1Gi memory, 1000m CPU

**To increase capacity**:

1. Edit `k8s/hpa.yaml`:

   ```yaml
   maxReplicas: 20  # Increase from 10
   ```

1. Edit `k8s/resourcequota.yaml`:

   ```yaml
   hard:
     pods: "30"  # Increase from 20
     limits.cpu: "20"  # Increase from 10
   ```

1. Redeploy:

   ```bash
   kubectl apply -f k8s/
   ```

### Vertical Scaling

Edit `k8s/deployment.yaml`:

```yaml
resources:
  requests:
    memory: "512Mi"   # Increase for more memory
    cpu: "500m"
  limits:
    memory: "2Gi"     # Increase limits
    cpu: "2000m"
```

______________________________________________________________________

## 🔍 Troubleshooting

### Pods CrashLoopBackOff

```bash
# Check pod logs
kubectl logs -n akosha <pod-name>

# Describe pod for events
kubectl describe pod -n akosha <pod-name>

# Common issues:
# - OOMKilled: Increase memory limit
# - CrashLoopBackOff: Check logs for errors
# - ImagePullBackOff: Verify image exists
```

### HPA Not Scaling

```bash
# Check HPA conditions
kubectl describe hpa akosha-mcp-hpa -n akosha

# Check resource usage
kubectl top pods -n akosha

# Verify metrics-server is running
kubectl get apiservice | grep metrics
```

### Monitoring Not Working

```bash
# Check Prometheus targets
kubectl port-forward svc/prometheus 9090:9090
# Open: http://localhost:9090/targets

# Check service endpoints
kubectl get endpoints -n akosha

# Verify annotations
kubectl get pod <pod-name> -n akosha -o yaml | grep prometheus
```

______________________________________________________________________

## 📚 Additional Resources

- [Kubernetes Documentation](https://kubernetes.io/docs/)
- [Prometheus on Kubernetes](https://prometheus.io/docs/prometheus/latest/installation/)
- [Grafana Installation](https://grafana.com/docs/grafana/latest/installation/kubernetes/)
- [Jaeger Kubernetes](https://www.jaegertracing.io/docs/latest/deployment/)
- [HPA Best Practices](https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/)

______________________________________________________________________

**Deploy Akosha with confidence on Kubernetes!** 🚀
