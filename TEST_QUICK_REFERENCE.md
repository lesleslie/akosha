# Akosha Test Quick Reference

Quick reference guide for running and maintaining Akosha tests.

## Test Structure

```
tests/
├── unit/
│   ├── test_embeddings.py          # Embedding service (14 tests)
│   ├── test_analytics.py            # Time-series analytics (14 tests)
│   ├── test_knowledge_graph.py      # Knowledge graph (60 tests) ✨ NEW
│   ├── test_hot_store.py            # DuckDB hot store (30 tests)
│   └── test_cli.py                  # CLI commands (10 tests) ✨ NEW
└── integration/
    └── test_mcp_integration.py      # MCP integration (8 tests)
```

## Running Tests

### All Tests
```bash
cd /Users/les/Projects/akosha
pytest
```

### Specific Test File
```bash
pytest tests/unit/test_knowledge_graph.py -v
```

### With Coverage Report
```bash
pytest --cov=akosha --cov-report=html --cov-report=term-missing
open htmlcov/index.html
```

### By Test Type
```bash
pytest -m unit              # Unit tests only
pytest -m integration       # Integration tests only
pytest -m "not slow"        # Skip slow tests
```

### Verbose Output
```bash
pytest -v                   # Verbose mode
pytest -vv                  # Extra verbose
pytest -s                   # Show print output
```

### Specific Test
```bash
pytest tests/unit/test_knowledge_graph.py::TestKnowledgeGraphBuilder::test_initialization -v
```

## Test Coverage

| Module | File | Coverage | Tests |
|--------|------|----------|-------|
| Embeddings | test_embeddings.py | 85-90% | 14 |
| Analytics | test_analytics.py | 80-85% | 14 |
| Knowledge Graph | test_knowledge_graph.py | 75-80% | 60 |
| Hot Store | test_hot_store.py | 70-75% | 30 |
| CLI | test_cli.py | 60-70% | 10 |
| MCP Integration | test_mcp_integration.py | 50-60% | 8 |

## Writing New Tests

### Test Template
```python
"""Tests for <module_name>."""

from __future__ import annotations

import pytest

from akosha.<module_path> import <ClassOrFunction>


class Test<ClassName>:
    """Test suite for <ClassName>."""

    @pytest.fixture
    async def setup(self) -> <ClassName>:
        """Create fresh instance for each test."""
        instance = <ClassName>()
        yield instance
        # Cleanup

    @pytest.mark.asyncio
    async def test_<functionality>(self, setup: <ClassName>) -> None:
        """Test <what this tests>."""
        # Arrange
        <setup_code>

        # Act
        result = await <action>()

        # Assert
        assert result == <expected>
```

### Test Naming Conventions
- Test files: `test_<module_name>.py`
- Test classes: `Test<ClassName>`
- Test methods: `test_<functionality>_<scenario>`

### Markers
```python
@pytest.mark.unit              # Unit test
@pytest.mark.integration       # Integration test
@pytest.mark.slow              # Slow test
@pytest.mark.security          # Security test
@pytest.mark.asyncio           # Async test
```

## Common Patterns

### Async Test with Fixture
```python
@pytest.fixture
async def service(self) -> MyService:
    """Create service instance."""
    svc = MyService()
    await svc.initialize()
    yield svc
    await svc.cleanup()

@pytest.mark.asyncio
async def test_something(self, service: MyService) -> None:
    """Test with service fixture."""
    result = await service.do_something()
    assert result is not None
```

### Mock External Dependencies
```python
from unittest.mock import patch, MagicMock

@patch("akosha.module.external_function")
async def test_with_mock(self, mock_func: MagicMock) -> None:
    """Test with mocked dependency."""
    mock_func.return_value = "mocked_value"
    result = await my_function()
    assert result == "mocked_value"
```

### Parameterized Tests
```python
@pytest.mark.parametrize("input,expected", [
    ("test1", "result1"),
    ("test2", "result2"),
    ("test3", "result3"),
])
async def test_multiple_cases(self, input: str, expected: str) -> None:
    """Test multiple cases."""
    result = process(input)
    assert result == expected
```

## Debugging Tests

### Run with Debugger
```bash
pytest --pdb  # Drop into debugger on failure
```

### Stop on First Failure
```bash
pytest -x     # Stop after first failure
pytest --ff   # Run failures first
```

### Show Local Variables
```bash
pytest -l     # Show local variables on failure
```

### Print Debugging
```bash
pytest -s     # Show print() output
pytest --capture=no  # Disable output capture
```

## Continuous Integration

### Pre-commit Hook
```bash
# Run tests before commit
pytest && pytest --cov=akosha --cov-report=term-missing
```

### GitHub Actions (if applicable)
```yaml
- name: Run tests
  run: |
    pytest --cov=akosha --cov-report=xml --cov-report=term-missing

- name: Upload coverage
  uses: codecov/codecov-action@v3
  with:
    file: ./coverage.xml
```

## Coverage Targets

- **Overall**: ≥ 60% ✅
- **Core Modules**: ≥ 70% ✅
- **Critical Paths**: ≥ 80% ✅

## Test Execution Time

- **Unit tests**: < 30 seconds
- **Integration tests**: < 2 minutes
- **Full suite**: < 5 minutes ✅

## Troubleshooting

### Tests Fail with ImportError
```bash
# Install dependencies
uv pip install -e ".[dev]"
```

### Database Tests Fail
```bash
# Ensure DuckDB is available
python -c "import duckdb; print(duckdb.__version__)"
```

### Async Tests Hang
```bash
# Check for proper async/await usage
# Ensure fixtures are async
# Verify cleanup code
```

### Coverage Report Missing
```bash
# Install pytest-cov
pip install pytest-cov

# Regenerate coverage
pytest --cov=akosha --cov-report=html
```

## Best Practices

1. **One assertion per test** (when possible)
2. **Descriptive test names** that explain what is being tested
3. **Arrange-Act-Assert** pattern for clarity
4. **Fixtures** for shared setup/teardown
5. **Mocks** for external dependencies
6. **Markers** for test categorization
7. **Docstrings** for complex tests
8. **Type hints** for test functions

## Resources

- [pytest Documentation](https://docs.pytest.org/)
- [pytest-asyncio Documentation](https://pytest-asyncio.readthedocs.io/)
- [Pytest Coverage Documentation](https://pytest-cov.readthedocs.io/)
- [Akosha README](/Users/les/Projects/akosha/README.md)

---

**Quick Commands**:
```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=akosha --cov-report=html

# Run specific test
pytest tests/unit/test_knowledge_graph.py -v

# Stop on first failure
pytest -x

# Show print output
pytest -s
```
