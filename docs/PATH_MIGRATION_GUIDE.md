# Akosha Storage Path Migration Guide

## Summary

Akosha has been updated to use **environment-aware path resolution** with XDG Base Directory compliance. This means data files are no longer stored in the project directory (`./data/`) but in platform-standard locations.

## What Changed

### Before (Legacy)

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

### After (XDG-Compliant)

```
~/.local/share/akosha/          # Linux/XDG
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
- ✅ Survives project clones/branch switches

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
# Check if migration is needed
python -c "
from akosha.storage.path_resolver import StoragePathResolver
resolver = StoragePathResolver()
print(f'New location: {resolver.base_path}')
"
```

### Step 2: Migrate Existing Data

```bash
# Dry run (preview changes)
akosha migrate data --dry-run

# Perform migration
akosha migrate data

# Verify migration
akosha migrate status
```

### Step 3: Clean Up Legacy Data

```bash
# After successful migration, remove old data
rm -rf ./data/

# Update .gitignore (remove data/ entry if present)
git add .gitignore
git commit -m "chore: migrate to XDG-compliant data paths"
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

### Legacy path warnings

If you see warnings about legacy paths:

```
⚠️  Legacy data path detected: /path/to/project/data
    New location: ~/.local/share/akosha
    Run 'akosha migrate data' to transfer data.
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

# If using legacy path, set override
export AKOSHA_WARM_PATH=./data/warm
```

## Files Changed

- ✅ `akosha/storage/path_resolver.py` - New (environment-aware path resolution)
- ✅ `akosha/config.py` - Updated (uses StoragePathResolver)
- ✅ `akosha/storage/__init__.py` - Updated (exports path resolver)
- ✅ `config/lite.yaml` - Updated (paths resolved dynamically)
- ✅ `config/standard.yaml` - Updated (paths resolved dynamically)
- ✅ `akosha/cli/commands/migrate.py` - New (migration CLI commands)

## Backward Compatibility

The old `./data/` paths will continue to work during a transition period, but you'll see warnings. Future versions will remove this support.

## Questions?

- **Why not use project-relative paths?** See the agent consultation summaries for detailed architectural rationale.
- **What about Windows?** The path resolver detects Windows and uses `%LOCALAPPDATA%\akosha\`
- **Can I still use project-local paths?** Yes, set `AKOSHA_ENV=development` to use `.akosha/data/` in project dir

## Migration CLI Commands

```bash
# Show current paths
akosha migrate status

# Preview migration (dry run)
akosha migrate data --dry-run

# Perform migration
akosha migrate data

# Rollback if needed
akosha migrate rollback ./data/warm
```
