from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from akosha.mcp.tools.akosha_tools import register_akosha_tools, register_analytics_tools
from akosha.processing.analytics import TimeSeriesAnalytics
from akosha.processing.embeddings import EmbeddingService
from akosha.processing.knowledge_graph import KnowledgeGraphBuilder


class Vector(list):
    def tolist(self) -> list[float]:
        return list(self)


@dataclass
class CapturingRegistry:
    tools: dict[str, object]

    def __init__(self) -> None:
        self.tools = {}

    def register(self, metadata):
        def decorator(func):
            self.tools[metadata.name] = func
            return func

        return decorator


@pytest.fixture(autouse=True)
def _disable_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AKOSHA_API_TOKEN", raising=False)
    monkeypatch.setenv("AKOSHA_AUTH_ENABLED", "false")


@pytest.fixture
def embedding_service() -> MagicMock:
    service = MagicMock(spec=EmbeddingService)
    service.generate_embedding = AsyncMock(side_effect=[Vector([0.1] * 384), Vector([0.2] * 384)])
    service.generate_batch_embeddings = AsyncMock(
        side_effect=[[Vector([0.3] * 384), Vector([0.4] * 384)], []]
    )
    service.is_available = MagicMock(side_effect=[True, False, True, False])
    return service


@pytest.fixture
def analytics_service() -> MagicMock:
    service = MagicMock(spec=TimeSeriesAnalytics)
    service.get_metric_names.return_value = ["conversation_count", "quality_score"]
    service.analyze_trend = AsyncMock(
        side_effect=[
            SimpleNamespace(
                metric_name="conversation_count",
                trend_direction="increasing",
                trend_strength=0.87,
                percent_change=23.5,
                confidence=0.91,
                time_range=(
                    datetime(2026, 1, 1, tzinfo=UTC),
                    datetime(2026, 1, 8, tzinfo=UTC),
                ),
            ),
            None,
        ]
    )
    service.detect_anomalies = AsyncMock(
        side_effect=[
            SimpleNamespace(
                metric_name="error_rate",
                anomaly_count=1,
                total_points=10,
                anomaly_rate=0.1,
                threshold=3.0,
                anomalies=[{"timestamp": "2026-01-01T00:00:00+00:00", "value": 12.5}],
            ),
            None,
        ]
    )
    service.correlate_systems = AsyncMock(
        side_effect=[
            SimpleNamespace(
                metric_name="quality_score",
                systems=["system-a", "system-b"],
                system_pairs=[{"system_1": "system-a", "system_2": "system-b", "correlation": 0.8}],
                time_range=(
                    datetime(2026, 1, 1, tzinfo=UTC),
                    datetime(2026, 1, 8, tzinfo=UTC),
                ),
            ),
            None,
        ]
    )
    return service


@pytest.fixture
def graph_builder() -> MagicMock:
    builder = MagicMock(spec=KnowledgeGraphBuilder)
    builder.get_neighbors.return_value = [
        {
            "entity_id": "project:myapp",
            "entity_type": "project",
            "edge_type": "worked_on",
            "weight": 1.0,
            "properties": {"name": "My App"},
        }
    ]
    builder.find_shortest_path = MagicMock(side_effect=[None, ["user:alice", "project:myapp"]])
    builder.get_statistics.return_value = {
        "total_entities": 42,
        "total_edges": 125,
        "entity_types": {"user": 15, "project": 12, "system": 10, "concept": 5},
        "edge_types": {"worked_on": 45, "contains": 30, "related_to": 50},
    }
    return builder


@pytest.mark.asyncio
async def test_tool_runtime_branches(
    embedding_service: MagicMock,
    analytics_service: MagicMock,
    graph_builder: MagicMock,
) -> None:
    registry = CapturingRegistry()
    register_akosha_tools(registry, embedding_service, analytics_service, graph_builder)

    generate_embedding = registry.tools["generate_embedding"]
    search_all_systems = registry.tools["search_all_systems"]
    generate_batch_embeddings = registry.tools["generate_batch_embeddings"]
    get_system_metrics = registry.tools["get_system_metrics"]
    analyze_trends = registry.tools["analyze_trends"]
    detect_anomalies = registry.tools["detect_anomalies"]
    correlate_systems = registry.tools["correlate_systems"]
    query_knowledge_graph = registry.tools["query_knowledge_graph"]
    find_path = registry.tools["find_path"]
    get_graph_statistics = registry.tools["get_graph_statistics"]

    embedding = await generate_embedding(text="how to secure JWT auth")
    assert embedding["embedding_dim"] == 384
    assert embedding["mode"] == "real"

    search = await search_all_systems(
        query="JWT auth", limit=2, threshold=0.8, system_id="system-x"
    )
    assert search["total_results"] == 1
    assert search["results"][0]["system_id"] == "system-x"
    assert search["mode"] == "fallback"

    batch = await generate_batch_embeddings(texts=["alpha", "beta"], batch_size=2)
    assert batch["count"] == 2
    assert batch["embedding_dim"] == 384
    assert batch["mode"] == "real"

    empty_batch = await generate_batch_embeddings(texts=["gamma"], batch_size=1)
    assert empty_batch["count"] == 0
    assert empty_batch["embedding_dim"] == 0
    assert empty_batch["mode"] == "fallback"

    metrics = await get_system_metrics(time_range_days=7)
    assert metrics == {
        "time_range_days": 7,
        "total_metrics": 2,
        "metric_names": ["conversation_count", "quality_score"],
    }

    trend = await analyze_trends(
        metric_name="conversation_count", system_id="system-1", time_window_days=7
    )
    assert trend["trend_direction"] == "increasing"
    assert trend["system_id"] == "system-1"
    missing_trend = await analyze_trends(metric_name="conversation_count", time_window_days=7)
    assert missing_trend["error"] == "Insufficient data for trend analysis"

    anomalies = await detect_anomalies(metric_name="error_rate", threshold_std=3.0)
    assert anomalies["anomaly_count"] == 1
    assert anomalies["anomalies"][0]["value"] == 12.5
    missing_anomalies = await detect_anomalies(metric_name="error_rate", threshold_std=3.0)
    assert missing_anomalies["error"] == "Insufficient data for anomaly detection"

    correlations = await correlate_systems(metric_name="quality_score", time_window_days=7)
    assert correlations["significant_correlations"] == 1
    missing_correlations = await correlate_systems(metric_name="quality_score", time_window_days=7)
    assert missing_correlations["error"] == "Insufficient data for correlation analysis"

    neighbors = await query_knowledge_graph(entity_id="user:alice", edge_type="worked_on", limit=10)
    assert neighbors["total_neighbors"] == 1
    assert neighbors["neighbors"][0]["entity_id"] == "project:myapp"

    path_missing = await find_path(source_id="user:alice", target_id="project:myapp", max_hops=3)
    assert path_missing["path_found"] is False
    path_found = await find_path(source_id="user:alice", target_id="project:myapp", max_hops=3)
    assert path_found["path_found"] is True
    assert path_found["hops"] == 1

    stats = await get_graph_statistics()
    assert stats["total_entities"] == 42
    assert stats["entity_types"]["user"] == 15


def test_register_analytics_tools_skips_when_service_is_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: analytics_service=None must not register broken closures.

    Mirrors the lite-mode wiring at akosha/mcp/server.py:296-304 where the
    lifespan leaves ``analytics_service = None`` and still hands it to
    ``register_akosha_tools``. Before the fix, the four analytics closures
    captured ``None`` and raised ``AttributeError`` on first invocation
    (``'NoneType' object has no attribute 'detect_anomalies'`` etc.).

    After the fix, ``register_analytics_tools`` returns early, logs a
    warning, and registers nothing.
    """
    registry = CapturingRegistry()

    with caplog.at_level("WARNING", logger="akosha.mcp.tools.akosha_tools"):
        register_analytics_tools(registry, analytics_service=None)

    assert "analytics_service is None" in caplog.text
    assert "detect_anomalies" not in registry.tools
    assert "analyze_trends" not in registry.tools
    assert "correlate_systems" not in registry.tools
    assert "get_system_metrics" not in registry.tools
