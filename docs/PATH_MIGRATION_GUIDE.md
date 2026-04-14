# Akosha Storage Path Guide

## Summary

Akosha uses **environment-aware path resolution**. By default, data files live outside the project directory and are resolved to platform-standard locations.

## What Changed

### Project-Local Layout

```
akosha/
├── data/
│   ├── warm/
│   │   └── warm.db
│   └── cold/
│       └── cache/
```

**Problems**:

- ❌ Pollutes git repository
- ❌ Can't have multiple instances per project
- ❌ Data tracked in git accidentally
- ❌ Violates container best practices

### Current Layout

```
~/.local/share/akosha/          # Linux default
├── warm/
│   └── warm.db
├── wal/
├── cold/
│   └── cache/
└── migrations/

# OR on macOS:
~/Library/Application Support/akosha/

# OR in containers:
/data/akosha/
├── warm/
├── wal/
└── cold/
```

**Benefits**:

- ✅ Clean project directory
- ✅ Industry standard locations
- ✅ Container-friendly (volume mounting)
- ✅ Cross-platform compatibility
- ✅ Survives project clones and branch switches

## Paths by Environment

| Environment | Base Path | Warm Store | Config |
|-------------|-----------|------------|--------|
| **Local Dev** (Linux) | `~/.local/share/akosha/` | `~/.local/share/akosha/warm/warm.db` | `~/.config/akosha/` |
| **Local Dev** (macOS) | `~/Library/Application Support/akosha/` | `~/Library/Application Support/akosha/warm/warm.db` | `~/.config/akosha/` |
| **Container/Prod** | `/data/akosha/` | `/data/akosha/warm/warm.db` | `/etc/akosha/` |
| **Testing** | `/tmp/akosha/test/` | `/tmp/akosha/test/warm/warm.db` | `/tmp/akosha/test/config/` |

## Environment Variables

You can override any path using environment variables:

```bash
# Override base data path
export AKOSHA_DATA_PATH=/custom/akosha

# Override warm store specifically
export AKOSHA_WARM_PATH=/custom/warm

# Force environment
export AKOSHA_ENV=container  # or 'local', 'development', 'test'
```

## Migration Instructions

### Step 1: Check Current Status

```bash
# Check current storage paths
python -c "
from akosha.storage.path_resolver import StoragePathResolver
resolver = StoragePathResolver()
print(f'New location: {resolver.base_path}')
"
```

### Step 2: Migrate Existing Data

```bash
# Dry run to preview changes
akosha migrate data --dry-run

# Move data
akosha migrate data

# Verify current storage paths
akosha migrate status
```

### Step 3: Remove the Source Directory

```bash
# After verifying the move, remove the source directory if it is no longer needed
rm -rf ./data/

# Update .gitignore if needed
git add .gitignore
git commit -m "chore: update data storage paths"
```

## Configuration Files

Configuration files now use `null` for paths, which are resolved automatically by `StoragePathResolver`:

```yaml
# config/lite.yaml
storage:
  warm:
    path: null  # Resolved to ~/.local/share/akosha/warm

# config/standard.yaml
storage:
  warm:
    path: null  # Resolved to /data/akosha/warm (in containers)
```

## Docker/Production Usage

### Docker Compose

```yaml
version: '3.8'
services:
  akosha:
    image: akosha:latest
    environment:
      - AKOSHA_ENV=container
      - AKOSHA_DATA_PATH=/data/akosha
    volumes:
      - akosha-data:/data/akosha
    ports:
      - "8682:8682"

volumes:
  akosha-data:
    driver: local
```

### Kubernetes

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: akosha-warm-storage
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 100Gi
  storageClassName: ssd

---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: akosha
spec:
  template:
    spec:
      volumes:
        - name: akosha-data
          persistentVolumeClaim:
            claimName: akosha-warm-storage
      containers:
        - name: akosha
          volumeMounts:
            - name: akosha-data
              mountPath: /data/akosha
          env:
            - name: AKOSHA_ENV
              value: "container"
            - name: AKOSHA_DATA_PATH
              value: "/data/akosha"
```

## Troubleshooting

### Project-local data warning

If you see a warning about project-local data:

```
⚠️  Project-local data detected at: /path/to/project/data
    Run 'akosha migrate data' to move it to the default storage location.
```

**Solution**: Run the migration command to move your data.

### Permission errors

If you get permission errors creating directories:

```bash
# Create directory manually
mkdir -p ~/.local/share/akosha/warm

# Or use alternative location
export AKOSHA_DATA_PATH=./akosha-data
```

### Database not found

If Akosha can't find your existing database:

```bash
# Check where it's looking
akosha migrate status

# If you need to force a specific path, set an override
export AKOSHA_WARM_PATH=./data/warm
```

## Files Changed

- ✅ `akosha/storage/path_resolver.py` - New (environment-aware path resolution)
- ✅ `akosha/config.py` - Updated (uses StoragePathResolver)
- ✅ `akosha/storage/__init__.py` - Updated (exports path resolver)
- ✅ `config/lite.yaml` - Updated (paths resolved dynamically)
- ✅ `config/standard.yaml` - Updated (paths resolved dynamically)
- ✅ `akosha/cli/commands/migrate.py` - Migration CLI commands

## Compatibility Notes

The `./data/` project-local path remains supported during the transition period, but it is no longer the default. Future versions may remove the compatibility path.

## Questions?

- **Why not use project-relative paths?** See the agent consultation summaries for detailed architectural rationale.
- **What about Windows?** The path resolver detects Windows and uses `%LOCALAPPDATA%\akosha\`
- **Can I still use a project-local path?** Yes, set `AKOSHA_ENV=development` to use `.akosha/data/` in the project directory

## Migration CLI Commands

```bash
# Show current paths
akosha migrate status

# Preview migration (dry run)
akosha migrate data --dry-run

# Perform migration
akosha migrate data

# Copy data back if needed
akosha migrate rollback ./data/warm
```
