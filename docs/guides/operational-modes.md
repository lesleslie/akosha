# Akosha Operational Modes

Akosha supports two operational modes to balance ease of development with production scalability.

## Overview

| Feature | Lite Mode | Standard Mode |
|---------|-----------|---------------|
| **Setup Time** | 2 min | 5 min |
| **Services** | 1 (Akosha only) | 2 (Akosha + Redis) |
| **Hot Storage** | DuckDB in-memory | DuckDB in-memory |
| **Warm Storage** | DuckDB on-disk | DuckDB on-disk |
| **Cold Storage** | Disabled | S3/Azure/GCS |
| **Cache Layer** | In-memory only | Redis L2 cache |
| **Data Persistence** | No | Yes |
| **Ideal For** | Development, testing | Production, scale |

## Lite Mode

**Best for:** Development, testing, local experimentation

### Characteristics

- Zero external dependencies
- 2-minute setup time
- In-memory storage only
- Fastest startup time
- Data lost on restart
- Limited scalability

### Quick Start

```bash
# Start in lite mode (default)
akosha start --mode=lite

# Or use the shortcut script
./scripts/dev-start.sh lite
```

### Configuration

Lite mode uses `config/lite.yaml`:

```yaml
mode: lite
cache:
  backend: memory
storage:
  cold:
    enabled: false
```

### Use Cases

- **Local development**: Rapid iteration without service dependencies
- **Testing**: Unit tests and integration tests
- **Prototyping**: Quick feature validation
- **Learning**: Understanding Akosha's architecture

### Limitations

- No data persistence (lost on restart)
- No distributed caching
- No long-term storage
- Single-machine deployment only

## Standard Mode

**Best for:** Production, scaling, persistent storage

### Characteristics

- Redis caching layer
- Cloud storage for cold tier
- Production-ready scalability
- Persistent storage
- Requires external services
- Graceful degradation if services unavailable

### Quick Start

```bash
# 1. Start Redis (if not running)
docker run -d -p 6379:6379 --name redis redis:alpine

# 2. Configure cloud storage (optional)
export AWS_S3_BUCKET=akosha-cold-data
export AWS_S3_REGION=us-west-2

# 3. Start Akosha in standard mode
akosha start --mode=standard

# Or use the shortcut script
./scripts/dev-start.sh standard
```

### Configuration

Standard mode uses `config/standard.yaml`:

```yaml
mode: standard
cache:
  backend: redis
  host: localhost
  port: 6379
storage:
  cold:
    enabled: true
    backend: s3
    bucket: akosha-cold-data
```

### Use Cases

- **Production deployment**: Full scalability and persistence
- **Distributed systems**: Multiple instances with shared cache
- **Long-term storage**: Archival with cloud storage
- **High availability**: Redis clustering for resilience

### Features

- **Redis caching**: L2 cache for improved performance
- **Cloud storage**: S3, Azure Blob Storage, or Google Cloud Storage
- **Graceful degradation**: Falls back to in-memory if Redis unavailable
- **Horizontal scaling**: Multiple Akosha instances with shared cache

## Mode Comparison

### Feature Matrix

| Feature | Lite | Standard |
|---------|------|----------|
| **Setup Time** | 2 min | 5 min |
| **Services** | Akosha only | Akosha + Redis |
| **Cache** | In-memory | Redis + in-memory |
| **Cold Storage** | Disabled | S3/Azure/GCS |
| **Data Persistence** | No | Yes |
| **Scalability** | Single machine | Horizontal |
| **External Deps** | None | Redis, cloud storage |
| **Startup Time** | <1s | 2-3s |
| **Memory Usage** | Low | Medium |
| **Network I/O** | None | Medium |

### Performance Considerations

**Lite Mode:**
- Faster startup (no connection overhead)
- Lower memory usage (no Redis client)
- Simpler debugging (no distributed state)
- Limited by single machine resources

**Standard Mode:**
- Distributed caching (shared state)
- Better cache hit rates (L2 cache)
- Horizontal scaling capability
- Network latency for cache operations

## Migration Guide

### Lite → Standard

To migrate from lite to standard mode:

1. **Install Redis**:
   ```bash
   # Docker (recommended)
   docker run -d -p 6379:6379 --name redis redis:alpine

   # macOS
   brew install redis
   brew services start redis

   # Ubuntu/Debian
   sudo apt-get install redis-server
   sudo systemctl start redis
   ```

2. **Configure cloud storage** (optional):
   ```bash
   # AWS S3
   export AWS_S3_BUCKET=akosha-cold-data
   export AWS_S3_REGION=us-west-2
   export AWS_ACCESS_KEY_ID=your_key
   export AWS_SECRET_ACCESS_KEY=your_secret

   # Azure Blob Storage
   export AZURE_STORAGE_ACCOUNT=akosha_storage
   export AZURE_CONTAINER=akosha-cold

   # Google Cloud Storage
   export GCS_BUCKET=akosha-cold-data
   export GOOGLE_APPLICATION_CREDENTIALS=/path/to/credentials.json
   ```

3. **Start in standard mode**:
   ```bash
   akosha start --mode=standard
   ```

4. **Verify configuration**:
   ```bash
   # Check Redis connection
   redis-cli ping
   # Should return: PONG

   # Check Akosha mode
   akosha modes
   ```

### Standard → Lite

To switch from standard to lite mode:

```bash
# Simply change the mode flag
akosha start --mode=lite
```

**Important Notes:**

- Data in Redis and cloud storage will not be accessible in lite mode
- No data migration is performed
- Use this for testing or development only

## CLI Reference

### Starting Akosha

```bash
# Start in lite mode (default)
akosha start

# Start in standard mode
akosha start --mode=standard

# Start with custom host and port
akosha start --mode=standard --host 0.0.0.0 --port 9000

# Start with custom configuration
akosha start --mode=standard --config /path/to/config.yaml

# Start with verbose logging
akosha start --mode=standard --verbose
```

### Listing Modes

```bash
# List all available modes
akosha modes
```

Output:
```
Available operational modes:

  lite:
    Description: Lite mode: In-memory only, zero external dependencies
    Redis: Disabled
    Cold Storage: Disabled
    Cache Backend: memory
    External Services: None

  standard:
    Description: Standard mode: Full production configuration with Redis and cloud storage
    Redis: Enabled
    Cold Storage: Enabled
    Cache Backend: redis
    External Services: Required
```

### Admin Shell

```bash
# Launch shell in lite mode
akosha shell --mode=lite

# Launch shell in standard mode
akosha shell --mode=standard
```

## Troubleshooting

### Lite Mode Issues

#### Problem: Data lost on restart

**Solution:** This is expected behavior for lite mode. Use standard mode for persistent storage.

#### Problem: Out of memory errors

**Solution:** Reduce data volume or switch to standard mode with on-disk warm storage:

```bash
akosha start --mode=standard
```

#### Problem: Slow queries with large datasets

**Solution:** Lite mode is not designed for large-scale data. Switch to standard mode:

```bash
akosha start --mode=standard
```

### Standard Mode Issues

#### Problem: Redis connection refused

**Diagnosis:**
```bash
# Check if Redis is running
redis-cli ping

# Check if Redis is accessible
telnet localhost 6379
```

**Solution:** Start Redis or fall back to lite mode:

```bash
# Start Redis
docker start redis

# Or use lite mode
akosha start --mode=lite
```

#### Problem: Cloud storage permissions error

**Diagnosis:**
```bash
# Check AWS credentials
aws sts get-caller-identity

# Verify bucket access
aws s3 ls s3://akosha-cold-data
```

**Solution:**
1. Verify credentials are set correctly
2. Check bucket permissions
3. Ensure bucket exists

```bash
# Create bucket if needed
aws s3 mb s3://akosha-cold-data

# Set bucket policy
aws s3api put-bucket-policy --bucket akosha-cold-data --policy file://policy.json
```

#### Problem: High memory usage

**Diagnosis:**
```bash
# Check memory usage
ps aux | grep akosha

# Check Redis memory usage
redis-cli INFO memory
```

**Solution:**
1. Reduce Redis max memory:
   ```bash
   redis-cli CONFIG SET maxmemory 1gb
   redis-cli CONFIG SET maxmemory-policy allkeys-lru
   ```
2. Reduce Akosha cache size in configuration
3. Scale horizontally with multiple instances

## Best Practices

### Development Workflow

1. **Start with lite mode** for rapid prototyping
2. **Switch to standard mode** when you need persistence
3. **Use environment variables** for mode-specific config
4. **Test in both modes** before production deployment

```bash
# Development
export AKOSHA_MODE=lite
akosha start

# Staging
export AKOSHA_MODE=standard
akosha start --config config/staging.yaml

# Production
export AKOSHA_MODE=standard
akosha start --config config/production.yaml
```

### Production Deployment

For production, always use **standard mode** with:

1. **Redis cluster** for high availability
2. **Cloud storage** for cold tier
3. **Environment-based configuration**
4. **Health checks and monitoring**
5. **Graceful shutdown handling**

Example production configuration:

```yaml
# config/production.yaml
mode: standard

cache:
  backend: redis
  host: redis.production.internal
  port: 6379
  pool_size: 20
  max_connections: 100

storage:
  cold:
    enabled: true
    backend: s3
    bucket: akosha-production-data
    region: us-west-2

monitoring:
  metrics_enabled: true
  tracing_enabled: true
  health_check_interval: 30

security:
  authentication_enabled: true
  tls_enabled: true
```

### Configuration Management

**Use environment variables** for sensitive data:

```bash
# Redis configuration
export AKOSHA_REDIS_HOST=localhost
export AKOSHA_REDIS_PORT=6379
export AKOSHA_REDIS_PASSWORD=secret

# Cloud storage
export AWS_S3_BUCKET=akosha-cold-data
export AWS_S3_REGION=us-west-2
export AWS_ACCESS_KEY_ID=your_key
export AWS_SECRET_ACCESS_KEY=your_secret

# Security
export AKOSHA_JWT_SECRET=your_jwt_secret
export AKOSHA_TLS_CERT_PATH=/path/to/cert.pem
export AKOSHA_TLS_KEY_PATH=/path/to/key.pem
```

## Advanced Topics

### Custom Mode Configuration

You can create custom configurations by extending the mode system:

```python
# akosha/modes/custom.py
from akosha.modes.base import BaseMode, ModeConfig

class CustomMode(BaseMode):
    def get_mode_config(self) -> ModeConfig:
        return ModeConfig(
            name="custom",
            description="Custom mode configuration",
            redis_enabled=True,
            cold_storage_enabled=False,
            cache_backend="redis",
        )

    async def initialize_cache(self):
        # Custom cache initialization
        pass

    async def initialize_cold_storage(self):
        # Custom cold storage initialization
        pass

    @property
    def requires_external_services(self) -> bool:
        return True
```

### Multi-Instance Deployment

For horizontal scaling:

1. **Use standard mode** with shared Redis
2. **Configure unique instance IDs**
3. **Use load balancer** for traffic distribution

```bash
# Instance 1
akosha start --mode=standard --port 8682 --instance-id akosha-1

# Instance 2
akosha start --mode=standard --port 8683 --instance-id akosha-2

# Instance 3
akosha start --mode=standard --port 8684 --instance-id akosha-3
```

### Monitoring and Observability

**Lite mode:**
```bash
# Check logs
tail -f /var/log/akosha/akosha.log

# Check metrics
curl http://localhost:8682/metrics
```

**Standard mode:**
```bash
# Check Redis metrics
redis-cli INFO

# Check Akosha metrics
curl http://localhost:8682/metrics

# Check health
curl http://localhost:8682/health
```

## FAQ

**Q: Can I switch between modes without restarting?**

A: No, mode selection happens at startup. You must restart to change modes.

**Q: Will I lose data when switching from lite to standard?**

A: No, lite mode data is in-memory and lost on restart anyway. Standard mode provides persistent storage going forward.

**Q: Can I use standard mode without Redis?**

A: Yes, standard mode gracefully falls back to in-memory cache if Redis is unavailable. However, you'll lose distributed caching benefits.

**Q: Which mode should I use for testing?**

A: Use lite mode for unit tests and standard mode for integration tests that require persistence.

**Q: Can I use standard mode without cloud storage?**

A: Yes, cloud storage is optional. Standard mode will work with Redis caching only, but you won't have long-term cold storage.

**Q: How do I backup data in lite mode?**

A: Lite mode doesn't support backup. Use standard mode with cloud storage for automatic backup.

## Additional Resources

- [Architecture Guide](../ADR_001_ARCHITECTURE_DECISIONS.md)
- [Deployment Guide](../DEPLOYMENT_GUIDE.md)
- [Configuration Reference](../CONFIGURATION_REFERENCE.md)
- [API Documentation](../API.md)
- [Troubleshooting](../TROUBLESHOOTING.md)
