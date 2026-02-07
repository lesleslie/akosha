# Akosha Admin Shell

The Akosha admin shell provides an interactive IPython environment for distributed intelligence operations and pattern recognition across systems.

## Quick Start

```bash
# Launch the admin shell
akosha shell

# Or with verbose output
akosha shell --verbose
```

## Features

The admin shell provides:

- **Distributed Intelligence Commands**: Aggregate, search, detect anomalies, query knowledge graphs, and analyze trends
- **Session Tracking**: Automatic session tracking via Session-Buddy MCP
- **IPython Integration**: Full IPython features including tab completion, magic commands, and rich output
- **Application Access**: Direct access to the Akosha application instance

## Intelligence Commands

### `aggregate(query, filters, limit)`

Aggregate data across distributed systems.

```python
# Aggregate all data
result = aggregate()

# Aggregate with filters
result = aggregate(
    query="*",
    filters={"source": "session-buddy", "type": "memory"},
    limit=100
)
```

**Parameters:**
- `query` (str): Aggregation query pattern (default: `"*"`)
- `filters` (dict): Optional filter criteria
- `limit` (int): Maximum results to return (default: 100)

**Returns:** Dictionary with aggregation results

### `search(query, index, limit)`

Search distributed memory using vector similarity.

```python
# Search all indices
results = search("user sessions about authentication")

# Search specific index
results = search(
    query="anomaly detection patterns",
    index="memories",
    limit=10
)
```

**Parameters:**
- `query` (str): Search query string
- `index` (str): Index to search (default: `"all"`)
- `limit` (int): Maximum results to return (default: 10)

**Returns:** Dictionary with search results and relevance scores

### `detect(metric, threshold, window)`

Detect anomalies in distributed systems using ML-based detection.

```python
# Detect all anomalies
anomalies = detect()

# Detect specific metric
anomalies = detect(
    metric="memory_usage",
    threshold=0.8,
    window=300  # 5 minutes
)
```

**Parameters:**
- `metric` (str): Metric to analyze (default: `"all"`)
- `threshold` (float): Anomaly detection threshold 0-1 (default: 0.8)
- `window` (int): Time window in seconds (default: 300)

**Returns:** Dictionary with detected anomalies

### `graph(query, node_type, depth)`

Query knowledge graph for relationships and patterns.

```python
# Query knowledge graph
result = graph("find connected components")

# Query with filters
result = graph(
    query="session flow patterns",
    node_type="session",
    depth=2
)
```

**Parameters:**
- `query` (str): Graph query pattern
- `node_type` (str): Optional node type filter
- `depth` (int): Maximum traversal depth (default: 2)

**Returns:** Dictionary with graph nodes and edges

### `trends(metric, window, granularity)`

Analyze trends in distributed systems using time-series analysis.

```python
# Analyze all trends
trends_data = trends()

# Analyze specific metric
trends_data = trends(
    metric="query_latency",
    window=3600,  # 1 hour
    granularity=60  # 1 minute buckets
)
```

**Parameters:**
- `metric` (str): Metric to analyze (default: `"all"`)
- `window` (int): Time window in seconds (default: 3600)
- `granularity` (int): Data granularity in seconds (default: 60)

**Returns:** Dictionary with trend analysis results

## Utility Commands

### `version()`

Show Akosha component version.

```python
>>> version()
'0.1.0'
```

### `adapters()`

List available adapters.

```python
>>> adapters()
['vector_db', 'graph_db', 'analytics', 'alerting']
```

### `app`

Access the Akosha application instance.

```python
>>> app
<akosha.main.AkoshaApplication object at 0x...>
```

## Session Tracking

The admin shell automatically tracks sessions via Session-Buddy MCP:

- **Session Start**: Emitted when shell launches
- **Session End**: Emitted when shell exits
- **Metadata**: Includes component name, type, version, and adapters

Session tracking enables:
- Cross-session history and analytics
- Collaboration features
- Audit logging
- Usage metrics

## IPython Features

The shell includes all standard IPython features:

### Tab Completion

```python
>>> agg<TAB>
aggregate
```

### Magic Commands

```python
# List all magic commands
%help_shell

# Time execution
%timeit result = aggregate()

# View history
%history
```

### Shell Commands

```python
# Run shell commands
!ls -la

# Capture output
files = !ls
```

### Rich Output

```python
# Display tables
from rich.table import Table
table = Table(title="Results")
# ...
table
```

## Configuration

Session tracking configuration via environment variables:

```bash
# Session-Buddy path (default: /Users/les/Projects/session-buddy)
export SESSION_BUDDY_PATH="/path/to/session-buddy"
```

## Architecture

The admin shell extends the Oneiric `AdminShell` base class:

```
AdminShell (oneiric.shell)
    ↓
AkoshaShell (akosha.shell.adapter)
    ├── SessionEventEmitter (session tracking)
    ├── Intelligence commands (aggregate, search, detect, graph, trends)
    └── Custom banner and namespace
```

### Component Metadata

- **Name**: `akosha`
- **Type**: `soothsayer` (reveals hidden patterns)
- **Adapters**: `vector_db`, `graph_db`, `analytics`, `alerting`

## Examples

### Example 1: Aggregate and Search

```python
# Aggregate data from all sources
results = aggregate(query="*", filters={"type": "session"}, limit=50)

# Search for specific patterns
memories = search("authentication failures", index="memories", limit=10)

# Combine results
for memory in memories["results"]:
    print(f"Found: {memory}")
```

### Example 2: Anomaly Detection

```python
# Detect anomalies in memory usage
anomalies = detect(
    metric="memory_usage",
    threshold=0.85,
    window=600  # 10 minutes
)

# Process anomalies
for anomaly in anomalies["anomalies"]:
    print(f"Anomaly detected: {anomaly}")
```

### Example 3: Knowledge Graph Analysis

```python
# Query knowledge graph
graph_result = graph(
    query="session to session-buddy connections",
    node_type="session",
    depth=2
)

# Analyze connections
print(f"Found {len(graph_result['nodes'])} nodes")
print(f"Found {len(graph_result['edges'])} edges")
```

### Example 4: Trend Analysis

```python
# Analyze query latency trends
trends_data = trends(
    metric="query_latency",
    window=3600,  # 1 hour
    granularity=60  # 1 minute
)

# Plot trends (if matplotlib available)
import matplotlib.pyplot as plt
plt.plot(trends_data["trends"])
plt.show()
```

## Troubleshooting

### Session Tracking Disabled

If session tracking is disabled:

```
ℹ️ Session-Buddy MCP unavailable - session tracking disabled
```

**Solution:** Ensure Session-Buddy is installed and MCP server is running:

```bash
# Check Session-Buddy is available
cd /Users/les/Projects/session-buddy
uv run python -m session_buddy
```

### Command Not Found

If intelligence commands are not available:

```python
# Check namespace
dir()

# Verify commands are present
'aggregate' in dir()  # Should be True
```

**Solution:** Restart the shell or reinitialize the namespace.

## Development

### Running Tests

```bash
# Run shell tests
pytest tests/unit/test_shell.py -v

# Run with coverage
pytest tests/unit/test_shell.py --cov=akosha.shell --cov-report=html
```

### Adding New Commands

To add new intelligence commands:

1. Add async method to `AkoshaShell` class:

```python
async def _my_command(self, param1, param2):
    """My custom command."""
    # Implementation
    return {"status": "success", "result": ...}
```

2. Add to namespace in `_add_akasha_namespace()`:

```python
self.namespace.update({
    "my_command": lambda *args, **kwargs: asyncio.run(
        self._my_command(*args, **kwargs)
    ),
})
```

3. Update banner to document the command

4. Add tests to `tests/unit/test_shell.py`

## Further Reading

- [Oneiric Admin Shell Documentation](https://github.com/yourusername/oneiric)
- [Session-Buddy MCP Documentation](https://github.com/yourusername/session-buddy)
- [Akosha Architecture](docs/ARCHITECTURE.md)
- [Akosha CLI Reference](docs/CLI.md)
