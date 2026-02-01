# Python Coding Standards for Akosha

This document defines the coding standards and best practices for the Akosha project.

## Code Style

### Formatting

- **Line Length**: Maximum 100 characters (Ruff enforced)
- **Indentation**: 4 spaces (no tabs)
- **Imports**: Group imports into three sections (stdlib, third-party, local)
- **Quotes**: Prefer double quotes for strings and docstrings

```python
# ✅ Good
import asyncio
from pathlib import Path

import fastapi
from pydantic import settings

from akosha.storage import HotStore

# ❌ Bad
import asyncio, pathlib
import fastapi
from akosha.storage import *
```

### Naming Conventions

- **Modules**: `snake_case` (e.g., `hot_store.py`)
- **Classes**: `PascalCase` (e.g., `HotStore`)
- **Functions/Methods**: `snake_case` (e.g., `get_connection()`)
- **Constants**: `UPPER_SNAKE_CASE` (e.g., `MAX_RETRIES`)
- **Private**: `_leading_underscore` (e.g., `_internal_method()`)

```python
# ✅ Good
class VectorIndexer:
    MAX_DIMENSIONS = 1536

    def __init__(self, config: Config):
        self._conn = None

    def _internal_method(self):
        pass

# ❌ Bad
class vectorIndexer:
    max_dimensions = 1536
```

## Type Hints

### Mandatory Type Annotations

- **All functions** must have return type annotations
- **All parameters** must have type annotations
- **Use modern Python 3.13+ syntax** (built-in types, pipe operator)

```python
# ✅ Good - Modern Python 3.13 syntax
from typing import TypeAlias

UserId: TypeAlias = str

def get_user(user_id: UserId) -> dict[str, str] | None:
    """Get user by ID with comprehensive type hints."""
    if not user_id:
        return None
    return {"id": user_id}

# ❌ Bad - No type hints
def get_user(user_id):
    if not user_id:
        return None
    return {"id": user_id}

# ❌ Bad - Old syntax (unless necessary for compatibility)
from typing import Dict, Optional

def get_user(user_id: str) -> Optional[Dict[str, str]]:
    pass
```

### Complex Type Hints

Use `TypeAlias` for complex types to improve readability:

```python
# ✅ Good
from typing import TypeAlias

Embedding: TypeAlias = list[float]  # or np.ndarray
Metadata: TypeAlias = dict[str, str | int | float]

def index_content(content: str, embedding: Embedding, meta: Metadata) -> bool:
    """Index content with embedding and metadata."""
    pass

# ❌ Bad - Hard to read
def index_content(content: str, embedding: list[float], meta: dict[str, str | int | float]) -> bool:
    pass
```

## Async/Await Patterns

### When to Use Async

- **Always use async for I/O operations**: Database, storage, network calls
- **Use executor threads for blocking operations**: File I/O, CPU-intensive work
- **Hybrid pattern**: Async signature with sync internal operations

```python
# ✅ Good - Hybrid pattern
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial

class StorageManager:
    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=4)

    async def store_batch(self, items: list[Item]) -> None:
        """Async signature for API consistency."""
        loop = asyncio.get_event_loop()
        # Run blocking I/O in executor thread
        await loop.run_in_executor(
            self._executor,
            partial(self._write_to_disk, items)
        )

    def _write_to_disk(self, items: list[Item]) -> None:
        """Sync operation - runs in executor thread."""
        with open("data.bin", "wb") as f:
            for item in items:
                f.write(item.serialize())

# ❌ Bad - Blocking async
async def store_batch(self, items: list[Item]) -> None:
    # This blocks the event loop!
    with open("data.bin", "wb") as f:
        for item in items:
            f.write(item.serialize())
```

## Error Handling

### Never Suppress Exceptions Silently

```python
# ✅ Good - Log and handle
import structlog

logger = structlog.get_logger()

async def process_upload(upload_id: str) -> bool:
    """Process upload with proper error handling."""
    try:
        await _validate_upload(upload_id)
        return True
    except ValidationError as e:
        logger.error("Validation failed", upload_id=upload_id, error=str(e))
        return False
    except Exception as e:
        logger.exception("Unexpected error processing upload", upload_id=upload_id)
        raise  # Re-raise unexpected exceptions

# ❌ Bad - Silent suppression
async def process_upload(upload_id: str) -> bool:
    try:
        await _validate_upload(upload_id)
        return True
    except Exception:
        pass  # Never do this!
    return False
```

### Structured Error Messages

```python
# ✅ Good - Structured with context
raise StorageError(
    "Failed to write to hot store",
    shard_id=shard_id,
    record_count=len(records),
    error=str(e)
)

# ❌ Bad - No context
raise StorageError("Write failed")
```

## Docstrings

### Google Style Docstrings

```python
# ✅ Good - Complete docstring
def store_conversation(
    content: str,
    system_id: str,
    embedding: list[float] | None = None,
) -> str:
    """Store a conversation in the hot store.

    Args:
        content: The conversation text content.
        system_id: Unique identifier for the source system.
        embedding: Optional pre-computed vector embedding.
            If None, will be computed automatically.

    Returns:
        The unique conversation ID.

    Raises:
        StorageError: If storage operation fails.
        ValidationError: If content is empty or invalid.

    Examples:
        >>> conv_id = store_conversation("Hello world", "sys-001")
        >>> print(conv_id)
        'conv-12345'
    """
    pass

# ❌ Bad - Minimal or no docstring
def store_conversation(content, system_id, embedding=None):
    """Store conversation."""
    pass
```

## Testing

### Test Organization

```python
# ✅ Good - Clear test structure
import pytest

class TestHotStore:
    """Test suite for HotStore functionality."""

    @pytest.fixture
    def store(self):
        """Create a fresh store for each test."""
        return HotStore()

    def test_store_conversation_success(self, store):
        """Test successful conversation storage."""
        conv_id = store.store_conversation("test content", "sys-001")
        assert conv_id is not None
        assert conv_id.startswith("conv-")

    @pytest.mark.integration
    def test_store_with_persistence(self, store):
        """Test that data persists across connections."""
        pass

    @pytest.mark.slow
    def test_bulk_insert_performance(self, store):
        """Test bulk insert performance with 10K records."""
        pass

# ❌ Bad - No structure, unclear intent
def test_store():
    store = HotStore()
    conv_id = store.store_conversation("test", "sys")
    assert conv_id
```

### Test Markers

Use appropriate markers for categorization:

```python
@pytest.mark.unit
def test_specific_function():  # Fast, isolated
    pass

@pytest.mark.integration
def test_with_database():  # Requires database
    pass

@pytest.mark.slow
def test_large_dataset():  # Takes >1 second
    pass

@pytest.mark.performance
def test_query_latency():  # Benchmark
    pass

@pytest.mark.network
def test_api_call():  # Requires network
    pass

@pytest.mark.security
def test_sql_injection_protection():  # Security test
    pass
```

## Imports Organization

### Import Order

1. Standard library imports
2. Third-party imports
3. Local application imports
4. Each section separated by blank line

```python
# ✅ Good
import asyncio
from pathlib import Path
from typing import TypeAlias

import fastapi
from pydantic import BaseModel

from akosha.storage import HotStore
from akosha.processing import VectorIndexer

# ❌ Bad - Mixed, no separation
import asyncio
from akosha.storage import HotStore
import fastapi
from pathlib import Path
```

### Avoid Wildcard Imports

```python
# ✅ Good - Explicit imports
from akosha.storage import HotStore, WarmStore, ColdStore

# ❌ Bad - Wildcard (pollutes namespace)
from akosha.storage import *
```

## Code Organization

### Module Structure

```python
# ✅ Good - Clear organization
"""Module docstring."""

# 1. Standard library imports
import asyncio
from typing import TypeAlias

# 2. Third-party imports
import fastapi
from pydantic import BaseModel

# 3. Local imports
from akosha.core import Config

# 4. Type definitions
UserId: TypeAlias = str

# 5. Constants
MAX_RETRIES = 3
TIMEOUT_SECONDS = 30

# 6. Classes
class MyClass:
    pass

# 7. Functions
def my_function():
    pass

# 8. Main guard
if __name__ == "__main__":
    pass
```

## Performance Considerations

### Avoid Premature Optimization

```python
# ✅ Good - Clear and correct first
def process_items(items: list[Item]) -> list[Item]:
    """Process items, filtering out invalid ones."""
    return [item for item in items if item.is_valid()]

# Optimize only if profiling shows it's a bottleneck
# ❌ Bad - Premature optimization, harder to read
def process_items(items: list[Item]) -> list[Item]:
    result = []
    append = result.append  # "Optimization"
    for item in items:
        if item.is_valid():
            append(item)
    return result
```

### Use Appropriate Data Structures

```python
# ✅ Good - Right tool for the job
def lookup_user(user_ids: set[str], target: str) -> bool:
    """O(1) lookup with set."""
    return target in user_ids

# ❌ Bad - Wrong data structure
def lookup_user(user_ids: list[str], target: str) -> bool:
    """O(n) lookup with list."""
    return target in user_ids
```

## Security

### Input Validation

```python
# ✅ Good - Validate inputs
from pydantic import BaseModel, validator

class StoreRequest(BaseModel):
    content: str
    system_id: str

    @validator('content')
    def content_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError('content cannot be empty')
        return v

    @validator('system_id')
    def system_id_format(cls, v):
        if not v.startswith('sys-'):
            raise ValueError('system_id must start with sys-')
        return v

# ❌ Bad - No validation
def store_content(content: str, system_id: str):
    # No validation!
    pass
```

### SQL Injection Prevention

```python
# ✅ Good - Parameterized queries
def get_user(user_id: str) -> dict:
    cursor.execute(
        "SELECT * FROM users WHERE id = ?",
        (user_id,)
    )

# ❌ Bad - SQL injection vulnerable
def get_user(user_id: str) -> dict:
    cursor.execute(
        f"SELECT * FROM users WHERE id = '{user_id}'"
    )
```

## Logging

### Structured Logging

```python
# ✅ Good - Structured logging
import structlog

logger = structlog.get_logger()

def process_upload(upload_id: str):
    logger.info("Processing upload", upload_id=upload_id)
    try:
        # ... processing ...
        logger.info("Upload processed successfully",
                   upload_id=upload_id,
                   record_count=100)
    except Exception as e:
        logger.error("Upload processing failed",
                    upload_id=upload_id,
                    error=str(e),
                    error_type=type(e).__name__)
        raise

# ❌ Bad - Unstructured logging
import logging

def process_upload(upload_id: str):
    logging.info(f"Processing upload {upload_id}")
    # ...
    logging.info(f"Upload {upload_id} processed with {100} records")
```

## Configuration

### Use Pydantic Settings

```python
# ✅ Good - Type-safe configuration
from pydantic import Field
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    hot_store_path: Path = Field(default=Path("/data/akosha/hot"))
    warm_store_path: Path = Field(default=Path("/data/akosha/warm"))
    max_batch_size: int = Field(default=1000, ge=1, le=10000)
    timeout_seconds: int = Field(default=30, ge=1)

    class Config:
        env_prefix = "AKOSHA_"

settings = Settings()

# ❌ Bad - Untyped, no validation
HOT_STORE_PATH = os.getenv("AKOSHA_HOT_PATH", "/data/akosha/hot")
MAX_BATCH_SIZE = int(os.getenv("AKOSHA_MAX_BATCH", "1000"))
```

## Summary

**Key Principles:**

1. **Type Safety**: Comprehensive type hints on all functions
2. **Error Handling**: Never suppress exceptions, use structured logging
3. **Async Patterns**: Use async for I/O, executors for blocking work
4. **Testing**: Comprehensive tests with appropriate markers
5. **Code Style**: Follow Ruff formatting (100 char line length)
6. **Security**: Validate inputs, use parameterized queries
7. **Documentation**: Google-style docstrings with examples
8. **Performance**: Optimize based on profiling, not speculation

**Quality Gates:**

- All code must pass: `crackerjack lint`
- All code must pass: `crackerjack typecheck`
- All code must pass: `crickerjack test` (85%+ coverage)
- All code must pass: `crackerjack security`
