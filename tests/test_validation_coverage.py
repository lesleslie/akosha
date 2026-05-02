"""Tests for akosha/mcp/validation.py — input validation schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from akosha.mcp.validation import (
    ALPHANUMERIC_DASH_PATTERN,
    SAFE_ID_PATTERN,
    SAFE_TEXT_PATTERN,
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


class TestValidationError:
    def test_message_and_details(self):
        err = ValidationError("bad input", {"field": "text"})
        assert err.message == "bad input"
        assert err.details == {"field": "text"}
        assert str(err) == "bad input"

    def test_default_details(self):
        err = ValidationError("bad")
        assert err.details == {}

    def test_to_dict(self):
        err = ValidationError("msg", {"key": "val"})
        d = err.to_dict()
        assert d["error"] == "validation_error"
        assert d["message"] == "msg"
        assert d["details"] == {"key": "val"}

    def test_is_exception(self):
        with pytest.raises(ValidationError):
            raise ValidationError("test")


class TestGenerateEmbeddingRequest:
    def test_valid(self):
        req = GenerateEmbeddingRequest(text="hello world")
        assert req.text == "hello world"

    def test_empty_text_rejected(self):
        with pytest.raises(PydanticValidationError):
            GenerateEmbeddingRequest(text="")

    def test_null_bytes_rejected(self):
        with pytest.raises(PydanticValidationError, match="null bytes"):
            GenerateEmbeddingRequest(text="hello\x00world")

    def test_whitespace_trimmed(self):
        req = GenerateEmbeddingRequest(text="  hello   world  ")
        assert req.text == "hello world"

    def test_max_length(self):
        with pytest.raises(PydanticValidationError):
            GenerateEmbeddingRequest(text="x" * 10_001)


class TestGenerateBatchEmbeddingsRequest:
    def test_valid(self):
        req = GenerateBatchEmbeddingsRequest(texts=["hello", "world"])
        assert req.texts == ["hello", "world"]
        assert req.batch_size == 32

    def test_custom_batch_size(self):
        req = GenerateBatchEmbeddingsRequest(texts=["a"], batch_size=10)
        assert req.batch_size == 10

    def test_empty_texts_rejected(self):
        with pytest.raises(PydanticValidationError):
            GenerateBatchEmbeddingsRequest(texts=[])

    def test_empty_text_in_list(self):
        with pytest.raises(PydanticValidationError, match="index 1 is empty"):
            GenerateBatchEmbeddingsRequest(texts=["ok", "", "ok"])

    def test_text_too_long(self):
        with pytest.raises(PydanticValidationError, match="too long"):
            GenerateBatchEmbeddingsRequest(texts=["x" * 10_001])

    def test_null_bytes_in_list(self):
        with pytest.raises(PydanticValidationError, match="null bytes"):
            GenerateBatchEmbeddingsRequest(texts=["ok", "bad\x00val"])

    def test_total_size_limit(self):
        texts = ["x" * 200_000 for _ in range(6)]
        with pytest.raises(PydanticValidationError, match="too large"):
            GenerateBatchEmbeddingsRequest(texts=texts)

    def test_batch_size_larger_than_texts(self):
        req = GenerateBatchEmbeddingsRequest(texts=["a"], batch_size=100)
        assert req.batch_size == 100

    def test_batch_size_bounds(self):
        with pytest.raises(PydanticValidationError):
            GenerateBatchEmbeddingsRequest(texts=["a"], batch_size=0)
        with pytest.raises(PydanticValidationError):
            GenerateBatchEmbeddingsRequest(texts=["a"], batch_size=200)


class TestSearchAllSystemsRequest:
    def test_valid_defaults(self):
        req = SearchAllSystemsRequest(query="test")
        assert req.query == "test"
        assert req.limit == 10
        assert req.threshold == 0.7
        assert req.system_id is None

    def test_valid_custom(self):
        req = SearchAllSystemsRequest(query="test", limit=100, threshold=0.5, system_id="sys-1")
        assert req.system_id == "sys-1"

    def test_null_bytes_in_query(self):
        with pytest.raises(PydanticValidationError, match="null bytes"):
            SearchAllSystemsRequest(query="test\x00")

    def test_query_whitespace_trimmed(self):
        req = SearchAllSystemsRequest(query="  hello  ")
        assert req.query == "hello"

    def test_sql_injection_patterns(self):
        patterns = [
            "'; --",
            "' OR '1'='1",
            "DROP TABLE",
            "UNION SELECT",
            "1=1",
            "--",
            "/*",
            "*/",
            "xp_cmdshell",
            "exec(",
        ]
        for p in patterns:
            with pytest.raises(PydanticValidationError, match="SQL injection"):
                SearchAllSystemsRequest(query=p)

    def test_sql_injection_case_insensitive(self):
        with pytest.raises(PydanticValidationError, match="SQL injection"):
            SearchAllSystemsRequest(query="drop table users")

    def test_valid_system_id(self):
        req = SearchAllSystemsRequest(query="q", system_id="my-system_01")
        assert req.system_id == "my-system_01"

    def test_invalid_system_id_chars(self):
        with pytest.raises(PydanticValidationError, match="Invalid system_id"):
            SearchAllSystemsRequest(query="q", system_id="bad id!")

    def test_limit_bounds(self):
        with pytest.raises(PydanticValidationError):
            SearchAllSystemsRequest(query="q", limit=0)
        with pytest.raises(PydanticValidationError):
            SearchAllSystemsRequest(query="q", limit=1001)

    def test_threshold_negative_allowed(self):
        req = SearchAllSystemsRequest(query="q", threshold=-0.5)
        assert req.threshold == -0.5


class TestGetSystemMetricsRequest:
    def test_valid_defaults(self):
        req = GetSystemMetricsRequest()
        assert req.time_range_days == 30

    def test_custom_range(self):
        req = GetSystemMetricsRequest(time_range_days=90)
        assert req.time_range_days == 90

    def test_out_of_bounds(self):
        with pytest.raises(PydanticValidationError):
            GetSystemMetricsRequest(time_range_days=0)
        with pytest.raises(PydanticValidationError):
            GetSystemMetricsRequest(time_range_days=400)


class TestAnalyzeTrendsRequest:
    def test_valid_defaults(self):
        req = AnalyzeTrendsRequest(metric_name="cpu_usage")
        assert req.metric_name == "cpu_usage"
        assert req.time_window_days == 7
        assert req.system_id is None

    def test_metric_name_lowered(self):
        req = AnalyzeTrendsRequest(metric_name="CPU_Usage")
        assert req.metric_name == "cpu_usage"

    def test_valid_system_id(self):
        req = AnalyzeTrendsRequest(metric_name="m", system_id="sys-1")
        assert req.system_id == "sys-1"

    def test_none_system_id(self):
        req = AnalyzeTrendsRequest(metric_name="m", system_id=None)
        assert req.system_id is None

    def test_invalid_metric_name(self):
        with pytest.raises(PydanticValidationError, match="Invalid metric_name"):
            AnalyzeTrendsRequest(metric_name="bad name!")

    def test_invalid_system_id(self):
        with pytest.raises(PydanticValidationError, match="Invalid system_id"):
            AnalyzeTrendsRequest(metric_name="m", system_id="bad!")

    def test_time_window_bounds(self):
        with pytest.raises(PydanticValidationError):
            AnalyzeTrendsRequest(metric_name="m", time_window_days=0)
        with pytest.raises(PydanticValidationError):
            AnalyzeTrendsRequest(metric_name="m", time_window_days=100)


class TestDetectAnomaliesRequest:
    def test_valid_defaults(self):
        req = DetectAnomaliesRequest(metric_name="error_rate")
        assert req.metric_name == "error_rate"
        assert req.threshold_std == 3.0
        assert req.system_id is None

    def test_custom_threshold(self):
        req = DetectAnomaliesRequest(metric_name="m", threshold_std=5.0)
        assert req.threshold_std == 5.0

    def test_metric_name_lowered(self):
        req = DetectAnomaliesRequest(metric_name="Error_Rate")
        assert req.metric_name == "error_rate"

    def test_invalid_metric_name(self):
        with pytest.raises(PydanticValidationError, match="Invalid metric_name"):
            DetectAnomaliesRequest(metric_name="bad!")

    def test_invalid_system_id(self):
        with pytest.raises(PydanticValidationError, match="Invalid system_id"):
            DetectAnomaliesRequest(metric_name="m", system_id="bad!")

    def test_threshold_bounds(self):
        with pytest.raises(PydanticValidationError):
            DetectAnomaliesRequest(metric_name="m", threshold_std=0.5)
        with pytest.raises(PydanticValidationError):
            DetectAnomaliesRequest(metric_name="m", threshold_std=20.0)

    def test_valid_system_id(self):
        req = DetectAnomaliesRequest(metric_name="m", system_id="sys-1")
        assert req.system_id == "sys-1"


class TestCorrelateSystemsRequest:
    def test_valid_defaults(self):
        req = CorrelateSystemsRequest(metric_name="latency")
        assert req.metric_name == "latency"
        assert req.time_window_days == 7

    def test_metric_name_lowered(self):
        req = CorrelateSystemsRequest(metric_name="Latency")
        assert req.metric_name == "latency"

    def test_invalid_metric_name(self):
        with pytest.raises(PydanticValidationError, match="Invalid metric_name"):
            CorrelateSystemsRequest(metric_name="bad!")


class TestQueryKnowledgeGraphRequest:
    def test_valid_defaults(self):
        req = QueryKnowledgeGraphRequest(entity_id="user:alice")
        assert req.entity_id == "user:alice"
        assert req.edge_type is None
        assert req.limit == 50

    def test_valid_with_edge_type(self):
        req = QueryKnowledgeGraphRequest(entity_id="user:alice", edge_type="friend_of")
        assert req.edge_type == "friend_of"

    def test_edge_type_lowered(self):
        req = QueryKnowledgeGraphRequest(entity_id="u:a", edge_type="Friend_Of")
        assert req.edge_type == "friend_of"

    def test_path_traversal_rejected(self):
        with pytest.raises(PydanticValidationError, match="Path traversal"):
            QueryKnowledgeGraphRequest(entity_id="../etc/passwd")

    def test_leading_slash_rejected(self):
        with pytest.raises(PydanticValidationError, match="Path traversal"):
            QueryKnowledgeGraphRequest(entity_id="/etc/passwd")

    def test_leading_dot_rejected(self):
        with pytest.raises(PydanticValidationError, match="Path traversal"):
            QueryKnowledgeGraphRequest(entity_id="./local")

    def test_invalid_entity_id_chars(self):
        with pytest.raises(PydanticValidationError, match="Invalid entity_id"):
            QueryKnowledgeGraphRequest(entity_id="bad entity!")

    def test_valid_edge_type_none(self):
        req = QueryKnowledgeGraphRequest(entity_id="u:a", edge_type=None)
        assert req.edge_type is None

    def test_invalid_edge_type(self):
        with pytest.raises(PydanticValidationError, match="Invalid edge_type"):
            QueryKnowledgeGraphRequest(entity_id="u:a", edge_type="bad type!")

    def test_limit_bounds(self):
        with pytest.raises(PydanticValidationError):
            QueryKnowledgeGraphRequest(entity_id="u:a", limit=0)
        with pytest.raises(PydanticValidationError):
            QueryKnowledgeGraphRequest(entity_id="u:a", limit=20_000)


class TestFindPathRequest:
    def test_valid_defaults(self):
        req = FindPathRequest(source_id="user:alice", target_id="user:bob")
        assert req.max_hops == 3

    def test_same_source_target_rejected(self):
        with pytest.raises(PydanticValidationError, match="must be different"):
            FindPathRequest(source_id="user:alice", target_id="user:alice")

    def test_path_traversal_source(self):
        with pytest.raises(PydanticValidationError, match="Path traversal"):
            FindPathRequest(source_id="../bad", target_id="user:bob")

    def test_path_traversal_target(self):
        with pytest.raises(PydanticValidationError, match="Path traversal"):
            FindPathRequest(source_id="user:alice", target_id="/bad")

    def test_invalid_chars_source(self):
        with pytest.raises(PydanticValidationError, match="Invalid entity_id"):
            FindPathRequest(source_id="bad!", target_id="user:bob")

    def test_invalid_chars_target(self):
        with pytest.raises(PydanticValidationError, match="Invalid entity_id"):
            FindPathRequest(source_id="user:a", target_id="bad!")

    def test_max_hops_bounds(self):
        with pytest.raises(PydanticValidationError):
            FindPathRequest(source_id="a", target_id="b", max_hops=0)
        with pytest.raises(PydanticValidationError):
            FindPathRequest(source_id="a", target_id="b", max_hops=20)


class TestValidateRequest:
    def test_valid_request(self):
        result = validate_request(SearchAllSystemsRequest, query="test")
        assert result.query == "test"

    def test_none_schema_raises(self):
        with pytest.raises(TypeError, match="Schema cannot be None"):
            validate_request(None, query="test")

    def test_invalid_params_raises_validation_error(self):
        with pytest.raises(ValidationError) as exc_info:
            validate_request(SearchAllSystemsRequest, query="")
        assert "validation failed" in exc_info.value.message
        assert "SearchAllSystemsRequest" in exc_info.value.details["schema"]

    def test_invalid_params_preserves_original_error(self):
        with pytest.raises(ValidationError) as exc_info:
            validate_request(SearchAllSystemsRequest, query="bad\x00")
        assert "null bytes" in exc_info.value.details["error"]


class TestPatterns:
    def test_alphanumeric_dash(self):
        assert ALPHANUMERIC_DASH_PATTERN.match("hello-world_01")
        assert not ALPHANUMERIC_DASH_PATTERN.match("bad!")
        assert not ALPHANUMERIC_DASH_PATTERN.match("has space")

    def test_safe_id(self):
        assert SAFE_ID_PATTERN.match("user:alice@host.com")
        assert SAFE_ID_PATTERN.match("sys-01")
        assert not SAFE_ID_PATTERN.match("bad!")

    def test_safe_text(self):
        assert SAFE_TEXT_PATTERN.match("Hello, world!")
        assert not SAFE_TEXT_PATTERN.match("bad\ttab\x00")
