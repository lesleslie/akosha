# Warm Tier Storage - Quick Start Implementation

This guide provides step-by-step instructions for implementing the warm tier storage strategy.

## Phase 1: Update Configuration (5 minutes)

### 1.1 Update Config Files

**Edit `config/lite.yaml`**:
```yaml
storage:
  hot:
    backend: duckdb-memory
    path: ":memory:"
    write_ahead_log: true
    wal_path: "~/.akosha/dev/wal"  # Updated

  warm:
    backend: duckdb-ssd
    path: "~/.akosha/dev/warm"  # Updated - uses home directory
    num_partitions: 64  # Reduced for dev
```

**Edit `config/standard.yaml`**:
```yaml
storage:
  hot:
    backend: duckdb-memory
    path: ":memory:"
    write_ahead_log: true
    wal_path: "/data/akosha/wal"  # Updated

  warm:
    backend: duckdb-ssd
    path: "/data/akosha/prod/warm"  # Updated - production path
    num_partitions: 256
```

### 1.2 Verify .gitignore

Ensure `.gitignore` excludes data directories:
```bash
# .gitignore should include:
data/
cache/
*.db
*.db-journal
```

## Phase 2: Test Local Development (10 minutes)

### 2.1 Test with New Paths

```bash
# Set environment variable for testing
export AKOSHA_MODE=lite
export AKOSHA_WARM_PATH=~/.akosha/dev/warm

# Run Akosha
python -m akosha.mcp

# In another terminal, verify path
ls -la ~/.akosha/dev/warm/
```

### 2.2 Verify No Project Directory Pollution

```bash
# Ensure no data directory created in project root
if [ -d "./data/warm" ]; then
    echo "ERROR: Data written to project directory!"
    exit 1
fi
echo "✅ Storage isolation verified"
```

## Phase 3: Docker Setup (15 minutes)

### 3.1 Create docker-compose.override.yml

```yaml
# docker-compose.override.yml (for local development)
version: '3.8'

services:
  akosha:
    environment:
      - AKOSHA_WARM_PATH=/data/akosha/dev/warm
    volumes:
      - akosha_warm_dev:/data/akosha/dev/warm
      - ./akosha:/app/akosha:ro

volumes:
  akosha_warm_dev:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: ${HOME}/.akosha/dev/warm  # Bind mount to home directory
```

### 3.2 Test Docker

```bash
# Build and run
docker-compose up --build

# Verify volume mount
docker-compose exec akosha ls -la /data/akosha/dev/warm
```

## Phase 4: Kubernetes Setup (20 minutes)

### 4.1 Create Namespace and PVC

```bash
# Apply Kubernetes manifests
kubectl create namespace akosha-dev
kubectl apply -f k8s/dev/pvc-warm.yaml
kubectl apply -f k8s/dev/configmap.yaml
kubectl apply -f k8s/dev/deployment.yaml
```

### 4.2 Verify PVC Binding

```bash
# Check PVC status
kubectl get pvc -n akosha-dev

# Describe PVC to see binding
kubectl describe pvc akosha-warm-dev -n akosha-dev

# Check pod mount
kubectl get pods -n akosha-dev
kubectl exec -it <pod-name> -n akosha-dev -- ls -la /data/akosha/warm
```

## Phase 5: Monitoring Setup (15 minutes)

### 5.1 Deploy Prometheus and Grafana

```bash
# Add Prometheus Helm repo
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

# Install Prometheus
helm install prometheus prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace

# Port forward to access Grafana
kubectl port-forward -n monitoring svc/prometheus-grafana 3000:80
```

### 5.2 Import Akosha Dashboard

1. Open Grafana: http://localhost:3000 (admin/prom-operator)
2. Navigate to Dashboards → Import
3. Paste dashboard JSON from `docs/grafana-dashboard.json`
4. Select Prometheus data source
5. Save dashboard

## Phase 6: Backup Setup (10 minutes)

### 6.1 Create Backup Script

```bash
# Make backup script executable
chmod +x scripts/backup_warm_storage.sh

# Test backup manually
AKOSHA_WARM_PATH=~/.akosha/dev/warm \
AKOSHA_BACKUP_BUCKET=akosha-backups-test \
./scripts/backup_warm_storage.sh
```

### 6.2 Schedule Cron Job (Kubernetes)

```bash
kubectl apply -f k8s/dev/cronjob-backup.yaml

# Verify cron job created
kubectl get cronjob -n akosha-dev

# Manually trigger backup for testing
kubectl create job test-backup \
  --from=cronjob/akosha-warm-backup \
  -n akosha-dev
```

## Validation Checklist

Run through this checklist to verify implementation:

```bash
#!/usr/bin/env bash
# validate_implementation.sh

echo "Validating warm tier storage implementation..."

# Check 1: Environment variable override
echo "[1/7] Testing environment variable override..."
export AKOSHA_WARM_PATH=/tmp/test_warm
python -c "from akosha.config import _resolve_warm_path; assert str(_resolve_warm_path()) == '/tmp/test_warm'"
echo "✅ Environment variable override works"

# Check 2: Default path for lite mode
echo "[2/7] Testing default path for lite mode..."
unset AKOSHA_WARM_PATH
export AKOSHA_MODE=lite
python -c "from akosha.config import _resolve_warm_path; from pathlib import Path; p = _resolve_warm_path(); assert p == Path.home() / '.akosha' / 'dev' / 'warm'"
echo "✅ Default path for lite mode correct"

# Check 3: Default path for standard mode
echo "[3/7] Testing default path for standard mode..."
export AKOSHA_MODE=standard
python -c "from akosha.config import _resolve_warm_path; from pathlib import Path; p = _resolve_warm_path(); assert str(p) == '/data/akosha/prod/warm'"
echo "✅ Default path for standard mode correct"

# Check 4: Path validation
echo "[4/7] Testing path validation..."
python -c "from akosha.config import get_config, validate_storage_config; c = get_config(); r = validate_storage_config(c); assert all(r.values())"
echo "✅ Path validation works"

# Check 5: No project directory pollution
echo "[5/7] Checking for project directory pollution..."
if [ -d "./data/warm" ]; then
    echo "❌ ERROR: ./data/warm exists in project directory"
    exit 1
fi
echo "✅ No project directory pollution"

# Check 6: Home directory structure
echo "[6/7] Checking home directory structure..."
if [ ! -d ~/.akosha/dev/warm ]; then
    echo "❌ ERROR: ~/.akosha/dev/warm does not exist"
    exit 1
fi
echo "✅ Home directory structure correct"

# Check 7: Configuration loading
echo "[7/7] Testing configuration loading..."
python -c "from akosha.config import get_config; c = get_config(); assert c.warm.path.exists()"
echo "✅ Configuration loading works"

echo ""
echo "🎉 All validation checks passed!"
```

## Migration Guide (Existing Data)

If you have existing data in `./data/warm`, migrate it to the new location:

```bash
#!/usr/bin/env bash
# migrate_existing_data.sh

set -euo pipefail

echo "Migrating existing warm storage data..."

# Old path
OLD_PATH="./data/warm"

# New path
NEW_PATH="${AKOSHA_WARM_PATH:-~/.akosha/dev/warm}"

if [ ! -d "$OLD_PATH" ]; then
    echo "No existing data found at $OLD_PATH"
    exit 0
fi

# Create backup
echo "Creating backup..."
BACKUP_PATH="./data/warm.backup.$(date +%Y%m%d_%H%M%S)"
cp -r "$OLD_PATH" "$BACKUP_PATH"

# Migrate data
echo "Migrating data to $NEW_PATH..."
mkdir -p "$NEW_PATH"
mv "$OLD_PATH"/* "$NEW_PATH/" 2>/dev/null || true

# Verify migration
echo "Verifying migration..."
if [ -f "$NEW_PATH/warm.db" ]; then
    echo "✅ Migration successful"
    echo "Backup saved at: $BACKUP_PATH"
    echo "You can safely remove the old directory after verification"
else
    echo "❌ Migration failed, restoring from backup"
    rm -rf "$OLD_PATH"
    mv "$BACKUP_PATH" "$OLD_PATH"
    exit 1
fi
```

## Troubleshooting

### Issue: "Permission denied when creating ~/.akosha"

**Solution**:
```bash
# Create directory with correct permissions
mkdir -p ~/.akosha/dev/{warm,wal}
chmod 755 ~/.akosha/dev/warm
chmod 755 ~/.akosha/dev/wal
```

### Issue: "Environment variable not taking effect"

**Solution**:
```bash
# Ensure environment variable is exported, not just set
export AKOSHA_WARM_PATH=/custom/path
python -c "from akosha.config import get_config; print(get_config().warm.path)"
```

### Issue: "Docker volume mount fails"

**Solution**:
```bash
# Create host directory before mounting
mkdir -p ~/.akosha/dev/warm
chmod 777 ~/.akosha/dev/warm

# Rebuild container
docker-compose down
docker-compose up --build
```

### Issue: "Kubernetes PVC stuck in Pending state"

**Solution**:
```bash
# Check PVC events
kubectl describe pvc akosha-warm-dev -n akosha-dev

# If using StorageClass with WaitForFirstConsumer, ensure pod is scheduled
kubectl get pods -n akosha-dev

# If needed, use immediate binding
kubectl patch storageclass standard -p '{"volumeBindingMode":"Immediate"}'
```

## Next Steps

After completing the quick start:

1. ✅ Read the full strategy document: `docs/WARM_TIER_STORAGE_STRATEGY.md`
2. ✅ Implement Docker configuration for production
3. ✅ Set up Kubernetes manifests for staging
4. ✅ Configure monitoring and alerting
5. ✅ Implement backup automation
6. ✅ Test disaster recovery procedures

## Summary

This quick start guide covers:

1. ✅ Configuration updates for dev and prod
2. ✅ Local development testing
3. ✅ Docker volume configuration
4. ✅ Kubernetes PVC setup
5. ✅ Monitoring with Prometheus/Grafana
6. ✅ Backup automation
7. ✅ Migration from old paths
8. ✅ Troubleshooting common issues

**Estimated time to complete**: 75 minutes

**Files modified**:
- `/Users/les/Projects/akosha/config/lite.yaml`
- `/Users/les/Projects/akosha/config/standard.yaml`
- `/Users/les/Projects/akosha/akosha/config.py`

**Files created**:
- `/Users/les/Projects/akosha/docs/WARM_TIER_STORAGE_STRATEGY.md`
- `/Users/les/Projects/akosha/docs/WARM_TIER_QUICKSTART.md` (this file)
