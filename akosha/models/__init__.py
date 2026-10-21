"""Akosha data models."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


@dataclass
class SystemMemoryUpload:
    """System memory upload from Session-Buddy."""

    system_id: str
    upload_id: str
    manifest: dict[str, Any]
    storage_prefix: str
    uploaded_at: datetime


class HotRecord(BaseModel):
    """Hot tier record with full embeddings."""

    system_id: str
    conversation_id: str
    content: str
    embedding: list[float]  # FLOAT[384]
    timestamp: datetime
    metadata: dict[str, Any]

    model_config = {"json_encoders": {datetime: lambda v: v.isoformat()}}


class WarmRecord(BaseModel):
    """Warm tier record with compressed embeddings."""

    system_id: str
    conversation_id: str
    embedding: list[int]  # INT8[384] (quantized)
    summary: str  # Extractive summary (3 sentences)
    timestamp: datetime
    metadata: dict[str, Any]


class ColdRecord(BaseModel):
    """Cold tier record with ultra-compressed data.

    This is the single definition site; ``akosha.storage.models`` re-exports it
    rather than defining a second class.

    ``fingerprint`` is a MinHash *list*, not bytes. A bytes payload does satisfy
    ``pa.list_(pa.int32())``, but only by being reinterpreted as a list of
    codepoints -- it round-trips and is no longer the MinHash value. ``metadata``
    is the JSON blob the exporter serializes, named the same as on
    HotRecord/WarmRecord so all three tiers present one shape to storage.
    """

    system_id: str
    conversation_id: str
    fingerprint: list[int]  # MinHash fingerprint for fuzzy deduplication
    ultra_summary: str  # Single-sentence summary
    timestamp: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)  # Plain dict for JSON
