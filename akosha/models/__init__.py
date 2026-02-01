"""Akosha data models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel


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
    """Cold tier record with ultra-compressed data."""

    system_id: str
    conversation_id: str
    fingerprint: bytes  # MinHash fingerprint (for deduplication)
    ultra_summary: str  # Single sentence summary
    timestamp: datetime
    daily_metrics: dict[str, float]
