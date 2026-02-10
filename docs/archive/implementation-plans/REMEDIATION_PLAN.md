# Akosha Remediation Action Plan

**Date**: 2025-02-01
**Scope**: Address all issues from multi-agent code review
**Timeline**: 11 weeks (3 phases + hardening)
**Overall Effort**: 160-220 hours

## Executive Summary

The multi-agent code review identified **43 issues** across security, performance, testing, and documentation. This plan prioritizes fixes by severity to achieve production readiness within 11 weeks.

**Overall Quality Score**: 6.8/10 → 9.5/10 (after implementation)

---

## Phase 1: Critical Security Fixes (Week 1)

**Priority**: BLOCKER - Must complete before any other work
**Risk**: HIGH - Security vulnerabilities are exploitable
**Effort**: 40-60 hours

### 1.1 SQL Injection in hot_store.py (CRITICAL)

**File**: `akosha/storage/hot_store.py:121`

**Issue**: f-string query construction allows arbitrary SQL execution

**Fix**:
```python
# Replace f-string with parameterized query
if system_id:
    query = """
        SELECT ... FROM conversations
        WHERE system_id = ?
        ORDER BY similarity DESC LIMIT ?
    """
    results = self.conn.execute(query, [query_embedding, system_id, limit])
```

**Verification**:
- Run `bandit -r akosha/storage/` (must pass)
- Test with SQL injection payloads
- Ensure no f-strings in SQL queries

**Rollback**: Revert if performance degradation >10%

---

### 1.2 MCP Server Authentication (CRITICAL)

**Files**:
- Create: `akosha/mcp/auth.py`
- Modify: `akosha/mcp/server.py`

**Issue**: All MCP tools lack authentication

**Fix**:
```python
# Create JWT authentication middleware
@asynccontextmanager
async def lifespan(server: Any) -> AsyncGenerator[dict[str, Any]]:
    # Validate JWT_SECRET is set
    if not os.getenv("JWT_SECRET"):
        raise RuntimeError("JWT_SECRET required in production")
    # ... rest of initialization

# Decorator for protected tools
@require_auth
async def search_all_systems(...) -> dict[str, Any]:
    # Requires valid JWT token
```

**Verification**:
- Test with invalid/missing/expired tokens
- Verify all 15+ MCP tools require auth
- Load test: auth adds <5ms latency

**Rollback**: Feature flag `AUTH_ENABLED` (default: false for dev)

---

### 1.3 Secure Secret Management (CRITICAL)

**File**: `k8s/secret.yaml`

**Issue**: "change-this-in-production" placeholder secrets

**Fix**:
```bash
# Generate production secrets
JWT_SECRET=$(openssl rand -base64 32)
ENCRYPTION_KEY=$(openssl rand -base64 32)

# Create secret from environment
kubectl create secret generic akosha-secrets \
  --from-literal=JWT_SECRET="$JWT_SECRET" \
  --from-literal=ENCRYPTION_KEY="$ENCRYPTION_KEY" \
  --namespace=akosha \
  --dry-run=client -o yaml > k8s/secret.production.yaml
```

**Verification**:
- No default secrets in version control
- Validate secret rotation process
- Test deployment with generated secrets

---

### 1.4 Input Validation for MCP Tools (CRITICAL)

**Files**:
- Create: `akosha/mcp/validation.py`
- Modify: `akosha/mcp/tools/akosha_tools.py`

**Issue**: No parameter validation (DoS, injection vectors)

**Fix**:
```python
from pydantic import BaseModel, Field, field_validator

class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=10_000)
    limit: int = Field(10, ge=1, le=1000)
    threshold: float = Field(0.7, ge=-1.0, le=1.0)
    system_id: str | None = Field(None, max_length=100)

    @field_validator("system_id")
    def validate_system_id(cls, v):
        if v and not re.match(r'^[a-zA-Z0-9_-]+$', v):
            raise ValueError("Invalid system_id format")
        return v
```

**Verification**:
- Test all tools with malicious inputs
- Verify error messages are user-friendly
- Load test: validation adds <1ms latency

**Rollback**: Feature flag `VALIDATE_INPUT` (default: true)

---

### 1.5 Secure Temp File Creation (HIGH)

**File**: `akosha/storage/cold_store.py:128`

**Issue**: Predictable filenames allow symlink attacks

**Fix**:
```python
import tempfile

fd, temp_path = tempfile.mkstemp(
    suffix=".parquet",
    prefix="akosha_export_",
    text=False
)
temp_file = Path(temp_path)
os.chmod(temp_file, 0o600)  # Owner read/write only
```

**Verification**:
- Check temp files have mode 0600
- Verify filenames are random
- Test cleanup on error

---

### 1.6 JSON Schema Validation (HIGH)

**Files**:
- Create: `akosha/models/schemas.py`
- Modify: `akosha/ingestion/worker.py:154`

**Issue**: Unvalidated JSON parsing from external sources

**Fix**:
```python
from pydantic import BaseModel

class SystemMemoryUploadManifest(BaseModel):
    uploaded_at: datetime
    conversation_count: int = Field(..., ge=0, le=1_000_000)
    checksum: str = Field(..., pattern=r"^[a-f0-9]{64}$")

    @field_validator("files")
    def validate_filenames(cls, v):
        for filename in v:
            if ".." in filename or filename.startswith("/"):
                raise ValueError(f"Invalid filename: {filename}")
        return v
```

**Verification**:
- Test with valid and invalid manifests
- Verify error messages are descriptive
- Check performance impact (<5ms per manifest)

---

### Phase 1 Success Criteria

- [ ] All 6 critical security vulnerabilities fixed
- [ ] `bandit` scan passes with 0 issues
- [ ] Authentication required for all MCP tools
- [ ] No hardcoded secrets in codebase
- [ ] All user inputs validated
- [ ] Temp files use secure APIs
- [ ] JSON schemas validate all external data

---

## Phase 2: Performance & Test Fixes (Weeks 2-3)

**Priority**: HIGH - Performance issues block production
**Risk**: MEDIUM - Can be rolled back without data loss
**Effort**: 60-80 hours

### 2.1 O(n²) Embedding Algorithm (CRITICAL)

**File**: `akosha/processing/embeddings.py:273`

**Issue**: Nested loops for ranking (100-1000x slower than vectorized)

**Fix**:
```python
# Vectorized similarity computation (O(n) vs O(n²))
candidate_matrix = np.array(candidate_embeddings, dtype=np.float32)
similarities = np.dot(candidate_matrix, query_embedding)

# Find top-k using argpartition (O(n))
k = min(limit, len(similarities))
top_k_indices = np.argpartition(-similarities, k-1)[:k]
```

**Performance Gain**: 10-100x speedup for large candidate sets

**Verification**:
- Benchmark: 100k candidates <10ms
- Compare output with legacy (match within 1e-6)
- Memory usage: No significant increase

---

### 2.2 Batch Migration Optimization (HIGH)

**File**: `akosha/storage/aging.py:76`

**Issue**: Sequential processing (20-50x slower than batch)

**Fix**:
```python
BATCH_SIZE = 1000

for batch_start in range(0, total_records, BATCH_SIZE):
    batch = records_to_migrate[batch_start:batch_start+BATCH_SIZE]

    # Batch quantize embeddings (vectorized)
    compressed_embeddings = await self._quantize_embeddings_batch(
        [r["embedding"] for r in batch]
    )

    # Batch generate summaries (parallel)
    summaries = await asyncio.gather(*[
        self._generate_summary(r["content"]) for r in batch
    ])

    # Batch insert and delete
    await self.warm_store.insert_batch(warm_records)
    await self._delete_batch_from_hot_store(conversation_ids)
```

**Performance Gain**: 20-50x faster migration

**Verification**:
- Benchmark: 10k records <30 seconds
- Compare checksums before/after
- Verify no duplicate or missing records

**Rollback**: Feature flag `USE_BATCH_MIGRATION` (default: true)

---

### 2.3 Concurrent Ingestion Optimization (HIGH)

**File**: `akosha/ingestion/worker.py:108`

**Issue**: Nested loops with sequential downloads (20-30x slower)

**Fix**:
```python
# Concurrent discovery of all systems
system_tasks = []
async for prefix in self.storage.list("systems/"):
    system_id = str(prefix).strip("/").split("/")[-1]
    if system_id:
        system_tasks.append(self._scan_system(system_id))

# Concurrent processing
system_results = await asyncio.gather(*system_tasks, return_exceptions=True)

# Flatten results
for result in system_results:
    if isinstance(result, Exception):
        logger.error(f"System scan failed: {result}")
    else:
        uploads.extend(result)
```

**Performance Gain**: 20-30x faster discovery

**Verification**:
- Benchmark: 1000 uploads <30 seconds
- Check no duplicate uploads
- Verify all uploads processed

**Rollback**: Feature flag `USE_CONCURRENT_DISCOVERY` (default: true)

---

### 2.4 Database Indexing (HIGH)

**File**: `akosha/storage/hot_store.py:51`

**Issue**: No index on `system_id` (10-100x slower queries)

**Fix**:
```python
# Create indexes during initialization
self.conn.execute("""
    CREATE INDEX IF NOT EXISTS system_id_index
    ON conversations (system_id)
""")

self.conn.execute("""
    CREATE INDEX IF NOT EXISTS timestamp_index
    ON conversations (timestamp)
""")

self.conn.execute("""
    CREATE INDEX IF NOT EXISTS system_timestamp_index
    ON conversations (system_id, timestamp)
""")
```

**Performance Gain**: 10-100x faster filtered queries

**Verification**:
- `EXPLAIN` shows index usage
- Benchmark: filtered queries <10ms for 1M records
- No degradation for non-filtered queries

---

### 2.5 Test Fixes (HIGH)

**Files**:
- `tests/unit/test_aging.py` (11 failures)
- `tests/unit/test_cold_store.py` (10 failures)
- `tests/unit/test_graceful_shutdown.py` (5 failures)
- `tests/unit/test_ingestion_worker.py` (4 failures)

**Root Causes**:
1. Mock Oneiric storage adapters not stubbed
2. Missing shutdown logic in ingestion worker
3. Test fixtures missing async wrappers

**Fix Strategy**:
```python
# Mock Oneiric adapters in test fixtures
@pytest.fixture
async def cold_store():
    store = ColdStore(bucket="test-bucket")
    mock_adapter = mock.AsyncMock()
    store._storage_adapter = mock_adapter
    await store.initialize()
    return store

# Add graceful shutdown to lifespan
@asynccontextmanager
async def lifespan(server: Any):
    # ... startup ...
    yield
    # Graceful shutdown
    if ingestion_task and not ingestion_task.done():
        worker.stop()
        await asyncio.wait_for(ingestion_task, timeout=30.0)
```

**Verification**:
- All 27 tests now pass
- Coverage >85%
- CI/CD pipeline green

---

### Phase 2 Success Criteria

- [ ] Embedding ranking: 100k candidates <10ms
- [ ] Hot→Warm migration: 10k records <30 seconds
- [ ] Upload discovery: 1000 uploads <30 seconds
- [ ] Filtered queries: <10ms for 1M records
- [ ] All 27 failing tests now pass
- [ ] Test coverage >85%

---

## Phase 3: Moderate Issues (Month 2)

**Priority**: MEDIUM - Technical debt and hardening
**Risk**: LOW - Non-breaking improvements
**Effort**: 40-50 hours

### 3.1 Rate Limiting (MEDIUM)

**Files**:
- Create: `akosha/mcp/rate_limit.py`
- Modify: `akosha/mcp/tools/akosha_tools.py`

**Fix**:
```python
class RateLimiter:
    def __init__(self, requests_per_second: float = 10.0, burst_limit: int = 100):
        self.rate = requests_per_second
        self.burst = burst_limit
        self.tokens = defaultdict(lambda: burst_limit)

@require_rate_limit
async def search_all_systems(...) -> dict[str, Any]:
    # Enforces 10 req/s per user
```

**Verification**:
- Load test: 100 req/s should be rate limited
- Legitimate traffic not blocked
- Metrics: violations logged

---

### 3.2 Security Logging (MEDIUM)

**File**: Create `akosha/observability/security_logging.py`

**Fix**:
```python
class SecurityLogger:
    def log_auth_success(self, user_id: str, source_ip: str): ...
    def log_auth_failure(self, reason: str, source_ip: str): ...
    def log_rate_limit_exceeded(self, user_id: str): ...
    def log_sql_injection_attempt(self, query: str): ...
    def log_path_traversal_attempt(self, path: str): ...
```

**Verification**:
- All security events logged with proper severity
- Logs include structured JSON for SIEM
- No sensitive data leaked

---

### 3.3 Path Traversal Fix (MEDIUM)

**File**: `akosha/storage/sharding.py:67`

**Issue**: Path construction without validation

**Fix**:
```python
import re

if not re.match(r'^[a-zA-Z0-9_-]+$', system_id):
    raise ValueError("Invalid system_id format")

if ".." in system_id or system_id.startswith("/"):
    raise ValueError("Path traversal detected")

# Verify resolved path is within base_path
resolved_path.relative_to(base_path.resolve())
```

**Verification**:
- Path traversal attempts blocked
- Valid system_ids still work
- No performance impact

---

### 3.4 Graph Traversal Optimization (LOW)

**File**: `akosha/processing/knowledge_graph.py`

**Issue**: O(E) traversal (1000-10000x slower than bidirectional BFS)

**Fix**:
```python
def find_shortest_path(self, source_id: str, target_id: str):
    # Bidirectional BFS: O(b^(d/2)) vs O(b^d)
    forward_queue = deque([source_id])
    backward_queue = deque([target_id])
    # ... expand both frontiers until meeting point
```

**Performance Gain**: 1000-10000x faster for large graphs

**Verification**:
- Benchmark: 100k nodes <100ms
- Path correctness matches old implementation
- Memory: O(V + E) as expected

---

### 3.5 Bounded List Comprehensions (LOW)

**Files**: Multiple files with unbounded comprehensions

**Fix**:
```python
# Add limits to all comprehensions
MAX_CANDIDATES = 100_000
system_prefixes = []
async for prefix in self.storage.list("systems/"):
    system_prefixes.append(str(prefix))
    if len(system_prefixes) >= 10_000:
        logger.warning("System prefix limit reached")
        break
```

**Verification**:
- All comprehensions have size limits
- Memory bounded even with adversarial input
- No functional regressions

---

### Phase 3 Success Criteria

- [ ] Rate limiting: 10 req/s enforced
- [ ] Security events logged with structured format
- [ ] Path traversal attacks blocked
- [ ] Graph traversal: 100k nodes <100ms
- [ ] All list comprehensions bounded

---

## Phase 4: Hardening & Documentation (Month 3)

**Priority**: LOW - Quality of life improvements
**Risk**: MINIMAL - Documentation and monitoring
**Effort**: 20-30 hours

### Tasks

1. **Documentation**
   - Add docstrings to all public APIs
   - Create architecture diagrams
   - Document security controls
   - Write troubleshooting guide

2. **Monitoring & Alerting**
   - Add Prometheus metrics for all critical paths
   - Create Grafana dashboards
   - Setup alerting rules
   - Test alerting pipeline

3. **Disaster Recovery Testing**
   - Test backup/restore procedures
   - Simulate failure scenarios
   - Verify recovery time objectives (RTO)

---

## Critical Files Summary

### Phase 1 (Security)
- `akosha/storage/hot_store.py` - SQL injection fix, indexes
- `akosha/mcp/server.py` - Authentication middleware
- `akosha/mcp/auth.py` - NEW - JWT authentication
- `akosha/mcp/validation.py` - NEW - Input validation
- `k8s/secret.yaml` - Remove placeholder secrets
- `akosha/storage/cold_store.py` - Secure temp files
- `akosha/models/schemas.py` - NEW - JSON validation
- `akosha/ingestion/worker.py` - Schema validation

### Phase 2 (Performance & Tests)
- `akosha/processing/embeddings.py` - Vectorized ranking
- `akosha/storage/aging.py` - Batch migration
- `akosha/storage/warm_store.py` - Batch insert methods
- `akosha/ingestion/worker.py` - Concurrent discovery
- `tests/unit/test_aging.py` - Fix fixtures
- `tests/unit/test_cold_store.py` - Mock adapters
- `tests/unit/test_ingestion_worker.py` - Improve mocks

### Phase 3 (Moderate Issues)
- `akosha/mcp/rate_limit.py` - NEW - Rate limiting
- `akosha/observability/security_logging.py` - NEW - Security logging
- `akosha/storage/sharding.py` - Path traversal fix
- `akosha/processing/knowledge_graph.py` - Graph optimization

---

## Execution Timeline

| Phase | Duration | Effort | Risk | Issues Fixed |
|-------|----------|--------|------|--------------|
| **Phase 1** | 1 week | 40-60h | HIGH | 6 critical security |
| **Phase 2** | 2 weeks | 60-80h | MEDIUM | 4 performance + 27 tests |
| **Phase 3** | 4 weeks | 40-50h | LOW | 6 moderate issues |
| **Phase 4** | 4 weeks | 20-30h | MINIMAL | Documentation & monitoring |
| **Total** | **11 weeks** | **160-220h** | - | **43 issues** |

---

## Risk Mitigation

### High-Risk Changes

1. **Authentication Middleware** (Phase 1.2)
   - Risk: May break existing MCP clients
   - Mitigation: Feature flag, gradual rollout, client updates

2. **Batch Migration** (Phase 2.2)
   - Risk: Data corruption during bulk operations
   - Mitigation: Test on test data first, checksum validation

3. **Concurrent Ingestion** (Phase 2.3)
   - Risk: Rate limiting by storage backend
   - Mitigation: Configurable concurrency, exponential backoff

### Rollback Strategies

- Feature flags for all major changes
- A/B testing before full rollout
- Database backups before schema changes
- Monitoring for regressions
- Gradual rollout (10% → 50% → 100%)

---

## Success Criteria

### Production Readiness

✅ **All Critical Security Issues Resolved**
- No SQL injection vulnerabilities
- Authentication required for all MCP tools
- All inputs validated
- No hardcoded secrets
- Secure temp file creation
- JSON schema validation

✅ **Performance Targets Met**
- Embedding ranking: 100k candidates <10ms
- Hot→Warm migration: 10k records <30s
- Upload discovery: 1000 uploads <30s
- Search latency (p50): <50ms
- Search latency (p99): <200ms
- Ingestion throughput: 1000 uploads/minute

✅ **Test Coverage & Quality**
- All 176 tests passing
- Coverage >85%
- Zero critical vulnerabilities in security scans
- CI/CD pipeline green

✅ **Operational Readiness**
- Comprehensive documentation
- Monitoring and alerting deployed
- Disaster recovery tested
- Security logging enabled

---

## Next Steps

1. **Review this plan** with team for feedback
2. **Create tracking issues** in project management system
3. **Setup feature flags** for high-risk changes
4. **Allocate developer time** (2-3 senior devs recommended)
5. **Begin Phase 1** with security fixes

**This plan provides a structured, risk-managed approach to achieving production readiness within 11 weeks.**
