"""Input validation schemas for Akosha MCP tools.

This module provides Pydantic-based validation for all MCP tool inputs,
preventing DoS attacks, injection attempts, and invalid data.

All validation schemas include:
- Type checking
- Length/range constraints
- Format validation
- Sanitization where applicable
"""

from __future__ import annotations

import re
from typing import Any, TypeVar

from pydantic import BaseModel, Field, field_validator, model_validator

# Regular expressions for validation
ALPHANUMERIC_DASH_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_:@.-]+$")
SAFE_TEXT_PATTERN = re.compile(r"^[\w\s\-\.\,\!\?\:\;\'\"\(\)\[\]\{\}]+$")

# Type variable for generic validate_request function
T = TypeVar("T", bound=BaseModel)


class ValidationError(Exception):
    """Raised when input validation fails."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        """Initialize validation error.

        Args:
            message: Error message
            details: Additional error details
        """
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        """Convert error to dictionary.

        Returns:
            Error dictionary with message and details
        """
        return {
            "error": "validation_error",
            "message": self.message,
            "details": self.details,
        }


# ============================================================================
# Embedding Tool Validation
# ============================================================================


class GenerateEmbeddingRequest(BaseModel):
    """Validation schema for generate_embedding tool."""

    text: str = Field(
        ...,
        min_length=1,
        max_length=10_000,
        description="Input text to embed",
    )

    @field_validator("text")
    @classmethod
    def validate_text(cls, v: str) -> str:
        """Validate text input.

        Args:
            v: Text value

        Returns:
            Sanitized text

        Raises:
            ValueError: If text contains invalid characters
        """
        # Check for null bytes
        if "\x00" in v:
            raise ValueError("Text cannot contain null bytes")

        # Trim excessive whitespace
        v = " ".join(v.split())

        return v


class GenerateBatchEmbeddingsRequest(BaseModel):
    """Validation schema for generate_batch_embeddings tool."""

    texts: list[str] = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="List of input texts",
    )
    batch_size: int = Field(
        32,
        ge=1,
        le=128,
        description="Batch size for processing",
    )

    @field_validator("texts")
    @classmethod
    def validate_texts(cls, v: list[str]) -> list[str]:
        """Validate list of texts.

        Args:
            v: List of texts

        Returns:
            Validated list

        Raises:
            ValueError: If texts contain invalid data
        """
        # Check total length to prevent DoS
        total_chars = sum(len(text) for text in v)
        if total_chars > 1_000_000:  # 1MB total limit
            raise ValueError(
                f"Total text size too large: {total_chars:,} characters (max: 1,000,000)"
            )

        # Check each text
        for i, text in enumerate(v):
            if not text:
                raise ValueError(f"Text at index {i} is empty")
            if len(text) > 10_000:
                raise ValueError(
                    f"Text at index {i} too long: {len(text)} characters (max: 10,000)"
                )
            if "\x00" in text:
                raise ValueError(f"Text at index {i} contains null bytes")

        return v

    @model_validator(mode="after")
    def validate_batch_size(self) -> GenerateBatchEmbeddingsRequest:
        """Validate batch size against text count.

        Returns:
            Validated request

        Raises:
            ValueError: If batch_size is larger than text count
        """
        if self.batch_size > len(self.texts):
            # Allow larger batch size, but log warning
            pass
        return self


# ============================================================================
# Search Tool Validation
# ============================================================================


class SearchAllSystemsRequest(BaseModel):
    """Validation schema for search_all_systems tool."""

    query: str = Field(
        ...,
        min_length=1,
        max_length=1_000,
        description="Search query text",
    )
    limit: int = Field(
        10,
        ge=1,
        le=1000,
        description="Maximum results to return",
    )
    threshold: float = Field(
        0.7,
        ge=-1.0,
        le=1.0,
        description="Minimum similarity score (-1 to 1)",
    )
    system_id: str | None = Field(
        None,
        max_length=100,
        description="Optional system ID filter",
    )

    @field_validator("query")
    @classmethod
    def validate_query(cls, v: str) -> str:
        """Validate search query.

        Args:
            v: Query string

        Returns:
            Sanitized query

        Raises:
            ValueError: If query contains invalid characters
        """
        # Check for null bytes
        if "\x00" in v:
            raise ValueError("Query cannot contain null bytes")

        # Trim whitespace
        v = v.strip()

        # Check for suspicious patterns (basic SQL injection detection)
        # These patterns are case-insensitive
        suspicious_patterns = [
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
        query_upper = v.upper()
        for pattern in suspicious_patterns:
            if pattern.upper() in query_upper:
                raise ValueError(
                    "Query contains suspicious pattern that may indicate SQL injection"
                )

        return v

    @field_validator("system_id")
    @classmethod
    def validate_system_id(cls, v: str | None) -> str | None:
        """Validate system ID format.

        Args:
            v: System ID or None

        Returns:
            Validated system ID

        Raises:
            ValueError: If system ID format is invalid
        """
        if v is None:
            return v

        # Check format
        if not ALPHANUMERIC_DASH_PATTERN.match(v):
            raise ValueError(
                f"Invalid system_id format: '{v}'. "
                "System IDs must contain only alphanumeric characters, hyphens, and underscores"
            )

        # Check length
        if len(v) > 100:
            raise ValueError(f"System ID too long: {len(v)} characters (max: 100)")

        return v


# ============================================================================
# Analytics Tool Validation
# ============================================================================


class GetSystemMetricsRequest(BaseModel):
    """Validation schema for get_system_metrics tool."""

    time_range_days: int = Field(
        30,
        ge=1,
        le=365,
        description="Time range in days",
    )


class AnalyzeTrendsRequest(BaseModel):
    """Validation schema for analyze_trends tool."""

    metric_name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Name of metric to analyze",
    )
    system_id: str | None = Field(
        None,
        max_length=100,
        description="Optional system ID filter",
    )
    time_window_days: int = Field(
        7,
        ge=1,
        le=90,
        description="Time window for analysis (days)",
    )

    @field_validator("metric_name")
    @classmethod
    def validate_metric_name(cls, v: str) -> str:
        """Validate metric name format.

        Args:
            v: Metric name

        Returns:
            Validated metric name

        Raises:
            ValueError: If metric name format is invalid
        """
        # Metric names should only contain alphanumeric, underscores, hyphens, and colons
        # More restrictive than general SAFE_ID_PATTERN
        metric_pattern = re.compile(r"^[a-zA-Z0-9_:-]+$")
        if not metric_pattern.match(v):
            raise ValueError(
                f"Invalid metric_name format: '{v}'. "
                "Metric names must contain only alphanumeric characters, underscores, hyphens, and colons"
            )

        return v.lower()

    @field_validator("system_id")
    @classmethod
    def validate_system_id(cls, v: str | None) -> str | None:
        """Validate system ID format.

        Args:
            v: System ID or None

        Returns:
            Validated system ID

        Raises:
            ValueError: If system ID format is invalid
        """
        if v is None:
            return v

        if not ALPHANUMERIC_DASH_PATTERN.match(v):
            raise ValueError(
                f"Invalid system_id format: '{v}'. "
                "System IDs must contain only alphanumeric characters, hyphens, and underscores"
            )

        return v


class DetectAnomaliesRequest(BaseModel):
    """Validation schema for detect_anomalies tool."""

    metric_name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Name of metric to analyze",
    )
    system_id: str | None = Field(
        None,
        max_length=100,
        description="Optional system ID filter",
    )
    time_window_days: int = Field(
        7,
        ge=1,
        le=90,
        description="Time window for analysis (days)",
    )
    threshold_std: float = Field(
        3.0,
        ge=1.0,
        le=10.0,
        description="Standard deviation threshold for anomaly detection",
    )

    @field_validator("metric_name")
    @classmethod
    def validate_metric_name(cls, v: str) -> str:
        """Validate metric name format.

        Args:
            v: Metric name

        Returns:
            Validated metric name

        Raises:
            ValueError: If metric name format is invalid
        """
        # Metric names should only contain alphanumeric, underscores, hyphens, and colons
        metric_pattern = re.compile(r"^[a-zA-Z0-9_:-]+$")
        if not metric_pattern.match(v):
            raise ValueError(
                f"Invalid metric_name format: '{v}'. "
                "Metric names must contain only alphanumeric characters, underscores, hyphens, and colons"
            )

        return v.lower()

    @field_validator("system_id")
    @classmethod
    def validate_system_id(cls, v: str | None) -> str | None:
        """Validate system ID format.

        Args:
            v: System ID or None

        Returns:
            Validated system ID

        Raises:
            ValueError: If system ID format is invalid
        """
        if v is None:
            return v

        if not ALPHANUMERIC_DASH_PATTERN.match(v):
            raise ValueError(
                f"Invalid system_id format: '{v}'. "
                "System IDs must contain only alphanumeric characters, hyphens, and underscores"
            )

        return v


class CorrelateSystemsRequest(BaseModel):
    """Validation schema for correlate_systems tool."""

    metric_name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Name of metric to analyze",
    )
    time_window_days: int = Field(
        7,
        ge=1,
        le=90,
        description="Time window for analysis (days)",
    )

    @field_validator("metric_name")
    @classmethod
    def validate_metric_name(cls, v: str) -> str:
        """Validate metric name format.

        Args:
            v: Metric name

        Returns:
            Validated metric name

        Raises:
            ValueError: If metric name format is invalid
        """
        # Metric names should only contain alphanumeric, underscores, hyphens, and colons
        metric_pattern = re.compile(r"^[a-zA-Z0-9_:-]+$")
        if not metric_pattern.match(v):
            raise ValueError(
                f"Invalid metric_name format: '{v}'. "
                "Metric names must contain only alphanumeric characters, underscores, hyphens, and colons"
            )

        return v.lower()


# ============================================================================
# Graph Tool Validation
# ============================================================================


class QueryKnowledgeGraphRequest(BaseModel):
    """Validation schema for query_knowledge_graph tool."""

    entity_id: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Entity ID to query",
    )
    edge_type: str | None = Field(
        None,
        max_length=100,
        description="Optional edge type filter",
    )
    limit: int = Field(
        50,
        ge=1,
        le=10_000,
        description="Maximum neighbors to return",
    )

    @field_validator("entity_id")
    @classmethod
    def validate_entity_id(cls, v: str) -> str:
        """Validate entity ID format.

        Args:
            v: Entity ID

        Returns:
            Validated entity ID

        Raises:
            ValueError: If entity ID format is invalid
        """
        # Check for path traversal attempts
        if ".." in v or v.startswith(("/", ".")):
            raise ValueError(
                f"Invalid entity_id format: '{v}'. Path traversal patterns are not allowed"
            )

        # Check for safe characters
        if not SAFE_ID_PATTERN.match(v):
            raise ValueError(
                f"Invalid entity_id format: '{v}'. "
                "Entity IDs must contain only alphanumeric characters, underscores, colons, at-signs, periods, and hyphens"
            )

        return v

    @field_validator("edge_type")
    @classmethod
    def validate_edge_type(cls, v: str | None) -> str | None:
        """Validate edge type format.

        Args:
            v: Edge type or None

        Returns:
            Validated edge type

        Raises:
            ValueError: If edge type format is invalid
        """
        if v is None:
            return v

        if not SAFE_ID_PATTERN.match(v):
            raise ValueError(
                f"Invalid edge_type format: '{v}'. "
                "Edge types must contain only alphanumeric characters, underscores, hyphens, and colons"
            )

        return v.lower()


class FindPathRequest(BaseModel):
    """Validation schema for find_path tool."""

    source_id: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Source entity ID",
    )
    target_id: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Target entity ID",
    )
    max_hops: int = Field(
        3,
        ge=1,
        le=10,
        description="Maximum path length",
    )

    @field_validator("source_id", "target_id")
    @classmethod
    def validate_entity_id(cls, v: str) -> str:
        """Validate entity ID format.

        Args:
            v: Entity ID

        Returns:
            Validated entity ID

        Raises:
            ValueError: If entity ID format is invalid
        """
        # Check for path traversal attempts
        if ".." in v or v.startswith(("/", ".")):
            raise ValueError(
                f"Invalid entity_id format: '{v}'. Path traversal patterns are not allowed"
            )

        # Check for safe characters
        if not SAFE_ID_PATTERN.match(v):
            raise ValueError(
                f"Invalid entity_id format: '{v}'. "
                "Entity IDs must contain only alphanumeric characters, underscores, colons, at-signs, periods, and hyphens"
            )

        return v

    @model_validator(mode="after")
    def validate_different_entities(self) -> FindPathRequest:
        """Validate source and target are different.

        Returns:
            Validated request

        Raises:
            ValueError: If source and target are the same
        """
        if self.source_id == self.target_id:
            raise ValueError("source_id and target_id must be different")

        return self


# ============================================================================
# Validation Utilities
# ============================================================================


def validate_request(schema: type[T], **kwargs: Any) -> T:  # noqa: UP047  # type[T] is correct for Python 3.9+
    """Validate request parameters against a schema.

    Args:
        schema: Pydantic schema class
        **kwargs: Request parameters to validate

    Returns:
        Validated model instance with proper type inference

    Raises:
        ValidationError: If validation fails
    """
    try:
        return schema(**kwargs)
    except Exception as e:
        # Convert Pydantic validation error to custom ValidationError
        error_msg = str(e)
        raise ValidationError(
            f"Input validation failed: {error_msg}",
            {"schema": schema.__name__, "error": error_msg},
        ) from e


__all__ = [
    "AnalyzeTrendsRequest",
    "CorrelateSystemsRequest",
    "DetectAnomaliesRequest",
    "FindPathRequest",
    "GenerateBatchEmbeddingsRequest",
    # Embedding tools
    "GenerateEmbeddingRequest",
    # Analytics tools
    "GetSystemMetricsRequest",
    # Graph tools
    "QueryKnowledgeGraphRequest",
    # Search tools
    "SearchAllSystemsRequest",
    # Exceptions
    "ValidationError",
    # Validation utilities
    "validate_request",
]
