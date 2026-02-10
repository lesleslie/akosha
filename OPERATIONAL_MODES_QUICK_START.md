# Akosha Operational Modes - Quick Start

## What's New?

Akosha now supports **two operational modes** for different deployment scenarios:

### Lite Mode (Default)
- **Zero dependencies** - Works out of the box
- **In-memory storage** - Fastest startup
- **Perfect for** - Development, testing, learning

### Standard Mode
- **Redis caching** - Distributed cache layer
- **Cloud storage** - S3/Azure/GCS support
- **Perfect for** - Production, scaling

## Quick Start

### Lite Mode (2 minutes)

```bash
# Clone and install
git clone https://github.com/yourusername/akosha.git
cd akosha
uv sync --group dev

# Start in lite mode (default)
uv run akosha start

# Or use the startup script
./scripts/dev-start.sh lite
```

That's it! Akosha is running with zero external dependencies.

### Standard Mode (5 minutes)

```bash
# 1. Start Redis
docker run -d -p 6379:6379 --name redis redis:alpine

# 2. (Optional) Configure cloud storage
export AWS_S3_BUCKET=akosha-cold-data
export AWS_S3_REGION=us-west-2

# 3. Start in standard mode
uv run akosha start --mode=standard

# Or use the startup script
./scripts/dev-start.sh standard
```

## CLI Commands

```bash
# List available modes
uv run akosha modes

# Start in specific mode
uv run akosha start --mode=lite
uv run akosha start --mode=standard

# Show system info
uv run akosha info

# Admin shell with mode
uv run akosha shell --mode=standard
```

## Mode Comparison

| Feature | Lite Mode | Standard Mode |
|---------|-----------|---------------|
| **Setup Time** | 2 min | 5 min |
| **Services** | Akosha only | Akosha + Redis |
| **Dependencies** | None | Redis (optional) |
| **Cache** | In-memory | Redis + in-memory |
| **Cold Storage** | Disabled | S3/Azure/GCS |
| **Data Persistence** | No | Yes |
| **Scalability** | Single machine | Horizontal |
| **Best For** | Development | Production |

## Which Mode Should I Use?

### Choose Lite Mode If:
- You're developing locally
- You're writing tests
- You're learning Akosha
- You don't need data persistence

### Choose Standard Mode If:
- You're deploying to production
- You need distributed caching
- You want long-term storage
- You're running multiple instances

## Documentation

- [Complete Guide](docs/guides/operational-modes.md) - Full documentation
- [Implementation Plan](OPERATIONAL_MODES_PLAN.md) - Design decisions
- [Completion Report](OPERATIONAL_MODES_COMPLETE.md) - Implementation details

## FAQ

**Q: Can I switch modes?**
A: Yes, just use the `--mode` flag. No migration needed (lite mode data is in-memory anyway).

**Q: Do I need Redis for standard mode?**
A: No, standard mode gracefully falls back to in-memory cache if Redis is unavailable.

**Q: Will I lose data in lite mode?**
A: Yes, lite mode doesn't persist data. Use standard mode for persistence.

**Q: Can I use standard mode without cloud storage?**
A: Yes, cloud storage is optional. You'll get Redis caching without long-term storage.

## Next Steps

1. **Try lite mode**: `uv run akosha start`
2. **Explore the docs**: [Complete Guide](docs/guides/operational-modes.md)
3. **Set up Redis**: `docker run -d -p 6379:6379 redis:alpine`
4. **Try standard mode**: `uv run akosha start --mode=standard`

---

**Questions?** Check the [complete guide](docs/guides/operational-modes.md) or [open an issue](https://github.com/yourusername/akosha/issues).
