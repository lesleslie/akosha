# Akosha Entity Storage Identifier Analysis

**Date:** 2026-02-11
**Analysis For:** ULID Ecosystem Integration - Phase 1 Task 4

## Current Identifier Patterns

### GraphEntity Identifiers

**Location:** `/Users/les/Projects/akosha/akosha/processing/knowledge_graph.py:20-24`

**Current Format:**
- **System entities:** `f"system:{system_id}"` (e.g., `system:crackerjack`, `system:mahavishnu`)
- **User entities:** `f"user:{user_id}"` (e.g., `user:12345`, `user:admin`)
- **Other types:** `entity_type` field stores: `user`, `project`, `concept`, `error`, `system`

**Example entity IDs:**
```python
GraphEntity(
    entity_id="system:crackerjack",
    entity_type="system",
    properties={"name": "crackerjack"},
    source_system="crackerjack"
)

GraphEntity(
    entity_id="user:les",
    entity_type="user",
    properties={"user_id": "les"},
    source_system="unknown"
)
```

### GraphEdge Identifiers

**Location:** `/Users/les/Projects/akosha/akosha/processing/knowledge_graph.py:27-36`

**Current Format:**
- `source_id: str` - References source entity by its string ID
- `target_id: str` - References target entity by its string ID
- No semantic validation - any string is accepted

**Example edge IDs:**
```python
GraphEdge(
    source_id="user:les",
    target_id="system:mahavishnu",
    edge_type="worked_on",
    weight=1.0
)
```

## Storage Architecture

### In-Memory Storage
```python
# KnowledgeGraphBuilder stores entities in memory
self.entities: dict[str, GraphEntity] = {}
self.edges: list[GraphEdge] = []
```

**Observations:**
- No persistent storage (Dhruva) currently used for graph storage
- Graph is built dynamically from conversations
- No database schema for entities/edges
- Export/persistence happens via external MCP calls

### Validation Schema

**Location:** `/Users/les/Projects/akosha/akosha/mcp/validation.py:479-518`

**Entity ID Validation:**
```python
entity_id: str = Field(
    ...,
    min_length=1,
    max_length=200,
)

@field_validator("entity_id")
@classmethod
def validate_entity_id(cls, v: str) -> str:
    # Check for path traversal attempts
    # Check for safe characters
    # No format validation (allows any string)
```

**Security Validations:**
- Path traversal prevention (blocks `..`, `/`, `.` prefixes)
- Character restrictions (alphanumeric, hyphen, underscore, period)
- Length bounds (1-200 characters)

## Migration Requirements

### Changes Needed for ULID Integration

1. **Entity ID Format:**
   - Replace `f"system:{id}"` with ULID from Oneiric/Dhruva
   - Replace `f"user:{id}"` with ULID
   - Update validation to use `oneiric.core.ulid.is_ulid()` or `dhruva.ulid.is_ulid()`

2. **Graph Storage:**
   - Integrate Dhruva persistent storage for `entities` and `edges`
   - Use ULID as primary key in Dhruva BTree for fast lookups
   - Create Dhruva adapter in `akosha/processing/knowledge_graph.py`

3. **Foreign Key References:**
   - `GraphEdge.source_id` → ULID referencing `GraphEntity.entity_id`
   - `GraphEdge.target_id` → ULID referencing `GraphEntity.entity_id`
   - Update queries to use ULID-based joins

4. **Cross-System References:**
   - Keep `source_system` field for cross-system correlation
   - ULID embeds timestamp for time-based queries
   - Add `correlation_id` field to Mahavishnu workflow executions

## Estimated Record Counts

**Based on code analysis:**
- Entities: Dynamic (built from conversations)
- Edges: Dynamic (built from conversations)
- No fixed baseline (depends on conversation history)

**Migration Complexity:** LOW
- No existing large database to migrate
- In-memory storage can be switched to Dhruva incrementally
- ULID can be generated on-demand for new entities

## Recommended Migration Strategy

### Phase 1: Backend (Week 1-2)
1. Create Dhruva file storage adapter for knowledge graph
2. Modify `KnowledgeGraphBuilder` to use Dhruva `PersistentDict` for entities
3. Add ULID generation to entity creation flow
4. Update entity ID validation in schemas

### Phase 2: Frontend (Week 3)
1. Update MCP tools to accept ULID parameters
2. Update entity queries to use ULID lookups
3. Add ULID → legacy ID resolution for backward compatibility
4. Update OpenAPI documentation

### Migration SQL (Expand-Contract)

**Expand Phase:**
```sql
ALTER TABLE entities ADD COLUMN entity_ulid TEXT;
UPDATE entities SET entity_ulid = CONCAT('system:', entity_id) WHERE entity_ulid IS NULL;
```

**Switch Phase:**
```python
# Update application code to reference entity_ulid
# Keep entity_id for backward compatibility
```

**Contract Phase:**
```sql
ALTER TABLE entities DROP COLUMN entity_id;
-- After 30-day verification period
```

## Next Steps

1. ✅ **COMPLETED:** Analysis of current identifier patterns
2. **NEXT:** Design Dhruva BTree schema for knowledge graph
3. **NEXT:** Implement Dhruva storage adapter for Akosha
4. **NEXT:** Create migration scripts from custom IDs → ULID

**Status:** Analysis complete, ready for Task 5 (Crackerjack analysis)
