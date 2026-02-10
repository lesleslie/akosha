# Akosha Test Coverage Expansion Plan

**Project**: Akosha (Universal Memory Aggregation System)
**Goal**: Expand test coverage to 60%+ overall, 70%+ for core modules
**Current Status**: 32/32 tests passing (100% pass rate, but likely low coverage)

## Phase 1: Coverage Audit (Day 1)

### Tasks
1. Run coverage report: `pytest --cov=akosha --cov-report=html`
2. Identify low-coverage modules
3. Create test implementation plan

### Target Modules for Coverage

**Core Processing** (Priority: HIGH):
- `akosha/processing/embeddings.py` - Embedding service
- `akosha/processing/analytics.py` - Time-series analytics
- `akosha/processing/knowledge_graph.py` - Knowledge graph builder

**Storage** (Priority: HIGH):
- `akosha/storage/hot_store.py` - DuckDB hot store

**MCP Server** (Priority: MEDIUM):
- `akosha/mcp/server.py` - MCP server initialization
- `akosha/mcp/tools/akosha_tools.py` - MCP tools registration
- `akosha/mcp/auth.py` - Authentication

**CLI** (Priority: MEDIUM):
- `akosha/cli.py` - CLI commands

**Models & Config** (Priority: LOW):
- `akosha/models/` - Data models
- `akosha/config.py` - Configuration

## Phase 2: Core Tests (Days 2-3)

### Knowledge Graph Tests (`test_knowledge_graph.py`)

**Test Cases**:
1. Entity extraction
   - Extract system entity
   - Extract user entity
   - Extract project entity
   - Handle missing metadata

2. Relationship extraction
   - User worked on project
   - System contains project
   - Multiple entities and relationships

3. Graph operations
   - Add entities and edges
   - Get neighbors
   - Find shortest path (bidirectional BFS)
   - Get statistics

4. Edge cases
   - Empty graph
   - Disconnected entities
   - Path not found
   - Self-path (source == target)

### Hot Store Tests (`test_hot_store.py`)

**Test Cases**:
1. Initialization
   - In-memory database
   - File-based database
   - Schema creation
   - Index creation

2. Insert operations
   - Insert conversation
   - Insert duplicate (primary key violation)

3. Search operations
   - Vector similarity search
   - System filtering
   - Threshold filtering
   - Limit results

4. Code graph operations
   - Initialize code graphs table
   - Store code graph
   - Get code graph
   - List code graphs

5. Edge cases
   - Search with no results
   - Search below threshold
   - Uninitialized database

### Observability Tests (`test_observability.py`)

**Test Cases**:
1. OpenTelemetry setup
2. Tracing decorators
3. Metrics recording
4. Span attributes

## Phase 3: MCP Tools Tests (Days 4-5)

### MCP Tool Tests (`test_mcp_tools.py`)

**Test Cases**:
1. Search Tools
   - generate_embedding
   - generate_batch_embeddings
   - search_all_systems

2. Analytics Tools
   - get_system_metrics
   - analyze_trends
   - detect_anomalies
   - correlate_systems

3. Graph Tools
   - query_knowledge_graph
   - find_path
   - get_graph_statistics

4. System Tools
   - get_storage_status

### MCP Server Tests (`test_mcp_server.py`)

**Test Cases**:
1. Server initialization
2. Lifespan management
3. Tool registration
4. Authentication validation

## Phase 4: CLI Tests (Day 6)

### CLI Tests (`test_cli.py`)

**Test Cases**:
1. Shell command
2. Start command
3. Version command
4. Info command

## Phase 5: Integration Tests (Days 7-8)

### End-to-End Tests (`test_e2e.py`)

**Test Cases**:
1. Complete ingestion pipeline
2. Search workflow
3. Analytics workflow
4. Knowledge graph workflow

## Success Metrics

- Overall coverage: ≥ 60%
- Core modules coverage: ≥ 70%
- All tests passing: 100%
- Flaky tests: < 1%
- Test execution time: < 5 minutes

## Implementation Order

1. **Phase 1**: Coverage audit (identify gaps)
2. **Phase 2**: Core functionality tests (embeddings, analytics, knowledge graph, hot store)
3. **Phase 3**: MCP tools tests (11 tools)
4. **Phase 4**: CLI tests (4 commands)
5. **Phase 5**: Integration tests (end-to-end workflows)

## Testing Strategy

### Test Structure
```
tests/
├── unit/
│   ├── test_embeddings.py (existing)
│   ├── test_analytics.py (existing)
│   ├── test_knowledge_graph.py (NEW)
│   ├── test_hot_store.py (NEW)
│   ├── test_observability.py (NEW)
│   ├── test_mcp_tools.py (NEW)
│   ├── test_mcp_server.py (NEW)
│   └── test_cli.py (NEW)
└── integration/
    ├── test_mcp_integration.py (existing)
    └── test_e2e.py (NEW)
```

### Testing Principles
1. **Independence**: Each test should run independently
2. **Isolation**: Use fixtures for setup/teardown
3. **Clarity**: Descriptive test names and docstrings
4. **Coverage**: Test happy path + edge cases + error handling
5. **Performance**: Use async tests, mocks for external dependencies
6. **Maintainability**: Follow DRY, use helper functions

### Mocking Strategy
- Mock sentence-transformers (unavailable in test env)
- Mock DuckDB for unit tests
- Mock OpenTelemetry exporters
- Use real implementations for integration tests

## Next Steps

1. Run coverage report to establish baseline
2. Implement knowledge graph tests
3. Implement hot store tests
4. Implement MCP tools tests
5. Implement CLI tests
6. Implement integration tests
7. Verify coverage targets met
