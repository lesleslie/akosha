"""Storage models for Akosha.

Defines Pydantic models for conversation records, uploads, and metadata.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ConversationMetadata(BaseModel):
    """Metadata for a conversation."""

    source: str | None = None
    tags: list[str] = Field(default_factory=list)
    system_version: str | None = None
    language: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class HotRecord(BaseModel):
    """Record for hot tier storage (0-7 days).

    Contains full content with FLOAT[384] embeddings for vector search.
    """

    system_id: str
    conversation_id: str
    content: str
    embedding: list[float]  # FLOAT[384] for vector similarity search
    timestamp: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)  # Plain dict for DuckDB JSON


class WarmRecord(BaseModel):
    """Record for warm tier storage (7-90 days).

    Contains compressed INT8[384] embeddings and 3-sentence summary.
    """

    system_id: str
    conversation_id: str
    embedding: list[int]  # INT8[384] quantized (75% size reduction)
    summary: str  # Extractive summary (3 sentences)
    timestamp: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)  # Plain dict for DuckDB JSON


class ColdRecord(BaseModel):
    """Record for cold tier storage (90+ days).

    Contains ultra-compressed fingerprint and single-sentence summary.
    """

    system_id: str
    conversation_id: str
    fingerprint: list[int]  # MinHash fingerprint for fuzzy deduplication
    ultra_summary: str  # Single-sentence summary
    timestamp: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)  # Plain dict for DuckDB JSON


class SystemMemoryUpload(BaseModel):
    """Metadata for a Session-Buddy system memory upload.

    Represents an uploaded database from a Session-Buddy instance.
    """

    system_id: str
    upload_id: str
    conversation_count: int
    timestamp: datetime
    manifest_path: str  # Path to manifest.json in cloud storage
    checksum: str | None = None  # SHA-256 checksum for validation
    size_bytes: int | None = None  # Total upload size


class IngestionStats(BaseModel):
    """Statistics for ingestion operations."""

    uploads_processed: int = 0
    conversations_ingested: int = 0
    duplicates_skipped: int = 0
    errors_count: int = 0
    processing_time_seconds: float = 0.0


class CodeGraphMetadata(BaseModel):
    """Metadata for a code graph."""

    repo_path: str
    commit_hash: str
    branch: str | None = None
    nodes_count: int
    edges_count: int = 0
    languages: list[str] = Field(default_factory=list)
    ingested_at: datetime
    extra: dict[str, Any] = Field(default_factory=dict)
