______________________________________________________________________

## status: active role: canonical date: 2026-07-16 last_reviewed: 2026-07-16 superseded_by: null blocks_on: [] topic: auth

# Secret Management Guide

This document describes how to securely generate and manage secrets for Akosha deployments.

## Overview

Akosha requires several secrets for secure operation:

- **JWT_SECRET**: Used for signing JWT authentication tokens (required in production)
- **ENCRYPTION_KEY**: Used for data-at-rest encryption (optional)
- **External Service Credentials**: Redis, S3/R2, OpenTelemetry (optional)

## Security Principles

1. **Never commit secrets to version control**: Production secrets are gitignored
1. **Use cryptographically secure generation**: All secrets generated with `secrets` module
1. **Enforce minimum length**: JWT secrets must be ≥32 characters for HS256 algorithm
1. **Reject placeholders in production**: Startup validation prevents placeholder secrets
1. **Environment-specific controls**: Development is more permissive, production is strict

## Quick Start

### Development Setup

For local development, you can use test secrets:

```bash
export JWT_SECRET="development-test-secret-key-for-local-testing-min-32-chars"
export AUTH_ENABLED="true"
```

### Production Deployment

**Step 1: Generate production secrets**

```bash
# Secrets are now sourced from environment variables / Oneiric layered settings.
# See akosha/scripts/generate_secrets.py (deprecated 2026-09-10).
```

This creates environment-ready secrets with cryptographically secure values.

**Step 2: Review the generated secrets**

```bash
cat /tmp/akosha-secrets.env
```

**Step 3: Source the secrets into your shell**

```bash
source /tmp/akosha-secrets.env
mahavishnu mcp start
```

**Step 4: Verify secrets are loaded**

```bash
env | grep MAHAVISHNU_
curl http://localhost:8682/health
```

## Secret Generation Script

The `akosha/scripts/generate_secrets.py` script provides secure secret generation
(deprecated 2026-09-10 — Kubernetes manifests dropped, use env-based config):

### Features

- **Cryptographically secure**: Uses `secrets.token_urlsafe(32)` for 256-bit entropy
- **Template-based**: Supports custom templates via `--template` flag
- **Flexible output**: Specify output path with `--output` flag
- **Validation**: Ensures generated secrets meet minimum length requirements

### Usage

```bash
# Default usage (generates env-file at the path you pass via --output)
python -m akosha.scripts.generate_secrets --output /tmp/akosha-secrets.env
```

### Generated Secrets

| Secret | Length | Algorithm | Purpose |
|--------|--------|-----------|---------|
| JWT_SECRET | 43 chars | URL-safe base64 (32 bytes) | JWT token signing (HS256) |
| ENCRYPTION_KEY | 43 chars | URL-safe base64 (32 bytes) | AES-256 encryption |

## Environment-Specific Behavior

### Development (ENVIRONMENT=development)

- ✅ Allows test secrets
- ✅ No placeholder validation
- ⚠️ Still enforces minimum length (32 chars)
- ⚠️ Logs warning if auth disabled

### Production (ENVIRONMENT=production)

- ✅ Requires valid JWT_SECRET (if AUTH_ENABLED=true)
- ✅ Rejects placeholder secrets
- ✅ Enforces minimum length (32 chars)
- ❌ Startup fails if secrets are invalid

## Placeholder Rejection

In production, the following placeholder values are **rejected**:

- `change-this-in-production`
- `change-me`
- `placeholder`
- `""` (empty)
- `none`
- `null`
- `example`
- `test-secret`
- `jwt-secret-placeholder`

**Example error:**

```
RuntimeError: Authentication configuration failed: JWT_SECRET is using a placeholder value. Generate secure secrets with: python -m akosha.scripts.generate_secrets
```

## Kubernetes Secrets

> **Kubernetes manifests dropped 2026-09-10.** The `kubernetes/secret.yaml`,
> `kubernetes/secret.production.yaml`, and `kubernetes/secret.production.yaml.template`
> files no longer exist. Use environment variables and Oneiric layered settings
> for secrets management. The `akosha/scripts/generate_secrets.py` script is
> retained for reference but emits env-file output rather than k8s manifests.

## Validation

### Startup Validation

Akosha validates secrets on startup:

```python
# From akosha/mcp/auth.py
def validate_auth_config() -> bool:
    # 1. Check if auth is enabled
    # 2. Ensure JWT_SECRET is set
    # 3. Reject placeholders in production
    # 4. Enforce minimum length (32 chars)
```

**Startup failures:**

```
# Missing secret
RuntimeError: Authentication configuration failed: AUTH_ENABLED=true but JWT_SECRET is not set

# Too short
RuntimeError: Authentication configuration failed: JWT_SECRET too short (25 chars). Must be at least 32 characters for HS256 algorithm

# Placeholder in production
RuntimeError: Authentication configuration failed: JWT_SECRET is using a placeholder value
```

### Manual Validation

Test your secrets before deployment:

```bash
# Test JWT generation
python -c "
import os
os.environ['JWT_SECRET'] = 'your-secret-here-min-32-chars'
from akosha.security import generate_jwt_token
token = generate_jwt_token('test-user')
print(f'Token: {token[:50]}...')
"

# Test validation
python -c "
import os
os.environ['JWT_SECRET'] = 'your-secret-here-min-32-chars'
os.environ['ENVIRONMENT'] = 'production'
os.environ['AUTH_ENABLED'] = 'true'
from akosha.mcp.auth import validate_auth_config
validate_auth_config()
print('✅ Secrets validated')
"
```

## Rotation

### JWT Secret Rotation

**Recommended**: Rotate JWT secrets quarterly (every 3 months).

**Process**:

1. Generate new secret:

   ```bash
   python -m akosha.scripts.generate_secrets --output /tmp/akosha-secrets.new.env
   ```

1. Apply new secret:

   ```bash
   # Kubernetes manifests dropped 2026-09-10 — source the env file instead
   python -m akosha.scripts.generate_secrets --output /tmp/akosha-secrets.new.env
   source /tmp/akosha-secrets.new.env && mahavishnu mcp restart
   ```

1. Rolling restart pods:

   ```bash
   kubectl rollout restart deployment akosha-mcp -n akosha
   ```

1. Verify new tokens work:

   ```bash
   # Test authentication with new secret
   ```

1. **Important**: Old tokens will become invalid immediately. Plan rotation during low-traffic periods.

### Encryption Key Rotation

For data-at-rest encryption, key rotation is more complex:

1. **Stop ingestion**: Pause data ingestion to avoid mixed encryption
1. **Re-encrypt data**: Decrypt with old key, encrypt with new key
1. **Update secret**: Apply new secret to Kubernetes
1. **Restart services**: Rolling restart all pods
1. **Verify**: Check data accessibility and encryption

**Recommendation**: Consult security team before rotating encryption keys.

## Security Best Practices

### ✅ DO

- Generate secrets with `python -m akosha.scripts.generate_secrets`
- Store secrets in Kubernetes Secrets (not ConfigMaps)
- Use environment-specific secrets (dev/staging/prod)
- Rotate secrets regularly (quarterly for JWT, annually for encryption)
- Limit secret access to least-privilege service accounts
- Enable audit logging for secret access
- Use RBAC to control who can view/create secrets

### ❌ DON'T

- Commit secrets to git (use `.gitignore`)
- Share secrets via email/chat (use secret managers)
- Reuse secrets across environments
- Use weak/predictable secrets
- Store secrets in ConfigMaps or environment variables in manifests
- Log secrets or include them in error messages

## Troubleshooting

### "JWT_SECRET is not set"

**Cause**: Missing environment variable or Kubernetes secret.

**Fix**:

```bash
# Check if secret exists
kubectl get secrets -n akosha

# Check if secret has JWT_SECRET (Kubernetes manifests dropped 2026-09-10)
# Use env var check: echo $JWT_SECRET
# Or via Bodai orchestrator: mahavishnu status

# Generate and apply secret
python -m akosha.scripts.generate_secrets --output /tmp/akosha-secrets.env
source /tmp/akosha-secrets.env && mahavishnu mcp start
```

### "JWT_SECRET too short"

**Cause**: Secret is less than 32 characters (minimum for HS256).

**Fix**: Regenerate secret with proper length:

```bash
python -m akosha.scripts.generate_secrets
```

### "JWT_SECRET is using a placeholder value"

**Cause**: Production deployment is using placeholder secret.

**Fix**:

```bash
# Generate real secrets (Kubernetes manifests dropped 2026-09-10)
python -m akosha.scripts.generate_secrets --output /tmp/akosha-secrets.env

# Source and restart
source /tmp/akosha-secrets.env && mahavishnu mcp start
```

### Authentication works in dev but not production

**Cause**: Different environment variable (`ENVIRONMENT=production`).

**Fix**: Ensure you're using production secrets:

```bash
# Check environment
kubectl get deployment akosha-mcp -n akosha -o yaml | grep ENVIRONMENT

# Set production environment
kubectl set env deployment/akosha-mcp ENVIRONMENT=production -n akosha
```

## References

- **OWASP Secrets Management**: https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html
- **Kubernetes Secrets Best Practices**: https://kubernetes.io/docs/concepts/configuration/secret/
- **JWT Best Practices**: https://tools.ietf.org/html/rfc8725

## Appendix: Manual Secret Generation

If you need to generate secrets manually (not recommended):

```bash
# JWT Secret (32 bytes, URL-safe base64 = ~43 chars)
openssl rand -base64 32 | tr '+/' '-_' | tr -d '='

# Encryption Key (32 bytes, URL-safe base64 = ~43 chars)
openssl rand -base64 32 | tr '+/' '-_' | tr -d '='
```

**Note**: Use the provided script instead of manual generation for consistency.
