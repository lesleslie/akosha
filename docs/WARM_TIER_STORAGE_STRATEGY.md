# Warm Tier Storage Strategy - DevOps Guide

## Executive Summary

This document provides production-hardened recommendations for managing Akosha's warm tier DuckDB database across development, staging, and production environments.

**Database Characteristics**:

- Size: 10GB-100GB (7-90 days of data)
- Access pattern: Write-heavy (ingestion) + Read-heavy (analytics)
- Criticality: High (core to search/analytics functionality)
- Retention: 83 days in warm tier before aging to cold

______________________________________________________________________

## 1. Recommended Path Structure

### Development Environment

**Location**: `~/.akosha/dev/warm/`

**Rationale**:

- Isolated from project directory (doesn't pollute git)
- Survives project directory changes
- Supports multiple project instances
- Easy to reset/clear for testing
- Follows XDG Base Directory specification

**Implementation**:

```yaml
# config/lite.yaml (development)
storage:
  warm:
    backend: duckdb-ssd
    path: "~/.akosha/dev/warm"  # Expanded to user home
```

```python
# Path resolution in warm_store.py
import os
from pathlib import Path

def get_warm_path(config_path: str) -> Path:
    """Resolve warm storage path with environment variable override.

    Priority:
    1. AKOSHA_WARM_PATH env var
    2. config.yaml path value (with ~ expansion)
    3. Default: ~/.akosha/dev/warm
    """
    if env_path := os.getenv("AKOSHA_WARM_PATH"):
        return Path(env_path)

    # Expand ~ to user home directory
    resolved = Path(config_path).expanduser()

    # Default if not specified
    if str(resolved) == ".":
        return Path.home() / ".akosha" / "dev" / "warm"

    return resolved
```

### Staging Environment

**Location**: `/data/akosha/staging/warm/`

**Rationale**:

- Matches production structure
- Easier to test deployment scripts
- Realistic performance testing
- Separate from production data

```yaml
# config/staging.yaml
storage:
  warm:
    backend: duckdb-ssd
    path: "/data/akosha/staging/warm"
```

### Production Environment

**Location**: `/data/akosha/prod/warm/`

**Rationale**:

- Standard Linux filesystem hierarchy
- Easy to mount on dedicated SSD/NVMe
- Separate from application code
- Clear separation of concerns
- Easy backup/snapshot

```yaml
# config/production.yaml
storage:
  warm:
    backend: duckdb-ssd
    path: "/data/akosha/prod/warm"
```

______________________________________________________________________

## 2. Docker Volume Mount Considerations

### Development Docker Setup

**docker-compose.dev.yml**:

```yaml
version: '3.8'

services:
  akosha-dev:
    build: .
    command: python -m akosha.mcp
    environment:
      - AKOSHA_MODE=lite
      - AKOSHA_WARM_PATH=/data/akosha/dev/warm
    volumes:
      # Named volume for persistence
      - akosha_warm_dev:/data/akosha/dev/warm
      # Mount source code for hot reload
      - ./akosha:/app/akosha:ro
    ports:
      - "3002:3002"  # MCP port
      - "8682:8682"  # API port

volumes:
  akosha_warm_dev:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: ~/.akosha/dev/warm  # Bind mount to host directory
```

### Production Docker Setup

**docker-compose.prod.yml**:

```yaml
version: '3.8'

services:
  akosha:
    image: ghcr.io/yourorg/akosha:${VERSION:-latest}
    command: python -m akosha.mcp
    environment:
      - AKOSHA_MODE=standard
      - AKOSHA_WARM_PATH=/data/akosha/prod/warm
      - AKOSHA_REDIS_HOST=redis
      - AKOSHA_REDIS_PORT=6379
    volumes:
      # Dedicated NVMe SSD mount
      - warm_data:/data/akosha/prod/warm
      # WAL for hot store (separate disk)
      - wal_data:/data/akosha/wal
    ports:
      - "8682:8682"
      - "9090:9090"  # Prometheus metrics
    depends_on:
      - redis
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8682/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
    deploy:
      resources:
        limits:
          memory: 4G
          cpus: '2'
        reservations:
          memory: 2G
          cpus: '1'

  redis:
    image: redis:7-alpine
    command: redis-server --maxmemory 2gb --maxmemory-policy allkeys-lru
    volumes:
      - redis_data:/data
    restart: unless-stopped

volumes:
  warm_data:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /mnt/nvme/akosha/warm  # Dedicated NVMe SSD

  wal_data:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /mnt/ssd/akosha/wal  # Separate SSD for WAL

  redis_data:
    driver: local
```

### Dockerfile Considerations

```dockerfile
# Dockerfile
FROM python:3.13-slim

# Create non-root user for security
RUN groupadd -r akosha && useradd -r -g akosha akosha

# Create data directories with correct permissions
RUN mkdir -p /data/akosha/{warm,wal,cold} && \
    chown -R akosha:akosha /data/akosha

WORKDIR /app

# Install dependencies
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && \
    uv sync --frozen --no-dev

# Copy application code
COPY --chown=akosha:akosha . .

# Switch to non-root user
USER akosha

# Expose ports
EXPOSE 8682 3002 9090

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8682/health')"

# Run application
CMD ["python", "-m", "akosha.mcp"]
```

______________________________________________________________________

## 3. Kubernetes Persistent Volume Claim Patterns

### Development/Minikube

```yaml
# k8s/dev/pvc-warm.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: akosha-warm-dev
  namespace: akosha-dev
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 20Gi
  storageClassName: standard  # Minikube default
```

### Production (Cloud-Native)

#### GCP GKE Example

```yaml
# k8s/prod/pvc-warm.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: akosha-warm-prod
  namespace: akosha-prod
  labels:
    app: akosha
    tier: warm-storage
spec:
  accessModes:
    - ReadWriteOnce  # Single pod per PV for performance
  resources:
    requests:
      storage: 200Gi
  storageClassName: premium-rwo  # SSD-backed
  volumeMode: Filesystem
```

**StorageClass**:

```yaml
# k8s/prod/storageclass-premium.yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: premium-rwo
provisioner: pd.csi.storage.gke.io
parameters:
  type: pd-ssd  # SSD persistent disk
  fstype: ext4
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
allowedTopologies:
  - matchLabelExpressions:
      - key: failure-domain.beta.kubernetes.io/zone
        values:
          - us-west-2a
          - us-west-2b
```

#### AWS EKS Example

```yaml
# k8s/prod/pvc-warm-aws.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: akosha-warm-prod
  namespace: akosha-prod
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 200Gi
  storageClassName: gp3-encrypted  # AWS GP3 with encryption
```

**StorageClass**:

```yaml
# k8s/prod/storageclass-gp3.yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3-encrypted
provisioner: ebs.csi.aws.com
parameters:
  type: gp3
  iops: "16000"  # Max IOPS for 200GB
  throughput: "1000"  # MiB/s
  encrypted: "true"
  kmsKeyId: ${KMS_KEY_ID}
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
```

### Deployment Configuration

```yaml
# k8s/prod/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: akosha
  namespace: akosha-prod
spec:
  replicas: 3  # Only 1 can mount RWO PVC at a time
  selector:
    matchLabels:
      app: akosha
  template:
    metadata:
      labels:
        app: akosha
        tier: backend
    spec:
      # Anti-affinity to spread pods across nodes
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              podAffinityTerm:
                labelSelector:
                  matchExpressions:
                    - key: app
                      operator: In
                      values: [akosha]
                topologyKey: kubernetes.io/hostname

      # Init container to set directory permissions
      initContainers:
        - name: setup-permissions
          image: busybox:1.36
          command:
            - sh
            - -c
            - |
              mkdir -p /data/akosha/{warm,wal,cold}
              chown -R 1000:1000 /data/akosha
          volumeMounts:
            - name: warm-storage
              mountPath: /data/akosha/warm
            - name: wal-storage
              mountPath: /data/akosha/wal

      containers:
        - name: akosha
          image: ghcr.io/yourorg/akosha:${VERSION}
          env:
            - name: AKOSHA_MODE
              value: "standard"
            - name: AKOSHA_WARM_PATH
              value: "/data/akosha/prod/warm"
            - name: AKOSHA_REDIS_HOST
              value: "redis.akosha-prod.svc.cluster.local"
          ports:
            - containerPort: 8682
              name: api
            - containerPort: 9090
              name: metrics
          volumeMounts:
            - name: warm-storage
              mountPath: /data/akosha/warm
            - name: wal-storage
              mountPath: /data/akosha/wal
          resources:
            requests:
              memory: "2Gi"
              cpu: "1000m"
            limits:
              memory: "4Gi"
              cpu: "2000m"
          livenessProbe:
            httpGet:
              path: /health
              port: 8682
            initialDelaySeconds: 30
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 3
          readinessProbe:
            httpGet:
              path: /ready
              port: 8682
            initialDelaySeconds: 10
            periodSeconds: 5
            timeoutSeconds: 3
            failureThreshold: 2

      volumes:
        - name: warm-storage
          persistentVolumeClaim:
            claimName: akosha-warm-prod
        - name: wal-storage
          persistentVolumeClaim:
            claimName: akosha-wal-prod
```

### StatefulSet Alternative (for multi-pod write access)

**Note**: DuckDB doesn't support multi-writer, but you can use StatefulSet for:

1. Stable network identities
1. Ordered pod startup
1. Dedicated PVC per pod (for sharding)

```yaml
# k8s/prod/statefulset.yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: akosha-shard
  namespace: akosha-prod
spec:
  serviceName: akosha-shard
  replicas: 4  # 4 shards
  podManagementPolicy: Parallel
  updateStrategy:
    type: RollingUpdate
  selector:
    matchLabels:
      app: akosha-shard
  template:
    metadata:
      labels:
        app: akosha-shard
    spec:
      containers:
        - name: akosha
          image: ghcr.io/yourorg/akosha:${VERSION}
          env:
            - name: AKOSHA_SHARD_ID
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name  # akosha-shard-0, -1, -2, -3
          volumeMounts:
            - name: warm-data
              mountPath: /data/akosha/warm
  volumeClaimTemplates:
    - metadata:
        name: warm-data
      spec:
        accessModes: [ReadWriteOnce]
        storageClassName: premium-rwo
        resources:
          requests:
            storage: 50Gi  # 50Gi per shard
```

______________________________________________________________________

## 4. Environment Variable Override Strategy

### Environment Variable Naming Convention

```bash
# Primary path override
AKOSHA_WARM_PATH=/data/akosha/prod/warm

# Tier-specific overrides
AKOSHA_HOT_PATH=/data/akosha/hot
AKOSHA_WARM_PATH=/data/akosha/warm
AKOSHA_COLD_PATH=/data/akosha/cold

# Backend override
AKOSHA_WARM_BACKEND=duckdb-ssd

# Cloud storage (via Oneiric)
AKOSHA_COLD_BACKEND=s3
AKOSHA_COLD_BUCKET=akosha-cold-prod
AKOSHA_COLD_PREFIX=conversations/
AKOSHA_COLD_REGION=us-west-2
```

### Implementation Pattern

```python
# akosha/config.py
import os
from pathlib import Path
import yaml
from typing import Any

class StorageConfig:
    """Storage configuration with environment variable override."""

    def __init__(self, config_path: str = "config/standard.yaml"):
        """Initialize storage configuration.

        Priority:
        1. Environment variables (highest)
        2. Config file values
        3. Defaults (lowest)
        """
        # Load base config from file
        with open(config_path) as f:
            self._config = yaml.safe_load(f)

    def get_warm_path(self) -> Path:
        """Get warm storage path with environment override.

        Returns:
            Path to warm storage directory

        Examples:
            >>> config = StorageConfig()
            >>> config.get_warm_path()
            Path('/data/akosha/warm')
        """
        # Priority 1: Environment variable
        if env_path := os.getenv("AKOSHA_WARM_PATH"):
            return Path(env_path).expanduser()

        # Priority 2: Config file
        if config_path := self._config.get("storage", {}).get("warm", {}).get("path"):
            return Path(config_path).expanduser()

        # Priority 3: Default
        return Path.home() / ".akosha" / "warm"

    def get_warm_backend(self) -> str:
        """Get warm storage backend type."""
        return os.getenv(
            "AKOSHA_WARM_BACKEND",
            self._config.get("storage", {}).get("warm", {}).get("backend", "duckdb-ssd")
        )
```

### Configuration Matrix

| Environment | Config File | Env Override | Final Path |
|-------------|-------------|--------------|------------|
| **Local Dev** | `config/lite.yaml` | none | `~/.akosha/dev/warm` |
| **Docker Dev** | `config/lite.yaml` | `AKOSHA_WARM_PATH=/data/akosha/dev/warm` | `/data/akosha/dev/warm` |
| **Staging** | `config/staging.yaml` | none | `/data/akosha/staging/warm` |
| **Production** | `config/production.yaml` | `AKOSHA_WARM_PATH=/data/akosha/prod/warm` | `/data/akosha/prod/warm` |

### Kubernetes ConfigMap

```yaml
# k8s/prod/configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: akosha-config
  namespace: akosha-prod
data:
  AKOSHA_MODE: "standard"
  AKOSHA_WARM_PATH: "/data/akosha/prod/warm"
  AKOSHA_REDIS_HOST: "redis.akosha-prod.svc.cluster.local"
  AKOSHA_REDIS_PORT: "6379"
  AKOSHA_COLD_BACKEND: "s3"
  AKOSHA_COLD_BUCKET: "akosha-cold-prod"
  AKOSHA_COLD_REGION: "us-west-2"
---
apiVersion: v1
kind: Secret
metadata:
  name: akosha-secrets
  namespace: akosha-prod
type: Opaque
stringData:
  AKOSHA_JWT_SECRET: "${JWT_SECRET}"
  AWS_ACCESS_KEY_ID: "${AWS_ACCESS_KEY_ID}"
  AWS_SECRET_ACCESS_KEY: "${AWS_SECRET_ACCESS_KEY}"
```

**Usage in Deployment**:

```yaml
# k8s/prod/deployment.yaml (partial)
spec:
  template:
    spec:
      containers:
        - name: akosha
          envFrom:
            - configMapRef:
                name: akosha-config
            - secretRef:
                name: akosha-secrets
```

______________________________________________________________________

## 5. CI/CD Pipeline Implications

### Pipeline Stages

```yaml
# .github/workflows/deploy.yml
name: Deploy Akosha

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.13'

      - name: Install dependencies
        run: |
          pip install uv
          uv sync --group dev

      - name: Run tests with temp warm storage
        env:
          AKOSHA_WARM_PATH: /tmp/akosha_test_warm
        run: |
          pytest tests/ -v --cov=akosha

      - name: Verify storage path isolation
        run: |
          # Ensure no data written to project directory
          if [ -d "./data/warm" ]; then
            echo "ERROR: Warm data written to project directory!"
            exit 1
          fi
          echo "✅ Storage isolation verified"

  build:
    needs: test
    runs-on: ubuntu-latest
    outputs:
      image_tag: ${{ steps.meta.outputs.tags }}
    steps:
      - uses: actions/checkout@v4

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Login to GHCR
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Build and push
        uses: docker/build-push-action@v5
        with:
          context: .
          push: true
          tags: |
            ghcr.io/yourorg/akosha:${{ github.sha }}
            ghcr.io/yourorg/akosha:latest
          cache-from: type=gha
          cache-to: type=gha,mode=max
          build-args: |
            VERSION=${{ github.sha }}

  deploy-staging:
    needs: build
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    environment: staging
    steps:
      - name: Deploy to staging
        run: |
          kubectl set image deployment/akosha \
            akosha=ghcr.io/yourorg/akosha:${{ github.sha }} \
            -n akosha-staging

      - name: Wait for rollout
        run: |
          kubectl rollout status deployment/akosha -n akosha-staging --timeout=5m

      - name: Run smoke tests
        run: |
          ./scripts/smoke-test.sh staging

  deploy-production:
    needs: deploy-staging
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    environment:
      name: production
      url: https://akosha.example.com
    steps:
      - name: Deploy to production
        run: |
          # Blue-green deployment with canary
          kubectl apply -f k8s/prod/deployment-canary.yaml

          # Monitor canary for 15 minutes
          ./scripts/monitor-canary.sh --duration=15m

          # Promote to full production
          kubectl apply -f k8s/prod/deployment.yaml

      - name: Verify deployment
        run: |
          ./scripts/smoke-test.sh production
          ./scripts/performance-test.sh production
```

### Pre-Commit Checks

```bash
# .github/scripts/verify-storage-isolation.sh
#!/usr/bin/env bash
set -euo pipefail

# Ensure warm storage path is configured correctly
if grep -q 'path: "\./data/warm"' config/*.yaml; then
  echo "ERROR: Relative path ./data/warm found in config"
  echo "Use AKOSHA_WARM_PATH environment variable instead"
  exit 1
fi

echo "✅ Storage configuration verified"
```

### Database Migration Strategy

```python
# scripts/migrate_warm_storage.py
#!/usr/bin/env python3
"""Migrate warm storage from old path to new path."""

import asyncio
import shutil
from pathlib import Path
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def migrate_warm_storage(
    old_path: Path,
    new_path: Path,
    backup_path: Path | None = None,
) -> None:
    """Migrate warm storage with zero downtime.

    Strategy:
    1. Create backup of old database
    2. Copy to new location
    3. Verify integrity
    4. Update symlink atomically
    5. Remove old data after verification

    Args:
        old_path: Current warm storage path
        new_path: New warm storage path
        backup_path: Optional backup location
    """
    if not old_path.exists():
        logger.error(f"Old path does not exist: {old_path}")
        return

    logger.info(f"Migrating warm storage: {old_path} -> {new_path}")

    # Step 1: Create backup
    if backup_path:
        logger.info(f"Creating backup: {backup_path}")
        shutil.copy2(old_path, backup_path)

    # Step 2: Copy to new location
    logger.info(f"Copying to new location: {new_path}")
    new_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(old_path, new_path)

    # Step 3: Verify integrity
    logger.info("Verifying database integrity...")
    import duckdb

    try:
        conn = duckdb.connect(str(new_path))
        conn.execute("SELECT COUNT(*) FROM conversations")
        count = conn.fetchone()[0]
        logger.info(f"✅ Integrity check passed: {count} conversations")
        conn.close()
    except Exception as e:
        logger.error(f"❌ Integrity check failed: {e}")
        if backup_path:
            logger.info("Restoring from backup")
            shutil.copy2(backup_path, old_path)
        raise

    # Step 4: Update symlink (atomic operation)
    logger.info("Updating symlink...")
    link_path = old_path.parent / "warm.db.current"
    link_path.unlink(missing_ok=True)
    link_path.symlink_to(new_path)

    logger.info("✅ Migration complete")

    # Step 5: Cleanup (manual verification required)
    logger.warning(f"Old data still at {old_path}")
    logger.warning("Verify operation, then manually remove old data")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--old-path", type=Path, required=True)
    parser.add_argument("--new-path", type=Path, required=True)
    parser.add_argument("--backup-path", type=Path, default=None)
    args = parser.parse_args()

    asyncio.run(migrate_warm_storage(args.old_path, args.new_path, args.backup_path))
```

______________________________________________________________________

## 6. Monitoring and Metrics for Disk Usage

### Prometheus Metrics

```python
# akosha/monitoring/disk_metrics.py
"""Disk usage monitoring for warm storage."""

import os
import logging
from pathlib import Path
from prometheus_client import Gauge, Info

logger = logging.getLogger(__name__)

# Define metrics
warm_storage_size_bytes = Gauge(
    'akosha_warm_storage_size_bytes',
    'Size of warm storage in bytes',
    ['environment', 'mountpoint']
)

warm_storage_usage_percent = Gauge(
    'akosha_warm_storage_usage_percent',
    'Percentage of disk space used',
    ['environment', 'mountpoint']
)

warm_storage_available_bytes = Gauge(
    'akosha_warm_storage_available_bytes',
    'Available disk space in bytes',
    ['environment', 'mountpoint']
)

warm_storage_conversation_count = Gauge(
    'akosha_warm_conversation_count',
    'Number of conversations in warm store',
    ['environment']
)

warm_storage_info = Info(
    'akosha_warm_storage_info',
    'Information about warm storage configuration',
    ['environment']
)


def collect_disk_metrics(warm_path: Path, environment: str = "production") -> None:
    """Collect disk usage metrics for warm storage.

    Args:
        warm_path: Path to warm storage directory
        environment: Environment name (dev, staging, production)
    """
    try:
        stat = os.statvfs(warm_path)

        # Calculate disk usage
        total_space = stat.f_frsize * stat.f_blocks
        available_space = stat.f_frsize * stat.f_bavail
        used_space = total_space - available_space
        usage_percent = (used_space / total_space) * 100

        # Update metrics
        mountpoint = str(warm_path)
        warm_storage_size_bytes.labels(environment=environment, mountpoint=mountpoint).set(used_space)
        warm_storage_usage_percent.labels(environment=environment, mountpoint=mountpoint).set(usage_percent)
        warm_storage_available_bytes.labels(environment=environment, mountpoint=mountpoint).set(available_space)

        # Log warnings
        if usage_percent > 90:
            logger.error(f"🚨 Warm storage {usage_percent:.1f}% full: {warm_path}")
        elif usage_percent > 75:
            logger.warning(f"⚠️ Warm storage {usage_percent:.1f}% full: {warm_path}")
        else:
            logger.debug(f"✅ Warm storage {usage_percent:.1f}% full: {warm_path}")

    except Exception as e:
        logger.error(f"Failed to collect disk metrics: {e}")


def collect_database_metrics(warm_path: Path, environment: str = "production") -> None:
    """Collect database-specific metrics.

    Args:
        warm_path: Path to warm storage database
        environment: Environment name
    """
    try:
        import duckdb

        conn = duckdb.connect(str(warm_path))

        # Count conversations
        result = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
        conversation_count = result[0] if result else 0
        warm_storage_conversation_count.labels(environment=environment).set(conversation_count)

        # Get table size
        result = conn.execute("""
            SELECT pg_size_pretty(pg_database_size(current_database()))
        """).fetchone()

        logger.info(f"Warm store: {conversation_count} conversations, size: {result[0]}")
        conn.close()

    except Exception as e:
        logger.error(f"Failed to collect database metrics: {e}")


def update_storage_info(
    warm_path: Path,
    backend: str,
    environment: str = "production"
) -> None:
    """Update storage info metric.

    Args:
        warm_path: Path to warm storage
        backend: Backend type (duckdb-ssd, duckdb-hdd)
        environment: Environment name
    """
    warm_storage_info.labels(environment=environment).info({
        'path': str(warm_path),
        'backend': backend,
        'filesystem': os.path.basename(warm_path),
    })
```

### Grafana Dashboard Queries

```json
{
  "dashboard": {
    "title": "Akosha Warm Storage",
    "panels": [
      {
        "title": "Warm Storage Disk Usage",
        "targets": [
          {
            "expr": "akosha_warm_storage_usage_percent{environment=\"production\"}",
            "legendFormat": "{{mountpoint}}"
          }
        ],
        "alert": {
          "conditions": [
            {
              "evaluator": {"params": [85], "type": "gt"},
              "operator": {"type": "and"},
              "query": {"params": ["A", "5m", "now"]},
              "reducer": {"params": [], "type": "avg"},
              "type": "query"
            }
          ],
          "executionErrorState": "alerting",
          "frequency": "1m",
          "handler": 1,
          "name": "Warm Storage > 85%",
          "noDataState": "no_data",
          "notifications": []
        }
      },
      {
        "title": "Warm Storage Size (Bytes)",
        "targets": [
          {
            "expr": "akosha_warm_storage_size_bytes{environment=\"production\"}",
            "legendFormat": "{{mountpoint}}"
          }
        ]
      },
      {
        "title": "Conversation Count",
        "targets": [
          {
            "expr": "akosha_warm_conversation_count{environment=\"production\"}"
          }
        ]
      }
    ]
  }
}
```

### Alerting Rules

```yaml
# k8s/prod/prometheus-rules.yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: akosha-storage-alerts
  namespace: akosha-prod
spec:
  groups:
    - name: akosha_storage
      interval: 1m
      rules:
        # Critical: Disk almost full
        - alert: AkoshaWarmStorageCritical
          expr: |
            akosha_warm_storage_usage_percent{environment="production"} > 90
          for: 5m
          labels:
            severity: critical
            team: platform
          annotations:
            summary: "Warm storage > 90% full on {{ $labels.mountpoint }}"
            description: "Warm storage is at {{ $value }}% capacity. Immediate action required."
            runbook: "https://docs.example.com/runbooks/warm-storage-full"

        # Warning: Disk getting full
        - alert: AkoshaWarmStorageWarning
          expr: |
            akosha_warm_storage_usage_percent{environment="production"} > 75
          for: 15m
          labels:
            severity: warning
            team: platform
          annotations:
            summary: "Warm storage > 75% full on {{ $labels.mountpoint }}"
            description: "Warm storage is at {{ $value }}% capacity. Plan aging operation soon."

        # Growth rate anomaly
        - alert: AkoshaWarmStorageGrowingFast
          expr: |
            rate(akosha_warm_storage_size_bytes{environment="production"}[1h]) > 100*1024*1024
          for: 30m
          labels:
            severity: warning
            team: platform
          annotations:
            summary: "Warm storage growing > 100MB/hour"
            description: "Unusual growth rate detected: {{ $value | humanize }}B/s"

        # Database connection issues
        - alert: AkoshaWarmStoreDown
          expr: |
            up{job="akosha", environment="production"} == 0
          for: 2m
          labels:
            severity: critical
            team: platform
          annotations:
            summary: "Akosha warm store is down"
            description: "Warm store has been down for more than 2 minutes."
```

### Health Check Endpoints

```python
# akosha/api/health.py
"""Health check endpoints for storage monitoring."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from pathlib import Path
import os

router = APIRouter()


class StorageHealth(BaseModel):
    """Storage health status."""

    healthy: bool
    path: str
    size_bytes: int
    available_bytes: int
    usage_percent: float
    status: str


@router.get("/health/warm", response_model=StorageHealth)
async def check_warm_storage_health() -> StorageHealth:
    """Check warm storage health.

    Returns:
        Storage health status

    Raises:
        HTTPException: If storage is inaccessible
    """
    from akosha.config import get_current_config

    config = get_current_config()
    warm_path = config.get_warm_path()

    if not warm_path.exists():
        raise HTTPException(
            status_code=503,
            detail=f"Warm storage path does not exist: {warm_path}"
        )

    try:
        stat = os.statvfs(warm_path)
        total_space = stat.f_frsize * stat.f_blocks
        available_space = stat.f_frsize * stat.f_bavail
        used_space = total_space - available_space
        usage_percent = (used_space / total_space) * 100

        # Determine health status
        if usage_percent > 90:
            status = "critical"
            healthy = False
        elif usage_percent > 75:
            status = "warning"
            healthy = True
        else:
            status = "healthy"
            healthy = True

        return StorageHealth(
            healthy=healthy,
            path=str(warm_path),
            size_bytes=used_space,
            available_bytes=available_space,
            usage_percent=round(usage_percent, 2),
            status=status,
        )

    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Failed to check warm storage: {e}"
        )


@router.get("/metrics/storage")
async def get_storage_metrics():
    """Get detailed storage metrics."""
    from akosha.monitoring.disk_metrics import collect_disk_metrics, collect_database_metrics
    from akosha.config import get_current_config

    config = get_current_config()
    warm_path = config.get_warm_path()

    collect_disk_metrics(warm_path, environment="production")
    collect_database_metrics(warm_path, environment="production")

    return {"status": "metrics collected"}
```

______________________________________________________________________

## 7. Backup Strategy Recommendations

### Backup Hierarchy

```
┌─────────────────────────────────────────────────────────┐
│                  Backup Strategy                         │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  Hot Store (0-7 days)                                   │
│  ├─ WAL: Continuous backup to Redis AOF                 │
│  └─ Snapshots: Every 6 hours → Warm tier                │
│                                                          │
│  Warm Store (7-90 days)  ← FOCUS OF THIS DOCUMENT       │
│  ├─ Incremental: Hourly → Cold tier (Parquet)           │
│  ├─ Full daily: Midnight → S3/GCS                       │
│  ├─ Snapshots: Pre-migration → /snapshots               │
│  └─ Point-in-time: WAL replay capability                │
│                                                          │
│  Cold Store (90+ days)                                  │
│  ├─ Parquet files: Immutable, already in cloud storage  │
│  └─ Lifecycle: Move to Glacier after 1 year             │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

### Backup Script

```bash
#!/usr/bin/env bash
# scripts/backup_warm_storage.sh

set -euo pipefail

# Configuration
WARM_PATH="${AKOSHA_WARM_PATH:-/data/akosha/warm}"
BACKUP_BUCKET="${AKOSHA_BACKUP_BUCKET:-akosha-backups}"
BACKUP_PREFIX="warm/$(date +%Y/%m/%d)"
ENVIRONMENT="${AKOSHA_ENVIRONMENT:-production}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if warm database exists
if [ ! -f "${WARM_PATH}/warm.db" ]; then
    log_error "Warm database not found at ${WARM_PATH}/warm.db"
    exit 1
fi

log_info "Starting warm storage backup: ${WARM_PATH}"

# Create temporary backup directory
TMP_BACKUP_DIR=$(mktemp -d)
trap "rm -rf ${TMP_BACKUP_DIR}" EXIT

# Create checkpoint (consistent backup)
log_info "Creating database checkpoint..."
duckdb "${WARM_PATH}/warm.db" "CHECKPOINT;"

# Copy database to temp directory
log_info "Copying database to temp directory..."
cp "${WARM_PATH}/warm.db" "${TMP_BACKUP_DIR}/"

# Compress backup
log_info "Compressing backup..."
gzip -c "${TMP_BACKUP_DIR}/warm.db" > "${TMP_BACKUP_DIR}/warm.db.gz"

# Calculate checksum
log_info "Calculating checksum..."
sha256sum "${TMP_BACKUP_DIR}/warm.db.gz" | awk '{print $1}' > "${TMP_BACKUP_DIR}/warm.db.gz.sha256"

# Upload to S3/GCS
log_info "Uploading backup to ${BACKUP_BUCKET}..."
if command -v aws &> /dev/null; then
    # AWS S3
    aws s3 cp "${TMP_BACKUP_DIR}/warm.db.gz" \
        "s3://${BACKUP_BUCKET}/${BACKUP_PREFIX}/warm_${TIMESTAMP}.db.gz"

    aws s3 cp "${TMP_BACKUP_DIR}/warm.db.gz.sha256" \
        "s3://${BACKUP_BUCKET}/${BACKUP_PREFIX}/warm_${TIMESTAMP}.db.gz.sha256"

    # Tag with environment
    aws s3api put-object-tagging \
        --bucket "${BACKUP_BUCKET}" \
        --key "${BACKUP_PREFIX}/warm_${TIMESTAMP}.db.gz" \
        --tagging "Environment=${ENVIRONMENT},Timestamp=${TIMESTAMP}"
elif command -v gsutil &> /dev/null; then
    # GCP GCS
    gsutil cp "${TMP_BACKUP_DIR}/warm.db.gz" \
        "gs://${BACKUP_BUCKET}/${BACKUP_PREFIX}/warm_${TIMESTAMP}.db.gz"

    gsutil cp "${TMP_BACKUP_DIR}/warm.db.gz.sha256" \
        "gs://${BACKUP_BUCKET}/${BACKUP_PREFIX}/warm_${TIMESTAMP}.db.gz.sha256"
else
    log_error "Neither aws nor gsutil found. Cannot upload backup."
    exit 1
fi

# Cleanup old backups (retain last 30 days)
log_info "Cleaning up old backups (retaining 30 days)..."
CUTOFF_DATE=$(date -d "30 days ago" +%Y/%m/%d)

if command -v aws &> /dev/null; then
    aws s3 ls "s3://${BACKUP_BUCKET}/warm/" --recursive | while read -r line; do
        FILE_DATE=$(echo "$line" | awk '{print $4}' | cut -d'/' -f1-3)
        if [[ "${FILE_DATE}" < "${CUTOFF_DATE}" ]]; then
            log_info "Deleting old backup: ${FILE_DATE}"
            aws s3 rm "s3://${BACKUP_BUCKET}/${FILE_DATE}" --recursive
        fi
    done
fi

log_info "Backup completed successfully: warm_${TIMESTAMP}.db.gz"

# Print backup size
BACKUP_SIZE=$(du -h "${TMP_BACKUP_DIR}/warm.db.gz" | cut -f1)
log_info "Backup size: ${BACKUP_SIZE}"

# Verify backup
log_info "Verifying backup..."
REMOTE_SHA=$(aws s3 cp "s3://${BACKUP_BUCKET}/${BACKUP_PREFIX}/warm_${TIMESTAMP}.db.gz.sha256" - | awk '{print $1}')
LOCAL_SHA=$(sha256sum "${TMP_BACKUP_DIR}/warm.db.gz" | awk '{print $1}')

if [ "${REMOTE_SHA}" == "${LOCAL_SHA}" ]; then
    log_info "✅ Backup verification successful"
else
    log_error "❌ Backup verification failed: checksum mismatch"
    exit 1
fi
```

### Restore Procedure

```bash
#!/usr/bin/env bash
# scripts/restore_warm_storage.sh

set -euo pipefail

BACKUP_BUCKET="${AKOSHA_BACKUP_BUCKET:-akosha-backups}"
BACKUP_DATE="${1:-$(date +%Y/%m/%d)}"
WARM_PATH="${AKOSHA_WARM_PATH:-/data/akosha/warm}"

echo "Restoring warm storage from ${BACKUP_DATE}"

# Create backup of current database
if [ -f "${WARM_PATH}/warm.db" ]; then
    echo "Backing up current database..."
    cp "${WARM_PATH}/warm.db" "${WARM_PATH}/warm.db.before_restore.$(date +%Y%m%d_%H%M%S)"
fi

# Download backup
echo "Downloading backup..."
aws s3 cp "s3://${BACKUP_BUCKET}/warm/${BACKUP_DATE}/warm_*.db.gz" ./

# Decompress
echo "Decompressing backup..."
gunzip -c "warm_*.db.gz" > "${WARM_PATH}/warm.db"

# Verify database integrity
echo "Verifying database integrity..."
duckdb "${WARM_PATH}/warm.db" "SELECT COUNT(*) FROM conversations"

echo "✅ Restore completed successfully"
```

### Backup Kubernetes CronJob

```yaml
# k8s/prod/cronjob-backup.yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: akosha-warm-backup
  namespace: akosha-prod
spec:
  schedule: "0 2 * * *"  # 2 AM daily
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 3
      activeDeadlineSeconds: 3600  # 1 hour max
      template:
        spec:
          restartPolicy: OnFailure
          serviceAccountName: akosha-backup
          containers:
            - name: backup
              image: ghcr.io/yourorg/akosha:latest
              command:
                - /bin/bash
                - /scripts/backup_warm_storage.sh
              env:
                - name: AKOSHA_WARM_PATH
                  value: "/data/akosha/warm"
                - name: AKOSHA_BACKUP_BUCKET
                  value: "akosha-backups-prod"
                - name: AKOSHA_ENVIRONMENT
                  value: "production"
                - name: AWS_DEFAULT_REGION
                  value: "us-west-2"
              volumeMounts:
                - name: warm-storage
                  mountPath: /data/akosha/warm
                  readOnly: true
                - name: aws-credentials
                  mountPath: /root/.aws
                  readOnly: true
              resources:
                requests:
                  memory: "1Gi"
                  cpu: "500m"
                limits:
                  memory: "2Gi"
                  cpu: "1000m"
          volumes:
            - name: warm-storage
              persistentVolumeClaim:
                claimName: akosha-warm-prod
            - name: aws-credentials
              secret:
                secretName: aws-backup-credentials
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: akosha-backup
  namespace: akosha-prod
  annotations:
    eks.amazonaws.io/role-arn: ${AWS_BACKUP_ROLE_ARN}
```

### Disaster Recovery Procedure

````markdown
# Disaster Recovery Runbook

## Scenario 1: Warm Store Disk Failure

### Detection
- Alert: `AkoshaWarmStorageCritical` triggered
- Health check: `/health/warm` returns 503
- Pod status: `CreateContainerConfigError` or `FailedMount`

### Recovery Steps

1. **Verify failure** (5 min)
   ```bash
   kubectl describe pod akosha-xxx -n akosha-prod
   kubectl get pvc akosha-warm-prod -n akosha-prod
````

2. **Scale down deployment** (2 min)

   ```bash
   kubectl scale deployment akosha --replicas=0 -n akosha-prod
   ```

1. **Provision new PV** (15 min)

   ```bash
   # Create new PVC
   kubectl apply -f k8s/prod/pvc-warm-recovery.yaml
   ```

1. **Restore from latest backup** (30 min)

   ```bash
   # Restore job
   kubectl create job restore-warm-$(date +%s) \
     --from=cronjob/akosha-warm-backup \
     -n akosha-prod \
     -- restore-backup=$(date +%Y/%m/%d -d "1 day ago")
   ```

1. **Verify restored data** (10 min)

   ```bash
   kubectl exec -it akosha-xxx -n akosha-prod \
     -- duckdb /data/akosha/warm/warm.db \
     "SELECT COUNT(*) FROM conversations"
   ```

1. **Scale up deployment** (5 min)

   ```bash
   kubectl scale deployment akosha --replicas=3 -n akosha-prod
   ```

**Total RTO:** ~67 minutes
**Total RPO:** ~24 hours (last daily backup)

## Scenario 2: Database Corruption

### Detection

- Alert: High error rate in warm store queries
- Health check: `/health/warm` returns corruption error
- Logs: "Database file is corrupted"

### Recovery Steps

1. **Stop all writes** (5 min)

   ```bash
   kubectl scale deployment akosha --replicas=0 -n akosha-prod
   ```

1. **Checkpoint and export** (15 min)

   ```bash
   # Export what we can to Parquet
   kubectl create job export-warm -n akosha-prod \
     --image=ghcr.io/yourorg/akosha:latest \
     -- python -m akosha.scripts.export_warm_to_parquet
   ```

1. **Restore from backup** (30 min)

   - Same as Scenario 1, step 4

1. **Replay recent data** (20 min)

   ```bash
   # Re-import any data since last backup
   kubectl create job replay-warm -n akosha-prod \
     --image=ghcr.io/yourorg/akosha:latest \
     -- python -m akosha.scripts.replay_wal \
     --since-backup=$(date -d "1 day ago" +%s)
   ```

1. **Verify and scale up** (10 min)

**Total RTO:** ~80 minutes
**Total RPO:** Varies (may lose data between backup and corruption)

## Scenario 3: Accidental Data Deletion

### Detection

- Alert: Sudden drop in conversation count
- User report: "Missing conversations"
- Logs: DELETE executed on conversations table

### Recovery Steps

1. **IMMEDIATE: Stop all writes** (2 min)

   ```bash
   kubectl scale deployment akosha --replicas=0 -n akosha-prod
   ```

1. **Assess damage** (5 min)

   ```bash
   # Check when deletion occurred
   kubectl logs akosha-xxx -n akosha-prod | grep DELETE
   ```

1. **Clone current database** (10 min)

   ```bash
   # Preserve current state for forensics
   cp /data/akosha/warm/warm.db /data/akosha/warm/warm.db.forensic
   ```

1. **Restore from backup before deletion** (30 min)

   - Same as Scenario 1, step 4
   - Use backup from before deletion time

1. **Replay WAL up to deletion** (20 min)

   ```bash
   # Replay WAL, excluding the DELETE transaction
   kubectl create job replay-wal -n akosha-prod \
     --image=ghcr.io/yourorg/akosha:latest \
     -- python -m akosha.scripts.replay_wal \
     --exclude-txid=<deletion_txid>
   ```

1. **Verify and scale up** (10 min)

**Total RTO:** ~77 minutes
**Total RPO:** Near-zero (if WAL replay successful)

````

---

## 8. Implementation Checklist

### Phase 1: Development Environment (Week 1)

- [ ] Update `config/lite.yaml` with `~/.akosha/dev/warm` path
- [ ] Implement path resolution with environment variable override
- [ ] Update `.gitignore` to exclude `data/` directory
- [ ] Add migration script for existing `./data/warm` users
- [ ] Test local development with new path structure
- [ ] Update documentation with new paths

### Phase 2: Docker Configuration (Week 1-2)

- [ ] Create `docker-compose.dev.yml` with named volumes
- [ ] Create `docker-compose.prod.yml` with bind mounts
- [ ] Update `Dockerfile` with non-root user and data directories
- [ ] Test Docker volume persistence across container restarts
- [ ] Validate data isolation between dev and prod containers

### Phase 3: Kubernetes Configuration (Week 2-3)

- [ ] Create PVC for warm storage (dev/staging/prod)
- [ ] Define StorageClasses (SSD, HDD, encrypted)
- [ ] Create Deployment with volume mounts
- [ ] Add init container for directory permissions
- [ ] Implement StatefulSet for sharded deployments
- [ ] Test Pod restart with persistent data
- [ ] Test Pod migration across nodes

### Phase 4: Monitoring and Alerting (Week 3-4)

- [ ] Implement Prometheus metrics for disk usage
- [ ] Create Grafana dashboard for storage monitoring
- [ ] Define alerting rules (warning, critical)
- [ ] Add health check endpoints
- [ ] Test alert delivery (Slack, PagerDuty)
- [ ] Document runbook procedures

### Phase 5: Backup and Recovery (Week 4-5)

- [ ] Implement backup script
- [ ] Create Kubernetes CronJob for daily backups
- [ ] Implement restore script
- [ ] Test backup/restore in staging environment
- [ ] Document disaster recovery procedures
- [ ] Conduct DR drill (simulate disk failure)

### Phase 6: CI/CD Integration (Week 5-6)

- [ ] Update CI pipeline to test storage isolation
- [ ] Add pre-commit hook for path validation
- [ ] Implement canary deployment strategy
- [ ] Add smoke tests for storage health
- [ ] Test blue-green deployment
- [ ] Document rollback procedures

### Phase 7: Production Migration (Week 6-7)

- [ ] Create migration plan for existing production data
- [ ] Schedule maintenance window (if needed)
- [ ] Execute migration with zero downtime
- [ ] Verify data integrity post-migration
- [ ] Monitor performance metrics
- [ ] Update runbooks and documentation

---

## 9. Operational Best Practices

### Disk Performance Tuning

```bash
# Mount options for optimal DuckDB performance
# /etc/fstab entry for dedicated SSD

# For ext4
/dev/nvme0n1p1  /data/akosha/warm  ext4  defaults,noatime,nodiratime,data=writeback,barrier=0  0  2

# For XFS (recommended for large files)
/dev/nvme0n1p1  /data/akosha/warm  xfs  defaults,noatime,nodiratime,largeio,inode64  0  2

# Explanation:
# - noatime: Don't update access time (reduces writes)
# - nodiratime: Don't update directory access time
# - data=writeback: Faster writes at risk of data loss on power failure
# - barrier=0: Disable write barriers (use with battery-backed cache)
# - largeio: Enable large I/O requests
# - inode64: Use 64-bit inodes (for large filesystems)
````

### DuckDB Configuration

```python
# Optimize DuckDB for warm store workload
conn.execute("""
    PRAGMA enable_progress_bar=false;
    PRAGMA threads=4;
    PRAGMA max_memory='2GB';
    PRAGMA checkpoint_interval='15 min';
    PRAGMA wal_autocheckpoint=10000;
""")

# Enable compression for embedded data
conn.execute("""
    PRAGMA compression_codec='ZSTD';
    PRAGMA compression_level=9;
""")
```

### Filesystem Layout

```
/data/akosha/
├── hot/                    # Hot store (in-memory + WAL)
│   └── wal/                # Write-ahead log
├── warm/                   # Warm store (persistent SSD)
│   ├── warm.db             # Main database
│   ├── warm.db.wal         # DuckDB WAL
│   └── snapshots/          # Pre-migration snapshots
│       └── warm_20250209.db
├── cold/                   # Cold store (cloud storage)
│   └── staging/            # Staging area for uploads
└── backups/                # Local backup cache
    └── warm/
        └── 2025/02/09/
            └── warm_20250209_020000.db.gz
```

### Capacity Planning

**Storage Growth Formula**:

```
Daily Growth = (Systems × Conversations/System × Size/Conversation)

Example:
- 1,000 systems
- 100 conversations/system/day
- 5KB/conversation (compressed)
- Daily growth = 1,000 × 100 × 5KB = 500MB/day

Warm tier (83 days):
- Required storage = 500MB × 83 = 41.5GB
- Recommended = 41.5GB × 3 (headroom) = ~125GB
```

**Scaling Thresholds**:

| Metric | Warning | Critical | Action |
|--------|---------|----------|--------|
| Disk usage | 75% | 90% | Trigger aging, scale storage |
| IOPS | 10K | 15K | Add more shards, optimize queries |
| Latency p99 | 200ms | 500ms | Add cache, scale reads |
| Growth rate | 1GB/day | 2GB/day | Review retention, scale warm tier |

### Maintenance Windows

```yaml
# Schedule regular maintenance tasks
maintenance:
  daily:
    - time: "02:00 UTC"
      tasks:
        - checkpoint_database
        - collect_metrics
        - rotate_logs

  weekly:
    - day: "Sunday"
      time: "03:00 UTC"
      tasks:
        - analyze_query_performance
        - rebuild_indexes
        - vacuum_database

  monthly:
    - day: "1st"
      time: "04:00 UTC"
      tasks:
        - review_storage_growth
        - test_backup_restore
        - capacity_planning_review
```

______________________________________________________________________

## 10. Troubleshooting Guide

### Issue: "Database is locked"

**Symptoms**:

- Multiple pods trying to access same PVC
- ReadWriteOnce access mode violation

**Solution**:

```bash
# Check which pod has the mount
kubectl get pods -n akosha-prod -o wide

# Describe PVC to see binding
kubectl describe pvc akosha-warm-prod -n akosha-prod

# Scale down to single replica
kubectl scale deployment akosha --replicas=1 -n akosha-prod

# Consider StatefulSet for multi-pod access
```

### Issue: "Disk is full"

**Symptoms**:

- Alert: `AkoshaWarmStorageCritical`
- Write operations failing
- Database corruption risk

**Solution**:

```bash
# Check actual usage
du -sh /data/akosha/warm

# Trigger manual aging to cold tier
kubectl exec -it akosha-xxx -n akosha-prod \
  -- python -m akosha.scripts.trigger_aging \
  --batch-size=10000 \
  --target-age=60

# If still full, scale storage
kubectl patch pvc akosha-warm-prod -n akosha-prod \
  --type=json \
  -p='[{"op": "replace", "path": "/spec/resources/requests/storage", "value": "300Gi"}]'

# Or delete old data (last resort)
kubectl exec -it akosha-xxx -n akosha-prod \
  -- duckdb /data/akosha/warm/warm.db \
  "DELETE FROM conversations WHERE timestamp < NOW() - INTERVAL 60 DAYS"
```

### Issue: "Slow query performance"

**Symptoms**:

- p99 latency > 200ms
- High CPU usage
- User complaints

**Solution**:

```bash
# Check query plan
kubectl exec -it akosha-xxx -n akosha-prod \
  -- duckdb /data/akosha/warm/warm.db \
  "EXPLAIN ANALYZE SELECT * FROM conversations WHERE ... "

# Check indexes
kubectl exec -it akosha-xxx -n akosha-prod \
  -- duckdb /data/akosha/warm/warm.db \
  "PRAGMA database_size;"

# Rebuild indexes if needed
kubectl exec -it akosha-xxx -n akosha-prod \
  -- duckdb /data/akosha/warm/warm.db \
  "REINDEX;"

# Check for cache misses
curl http://akosha:9090/metrics | grep akosha_cache
```

### Issue: "Data inconsistency after aging"

**Symptoms**:

- Missing conversations
- Count mismatch between hot and warm
- Duplicate records

**Solution**:

```bash
# Compare counts
kubectl exec -it akosha-xxx -n akosha-prod \
  -- python -c "
from akosha.storage import HotStore, WarmStore
import asyncio

async def check():
    hot = HotStore(...)
    warm = WarmStore(...)
    hot_count = await hot.count()
    warm_count = await warm.count()
    print(f'Hot: {hot_count}, Warm: {warm_count}')

asyncio.run(check())
"

# Re-run aging with verification
kubectl exec -it akosha-xxx -n akosha-prod \
  -- python -m akosha.scripts.trigger_aging \
  --verify \
  --dry-run

# Restore from backup if needed
./scripts/restore_warm_storage.sh 2025/02/08
```

______________________________________________________________________

## Appendix A: Configuration Examples

### Complete Production Config

```yaml
# config/production.yaml
mode: standard

storage:
  hot:
    backend: duckdb-memory
    path: ":memory:"
    write_ahead_log: true
    wal_path: "/data/akosha/hot/wal"

  warm:
    backend: duckdb-ssd
    path: "/data/akosha/warm"
    num_partitions: 256

    # Performance tuning
    checkpoint_interval_minutes: 15
    max_memory_gb: 2
    threads: 4

    # Compression
    compression_codec: "ZSTD"
    compression_level: 9

  cold:
    enabled: true
    backend: "s3"
    bucket: "akosha-cold-prod"
    prefix: "conversations/"
    region: "us-west-2"

cache:
  backend: redis
  host: "${AKOSHA_REDIS_HOST}"
  port: 6379
  db: 0

monitoring:
  metrics_enabled: true
  prometheus_port: 9090

  # Disk usage alerts
  disk_usage_warning_percent: 75
  disk_usage_critical_percent: 90

logging:
  level: "INFO"
  format: "json"
```

### Environment Variables Reference

```bash
# Required for production
export AKOSHA_MODE=standard
export AKOSHA_WARM_PATH=/data/akosha/warm
export AKOSHA_REDIS_HOST=redis.akosha-prod.svc.cluster.local

# Optional overrides
export AKOSHA_HOT_PATH=/data/akosha/hot
export AKOSHA_COLD_BACKEND=s3
export AKOSHA_COLD_BUCKET=akosha-cold-prod
export AKOSHA_COLD_REGION=us-west-2

# Backup configuration
export AKOSHA_BACKUP_BUCKET=akosha-backups-prod
export AKOSHA_BACKUP_RETENTION_DAYS=30

# Monitoring
export AKOSHA_PROMETHEUS_PORT=9090
export AKOSHA_METRICS_ENABLED=true
```

______________________________________________________________________

## Appendix B: Performance Benchmarks

### DuckDB Performance on Different Storage

| Storage Type | Read IOPS | Write IOPS | Latency (p50) | Latency (p99) | Cost/GB |
|--------------|-----------|------------|---------------|---------------|---------|
| **Local NVMe** | 500K | 200K | 5ms | 15ms | $0.50 |
| **Local SSD** | 100K | 50K | 10ms | 30ms | $0.20 |
| **Network SSD (EBS gp3)** | 16K | 16K | 20ms | 50ms | $0.08 |
| **Network HDD (EBS st1)** | 500 | 200 | 50ms | 200ms | $0.045 |

**Recommendation**: Use local NVMe for warm tier in production.

### Query Performance (10GB database, 10M rows)

| Query | Hot Store (memory) | Warm Store (SSD) | Warm Store (HDD) |
|-------|-------------------|------------------|------------------|
| Point lookup (by ID) | 1ms | 5ms | 20ms |
| Vector search (top-10) | 10ms | 50ms | 200ms |
| Date range scan (1 day) | 5ms | 25ms | 100ms |
| Aggregation (COUNT) | 50ms | 200ms | 1000ms |

______________________________________________________________________

## Summary

This document provides a comprehensive DevOps strategy for managing Akosha's warm tier storage across all environments:

1. **Path Structure**: Environment-specific paths with XDG compliance
1. **Docker**: Named volumes for dev, bind mounts for prod
1. **Kubernetes**: PVC with StorageClasses, StatefulSet for sharding
1. **Environment Variables**: Override pattern with clear priority
1. **CI/CD**: Pipeline integration with pre-flight checks
1. **Monitoring**: Prometheus metrics, Grafana dashboards, alerting
1. **Backup**: Daily automated backups with disaster recovery procedures

**Key Takeaways**:

- Never write to project directory (use `~/.akosha` or `/data/akosha`)
- Use environment variables for deployment flexibility
- Implement comprehensive monitoring and alerting
- Automate backups and test restore procedures
- Plan for growth with capacity planning thresholds
- Document runbooks for common failure scenarios

**Next Steps**: Implement Phase 1 (Development Environment) and validate with local testing before proceeding to Docker and Kubernetes configurations.
