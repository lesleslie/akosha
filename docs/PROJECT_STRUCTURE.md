# Akasha Project Structure

```
akasha/
├── akasha/                    # Source code
│   ├── __init__.py
│   ├── config.py              # Configuration management
│   ├── main.py                # Application entry point
│   ├── storage/               # Storage layer
│   │   ├── __init__.py
│   │   ├── hot_store.py       # DuckDB in-memory (0-7 days)
│   │   ├── warm_store.py      # DuckDB on-disk (7-90 days)
│   │   ├── cold_store.py      # Oneiric Parquet (90+ days)
│   │   ├── sharding.py        # Consistent hashing router
│   │   └── aging.py           # Tier migration service
│   ├── ingestion/             # Ingestion pipeline
│   │   ├── __init__.py
│   │   ├── worker.py          # Pull-based ingestion worker
│   │   ├── discovery.py       # Upload discovery
│   │   └── orchestrator.py    # Multi-worker coordinator
│   ├── processing/            # Processing services
│   │   ├── __init__.py
│   │   ├── deduplication.py   # Exact + fuzzy deduplication
│   │   ├── enrichment.py      # Metadata enrichment
│   │   ├── vector_indexer.py  # HNSW index management
│   │   ├── time_series.py     # Aggregation + trends
│   │   └── knowledge_graph.py # Graph construction
│   ├── query/                 # Query layer
│   │   ├── __init__.py
│   │   ├── distributed.py     # Fan-out query engine
│   │   ├── aggregator.py      # Result merging
│   │   └── faceted.py         # Faceted search
│   ├── cache/                 # Caching layer
│   │   ├── __init__.py
│   │   └── layered_cache.py   # L1 (memory) + L2 (Redis)
│   ├── api/                   # REST API
│   │   ├── __init__.py
│   │   ├── routes.py          # FastAPI routes
│   │   └── middleware.py      # Auth, logging
│   ├── monitoring/            # Observability
│   │   ├── __init__.py
│   │   ├── metrics.py         # Prometheus metrics
│   │   ├── logging.py         # Structured logging
│   │   └── tracing.py         # OpenTelemetry tracing
│   └── utils/                 # Utilities
│       ├── __init__.py
│       ├── retry.py           # Retry logic
│       └── helpers.py         # Helper functions
├── tests/                     # Tests
│   ├── unit/                  # Unit tests
│   │   ├── test_storage.py
│   │   ├── test_ingestion.py
│   │   └── test_query.py
│   ├── integration/           # Integration tests
│   │   ├── test_ingestion_pipeline.py
│   │   └── test_api.py
│   ├── performance/           # Performance tests
│   │   ├── test_search_latency.py
│   │   └── test_ingestion_throughput.py
│   ├── fixtures/              # Test fixtures
│   │   ├── factories.py       # Data factories
│   │   └── conftest.py        # Pytest configuration
│   └── conftest.py            # Global test configuration
├── config/                    # Configuration files
│   ├── akasha.yaml            # Main configuration
│   ├── akasha_storage.yaml    # Storage configuration
│   └── akasha_secrets.yaml    # Secrets (not in git)
├── scripts/                   # Utility scripts
│   ├── deploy.sh              # Deployment script
│   ├── migrate_hot_to_warm.py # Manual migration trigger
│   └── benchmark.py           # Performance benchmarks
├── k8s/                       # Kubernetes manifests
│   ├── deployment.yaml        # Akasha deployment
│   ├── hpa.yaml               # Horizontal pod autoscaler
│   ├── service.yaml           # Kubernetes service
│   ├── configmap.yaml         # Configuration management
│   ├── ingress.yaml           # Ingress configuration
│   └── grafana/
│       └── dashboards/
│           └── akasha-dashboard.json
├── docs/                      # Documentation
│   ├── ADR_001_ARCHITECTURE_DECISIONS.md  # Architecture decisions
│   ├── IMPLEMENTATION_GUIDE.md           # Implementation guide
│   └── API_REFERENCE.md                   # API documentation
├── pyproject.toml             # Python dependencies
├── README.md                  # Project overview
├── CLAUDE.md                  # AI assistant instructions
└── .envrc                     # Direnv configuration
```
