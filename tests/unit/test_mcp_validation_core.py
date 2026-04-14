"""Core tests for MCP validation module.

Tests essential validation functionality that can run without external dependencies.
"""

import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch

from akosha.mcp.validation import (
    validate_request,
    GenerateEmbeddingRequest,
    GenerateBatchEmbeddingsRequest,
    SearchAllSystemsRequest,
    GetSystemMetricsRequest,
    AnalyzeTrendsRequest,
    DetectAnomaliesRequest,
    CorrelateSystemsRequest,
    QueryKnowledgeGraphRequest,
    FindPathRequest,
    ValidationError,
)


class TestBasicValidation:
    """Test basic validation functionality."""

    def test_validate_request_success(self):
        """Test successful request validation."""
        request_data = {"text": "hello world"}

        # Test with a valid schema
        result = validate_request(GenerateEmbeddingRequest, **request_data)

        assert result.text == "hello world"

    def test_validate_request_invalid_schema(self):
        """Test validation with invalid schema."""
        request_data = {"text": "hello world"}

        # Should raise TypeError for None schema (not ValidationError)
        with pytest.raises(TypeError):
            validate_request(None, **request_data)


class TestEmbeddingValidation:
    """Test embedding request validation."""

    def test_generate_embedding_request_valid(self):
        """Test valid generate embedding request."""
        request_data = {
            "text": "Hello world"
        }

        result = validate_request(GenerateEmbeddingRequest, **request_data)

        assert result.text == "Hello world"

    def test_generate_embedding_request_empty(self):
        """Test generate embedding with empty text."""
        request_data = {"text": ""}

        with pytest.raises(ValidationError):
            validate_request(GenerateEmbeddingRequest, **request_data)

    def test_generate_batch_embeddings_request_valid(self):
        """Test valid batch embeddings request."""
        request_data = {
            "texts": ["hello", "world"],
            "batch_size": 16
        }

        result = validate_request(GenerateBatchEmbeddingsRequest, **request_data)

        assert result.texts == ["hello", "world"]
        assert result.batch_size == 16

    def test_generate_batch_embeddings_request_empty_list(self):
        """Test batch embeddings with empty list - should fail."""
        request_data = {"texts": [], "batch_size": 32}

        # Empty list should fail validation (min_length=1)
        with pytest.raises(ValidationError):
            validate_request(GenerateBatchEmbeddingsRequest, **request_data)

    def test_generate_batch_embeddings_request_invalid_batch_size(self):
        """Test batch embeddings with invalid batch size."""
        request_data = {
            "texts": ["hello", "world"],
            "batch_size": 0  # Invalid
        }

        with pytest.raises(ValidationError):
            validate_request(GenerateBatchEmbeddingsRequest, **request_data)


class TestSearchValidation:
    """Test search request validation."""

    def test_search_all_systems_request_valid(self):
        """Test valid search request."""
        request_data = {
            "query": "test search",
            "limit": 10,
            "threshold": 0.7,
            "system_id": "system-1"
        }

        result = validate_request(SearchAllSystemsRequest, **request_data)

        assert result.query == "test search"
        assert result.limit == 10
        assert result.threshold == 0.7
        assert result.system_id == "system-1"

    def test_search_all_systems_request_empty_query(self):
        """Test search with empty query."""
        request_data = {"query": ""}

        with pytest.raises(ValidationError):
            validate_request(SearchAllSystemsRequest, **request_data)

    def test_search_all_systems_request_invalid_limit(self):
        """Test search with invalid limit."""
        request_data = {
            "query": "test",
            "limit": -1  # Invalid
        }

        with pytest.raises(ValidationError):
            validate_request(SearchAllSystemsRequest, **request_data)

    def test_search_all_systems_request_invalid_threshold(self):
        """Test search with invalid threshold."""
        request_data = {
            "query": "test",
            "threshold": 1.5  # Invalid, should be 0-1
        }

        with pytest.raises(ValidationError):
            validate_request(SearchAllSystemsRequest, **request_data)


class TestAnalyticsValidation:
    """Test analytics request validation."""

    def test_get_system_metrics_request_valid(self):
        """Test valid get system metrics request."""
        request_data = {"time_range_days": 30}

        result = validate_request(GetSystemMetricsRequest, **request_data)

        assert result.time_range_days == 30

    def test_get_system_metrics_request_invalid_days(self):
        """Test get system metrics with invalid days."""
        request_data = {"time_range_days": -1}

        with pytest.raises(ValidationError):
            validate_request(GetSystemMetricsRequest, **request_data)

    def test_analyze_trends_request_valid(self):
        """Test valid analyze trends request."""
        request_data = {
            "metric_name": "conversation_count",
            "system_id": "system-1",
            "time_window_days": 7
        }

        result = validate_request(AnalyzeTrendsRequest, **request_data)

        assert result.metric_name == "conversation_count"
        assert result.system_id == "system-1"
        assert result.time_window_days == 7

    def test_analyze_trends_request_empty_metric_name(self):
        """Test analyze trends with empty metric name."""
        request_data = {
            "metric_name": "",
            "time_window_days": 7
        }

        with pytest.raises(ValidationError):
            validate_request(AnalyzeTrendsRequest, **request_data)

    def test_detect_anomalies_request_valid(self):
        """Test valid detect anomalies request."""
        request_data = {
            "metric_name": "error_rate",
            "time_window_days": 7,
            "threshold_std": 3.0
        }

        result = validate_request(DetectAnomaliesRequest, **request_data)

        assert result.metric_name == "error_rate"
        assert result.time_window_days == 7
        assert result.threshold_std == 3.0

    def test_detect_anomalies_request_invalid_threshold(self):
        """Test detect anomalies with invalid threshold."""
        request_data = {
            "metric_name": "error_rate",
            "threshold_std": 0.5  # Too low
        }

        with pytest.raises(ValidationError):
            validate_request(DetectAnomaliesRequest, **request_data)

    def test_correlate_systems_request_valid(self):
        """Test valid correlate systems request."""
        request_data = {
            "metric_name": "quality_score",
            "time_window_days": 7
        }

        result = validate_request(CorrelateSystemsRequest, **request_data)

        assert result.metric_name == "quality_score"
        assert result.time_window_days == 7


class TestGraphValidation:
    """Test knowledge graph request validation."""

    def test_query_knowledge_graph_request_valid(self):
        """Test valid query knowledge graph request."""
        request_data = {
            "entity_id": "user:alice",
            "edge_type": "worked_on",
            "limit": 50
        }

        result = validate_request(QueryKnowledgeGraphRequest, **request_data)

        assert result.entity_id == "user:alice"
        assert result.edge_type == "worked_on"
        assert result.limit == 50

    def test_query_knowledge_graph_request_empty_entity_id(self):
        """Test query knowledge graph with empty entity ID."""
        request_data = {"entity_id": ""}

        with pytest.raises(ValidationError):
            validate_request(QueryKnowledgeGraphRequest, **request_data)

    def test_find_path_request_valid(self):
        """Test valid find path request."""
        request_data = {
            "source_id": "user:alice",
            "target_id": "project:myapp",
            "max_hops": 3
        }

        result = validate_request(FindPathRequest, **request_data)

        assert result.source_id == "user:alice"
        assert result.target_id == "project:myapp"
        assert result.max_hops == 3

    def test_find_path_request_same_entities(self):
        """Test find path with same source and target - should fail."""
        request_data = {
            "source_id": "user:alice",
            "target_id": "user:alice",
            "max_hops": 3
        }

        # Same entities should fail validation
        with pytest.raises(ValidationError):
            validate_request(FindPathRequest, **request_data)

    def test_find_path_request_invalid_max_hops(self):
        """Test find path with invalid max hops - should fail."""
        request_data = {
            "source_id": "user:alice",
            "target_id": "project:myapp",
            "max_hops": 1  # Too small
        }

        # Should raise ValidationError for invalid max_hops
        with pytest.raises(ValidationError):
            validate_request(FindPathRequest, **request_data)


class TestErrorHandling:
    """Test error handling in validation."""

    def test_validation_error_structure(self):
        """Test validation error structure."""
        request_data = {"text": ""}

        try:
            validate_request(GenerateEmbeddingRequest, **request_data)
        except ValidationError as e:
            assert hasattr(e, 'message')
            assert hasattr(e, 'details')
            assert isinstance(e.details, dict)
            assert "schema" in e.details
            assert "error" in e.details

    def test_error_message_format(self):
        """Test error message format."""
        request_data = {"text": ""}

        try:
            validate_request(GenerateEmbeddingRequest, **request_data)
        except ValidationError as e:
            assert isinstance(e.message, str)
            assert len(e.message) > 0
            assert "validation failed" in e.message.lower()


class TestPerformance:
    """Test validation performance."""

    def test_validation_performance(self):
        """Test validation performance with multiple requests."""
        request_data = {"text": "hello world", "limit": 10}

        start_time = time.time()
        for _ in range(1000):
            validate_request(GenerateEmbeddingRequest, **request_data)
        end_time = time.time()

        # Should be fast (< 1 second for 1000 validations)
        assert (end_time - start_time) < 1.0

    def test_large_text_validation_performance(self):
        """Test validation performance with large text."""
        large_text = "a" * 10000  # Large text
        request_data = {"text": large_text}

        start_time = time.time()
        result = validate_request(GenerateEmbeddingRequest, **request_data)
        end_time = time.time()

        assert result.text == large_text
        # Should handle large text efficiently
        assert (end_time - start_time) < 0.1