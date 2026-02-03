"""Tests for MCP tool input validation."""

from __future__ import annotations

import pytest

from akosha.mcp.validation import (
    AnalyzeTrendsRequest,
    CorrelateSystemsRequest,
    DetectAnomaliesRequest,
    FindPathRequest,
    GenerateBatchEmbeddingsRequest,
    GenerateEmbeddingRequest,
    GetSystemMetricsRequest,
    QueryKnowledgeGraphRequest,
    SearchAllSystemsRequest,
    ValidationError,
    validate_request,
)


class TestEmbeddingValidation:
    """Test embedding tool validation."""

    def test_generate_embedding_valid_request(self) -> None:
        """Test valid embedding generation request."""
        params = validate_request(
            GenerateEmbeddingRequest,
            text="how to implement JWT authentication",
        )

        assert params.text == "how to implement JWT authentication"

    def test_generate_embedding_empty_text(self) -> None:
        """Test that empty text is rejected."""
        with pytest.raises(ValidationError, match="Input validation failed"):
            validate_request(GenerateEmbeddingRequest, text="")

    def test_generate_embedding_text_too_long(self) -> None:
        """Test that text exceeding max length is rejected."""
        long_text = "a" * 10_001

        with pytest.raises(ValidationError, match="Input validation failed"):
            validate_request(GenerateEmbeddingRequest, text=long_text)

    def test_generate_embedding_null_bytes(self) -> None:
        """Test that null bytes are rejected."""
        with pytest.raises(ValidationError, match="Input validation failed"):
            validate_request(GenerateEmbeddingRequest, text="hello\x00world")

    def test_generate_batch_embeddings_valid(self) -> None:
        """Test valid batch embeddings request."""
        params = validate_request(
            GenerateBatchEmbeddingsRequest,
            texts=["text1", "text2", "text3"],
            batch_size=16,
        )

        assert len(params.texts) == 3
        assert params.batch_size == 16

    def test_generate_batch_embeddings_empty_list(self) -> None:
        """Test that empty text list is rejected."""
        with pytest.raises(ValidationError, match="Input validation failed"):
            validate_request(GenerateBatchEmbeddingsRequest, texts=[])

    def test_generate_batch_embeddings_too_many_texts(self) -> None:
        """Test that too many texts are rejected."""
        texts = ["text"] * 1_001

        with pytest.raises(ValidationError, match="Input validation failed"):
            validate_request(GenerateBatchEmbeddingsRequest, texts=texts)

    def test_generate_batch_embeddings_total_size_limit(self) -> None:
        """Test that total character limit is enforced."""
        # Create 1000 texts of 1001 characters each = 1,001,000 total chars (exceeds 1MB limit)
        texts = ["x" * 1_001] * 1_000

        with pytest.raises(ValidationError, match="Total text size too large"):
            validate_request(GenerateBatchEmbeddingsRequest, texts=texts)

    def test_generate_batch_embeddings_invalid_batch_size(self) -> None:
        """Test that invalid batch sizes are rejected."""
        with pytest.raises(ValidationError, match="Input validation failed"):
            validate_request(
                GenerateBatchEmbeddingsRequest,
                texts=["text1"],
                batch_size=0,  # Too small
            )

        with pytest.raises(ValidationError, match="Input validation failed"):
            validate_request(
                GenerateBatchEmbeddingsRequest,
                texts=["text1"],
                batch_size=129,  # Too large
            )


class TestSearchValidation:
    """Test search tool validation."""

    def test_search_all_systems_valid(self) -> None:
        """Test valid search request."""
        params = validate_request(
            SearchAllSystemsRequest,
            query="JWT authentication",
            limit=10,
            threshold=0.7,
            system_id="system-1",
        )

        assert params.query == "JWT authentication"
        assert params.limit == 10
        assert params.threshold == 0.7
        assert params.system_id == "system-1"

    def test_search_query_empty(self) -> None:
        """Test that empty query is rejected."""
        with pytest.raises(ValidationError):
            validate_request(SearchAllSystemsRequest, query="")

    def test_search_query_too_long(self) -> None:
        """Test that query exceeding max length is rejected."""
        long_query = "a" * 1_001

        with pytest.raises(ValidationError):
            validate_request(SearchAllSystemsRequest, query=long_query)

    def test_search_limit_out_of_range(self) -> None:
        """Test that invalid limits are rejected."""
        with pytest.raises(ValidationError):
            validate_request(SearchAllSystemsRequest, query="test", limit=0)

        with pytest.raises(ValidationError):
            validate_request(SearchAllSystemsRequest, query="test", limit=1_001)

    def test_search_threshold_out_of_range(self) -> None:
        """Test that invalid thresholds are rejected."""
        with pytest.raises(ValidationError):
            validate_request(SearchAllSystemsRequest, query="test", threshold=-1.1)

        with pytest.raises(ValidationError):
            validate_request(SearchAllSystemsRequest, query="test", threshold=1.1)

    def test_search_system_id_invalid_format(self) -> None:
        """Test that invalid system_id format is rejected."""
        # Use values that pass Field validation but fail custom validators
        invalid_ids = [
            "system/1",  # Forward slash
            "system;1",  # Semicolon
            "system.1",  # Period (not in allowed pattern)
            "system:1",  # Colon (not in allowed pattern)
        ]

        for invalid_id in invalid_ids:
            with pytest.raises(ValidationError, match="Invalid system_id"):
                validate_request(
                    SearchAllSystemsRequest,
                    query="test",
                    system_id=invalid_id,
                )

    def test_search_sql_injection_detected(self) -> None:
        """Test that SQL injection patterns are detected."""
        malicious_queries = [
            "'; DROP TABLE users; --",
            "' OR '1'='1",
            "test' UNION SELECT * FROM users",
            "admin'--",
            "test; DROP TABLE",
        ]

        for query in malicious_queries:
            with pytest.raises(ValidationError, match="suspicious pattern"):
                validate_request(SearchAllSystemsRequest, query=query)


class TestAnalyticsValidation:
    """Test analytics tool validation."""

    def test_get_system_metrics_valid(self) -> None:
        """Test valid metrics request."""
        params = validate_request(GetSystemMetricsRequest, time_range_days=30)

        assert params.time_range_days == 30

    def test_time_range_days_out_of_bounds(self) -> None:
        """Test that invalid time ranges are rejected."""
        with pytest.raises(ValidationError):
            validate_request(GetSystemMetricsRequest, time_range_days=0)

        with pytest.raises(ValidationError):
            validate_request(GetSystemMetricsRequest, time_range_days=366)

    def test_analyze_trends_valid(self) -> None:
        """Test valid trend analysis request."""
        params = validate_request(
            AnalyzeTrendsRequest,
            metric_name="conversation_count",
            system_id="system-1",
            time_window_days=7,
        )

        assert params.metric_name == "conversation_count"
        assert params.system_id == "system-1"
        assert params.time_window_days == 7

    def test_metric_name_invalid_format(self) -> None:
        """Test that invalid metric names are rejected."""
        # Use values that pass Field validation but fail custom validators
        invalid_names = [
            "metric/1",  # Forward slash
            "metric;1",  # Semicolon
            "metric@1",  # At-sign (not in allowed pattern for metrics)
        ]

        for name in invalid_names:
            with pytest.raises(ValidationError, match="Invalid metric_name"):
                validate_request(AnalyzeTrendsRequest, metric_name=name)

    def test_detect_anomalies_valid(self) -> None:
        """Test valid anomaly detection request."""
        params = validate_request(
            DetectAnomaliesRequest,
            metric_name="error_rate",
            threshold_std=3.0,
        )

        assert params.metric_name == "error_rate"
        assert params.threshold_std == 3.0

    def test_threshold_std_out_of_range(self) -> None:
        """Test that invalid thresholds are rejected."""
        with pytest.raises(ValidationError):
            validate_request(
                DetectAnomaliesRequest,
                metric_name="test",
                threshold_std=0.5,
            )

        with pytest.raises(ValidationError):
            validate_request(
                DetectAnomaliesRequest,
                metric_name="test",
                threshold_std=11.0,
            )


class TestGraphValidation:
    """Test knowledge graph tool validation."""

    def test_query_knowledge_graph_valid(self) -> None:
        """Test valid graph query request."""
        params = validate_request(
            QueryKnowledgeGraphRequest,
            entity_id="user:alice",
            edge_type="worked_on",
            limit=50,
        )

        assert params.entity_id == "user:alice"
        assert params.edge_type == "worked_on"
        assert params.limit == 50

    def test_entity_id_path_traversal(self) -> None:
        """Test that path traversal attempts are rejected."""
        malicious_ids = [
            "../../../etc/passwd",
            "../secrets",
            "/etc/passwd",
            "./hidden",
        ]

        for entity_id in malicious_ids:
            with pytest.raises(ValidationError, match="Path traversal"):
                validate_request(QueryKnowledgeGraphRequest, entity_id=entity_id)

    def test_entity_id_invalid_format(self) -> None:
        """Test that invalid entity IDs are rejected."""
        # Use values that pass Field validation but fail custom validators
        invalid_ids = [
            "user/alice",  # Forward slash
            "user;alice",  # Semicolon
        ]

        for entity_id in invalid_ids:
            with pytest.raises(ValidationError, match="Invalid entity_id"):
                validate_request(QueryKnowledgeGraphRequest, entity_id=entity_id)

    def test_find_path_valid(self) -> None:
        """Test valid path finding request."""
        params = validate_request(
            FindPathRequest,
            source_id="user:alice",
            target_id="project:akosha",
            max_hops=3,
        )

        assert params.source_id == "user:alice"
        assert params.target_id == "project:akosha"
        assert params.max_hops == 3

    def test_find_path_same_entities(self) -> None:
        """Test that same source and target are rejected."""
        with pytest.raises(ValidationError, match="must be different"):
            validate_request(
                FindPathRequest,
                source_id="user:alice",
                target_id="user:alice",
            )

    def test_max_hops_out_of_range(self) -> None:
        """Test that invalid max_hops are rejected."""
        with pytest.raises(ValidationError):
            validate_request(
                FindPathRequest,
                source_id="user:a",
                target_id="user:b",
                max_hops=0,
            )

        with pytest.raises(ValidationError):
            validate_request(
                FindPathRequest,
                source_id="user:a",
                target_id="user:b",
                max_hops=11,
            )


class TestValidationSecurity:
    """Test security-related validation."""

    def test_null_byte_injection_prevented(self) -> None:
        """Test that null byte injection is prevented."""
        with pytest.raises(ValidationError):
            validate_request(GenerateEmbeddingRequest, text="hello\x00world")

    def test_sql_injection_patterns_detected(self) -> None:
        """Test that common SQL injection patterns are detected."""
        malicious_queries = [
            "'; DROP TABLE conversations; --",
            "' OR '1'='1",
            "admin'--",
            "' UNION SELECT * FROM users --",
        ]

        for query in malicious_queries:
            with pytest.raises(ValidationError, match="suspicious pattern"):
                validate_request(SearchAllSystemsRequest, query=query)

    def test_path_traversal_prevented(self) -> None:
        """Test that path traversal attempts are prevented."""
        malicious_ids = [
            "../../../etc/passwd",
            "..\\..\\..\\windows\\system32",
            "/etc/passwd",
            "./.env",
        ]

        for entity_id in malicious_ids:
            with pytest.raises(ValidationError, match="Path traversal"):
                validate_request(QueryKnowledgeGraphRequest, entity_id=entity_id)

    def test_command_injection_prevented(self) -> None:
        """Test that command injection patterns are prevented."""
        malicious_systems = [
            "system-1; rm -rf /",
            "system-1 | cat /etc/passwd",
            "system-1 && malicious",
            "$(whoami)",
            "`id`",
        ]

        for system_id in malicious_systems:
            with pytest.raises(ValidationError, match="Invalid system_id"):
                validate_request(
                    SearchAllSystemsRequest,
                    query="test",
                    system_id=system_id,
                )


class TestValidationPerformance:
    """Test validation performance constraints."""

    def test_batch_size_limits_enforced(self) -> None:
        """Test that batch size limits prevent DoS."""
        # Maximum batch size is 128
        with pytest.raises(ValidationError):
            validate_request(
                GenerateBatchEmbeddingsRequest,
                texts=["text"] * 10,
                batch_size=129,
            )

    def test_text_length_limits_enforced(self) -> None:
        """Test that text length limits prevent DoS."""
        # Single text max is 10,000 chars
        with pytest.raises(ValidationError):
            validate_request(GenerateEmbeddingRequest, text="a" * 10_001)

    def test_total_batch_size_enforced(self) -> None:
        """Test that total batch size limits prevent DoS."""
        # Total size limit is 1MB
        texts = ["x" * 1_001] * 1_000  # 1,001,000 characters
        with pytest.raises(ValidationError, match="Total text size too large"):
            validate_request(GenerateBatchEmbeddingsRequest, texts=texts)

    def test_limit_params_enforced(self) -> None:
        """Test that limit parameters prevent excessive results."""
        # Max limit for search is 1000
        with pytest.raises(ValidationError):
            validate_request(SearchAllSystemsRequest, query="test", limit=1_001)

        # Max limit for graph queries is 10,000
        with pytest.raises(ValidationError):
            validate_request(QueryKnowledgeGraphRequest, entity_id="test", limit=10_001)


class TestValidationErrorHandling:
    """Test ValidationError exception handling."""

    def test_validation_error_to_dict(self) -> None:
        """Test ValidationError conversion to dictionary."""
        error = ValidationError(
            "Test validation failed",
            {"param": "value"},
        )

        error_dict = error.to_dict()

        assert error_dict["error"] == "validation_error"
        assert error_dict["message"] == "Test validation failed"
        assert error_dict["details"] == {"param": "value"}

    def test_validation_error_without_details(self) -> None:
        """Test ValidationError without details."""
        error = ValidationError("Test error")

        error_dict = error.to_dict()

        assert error_dict["details"] == {}
