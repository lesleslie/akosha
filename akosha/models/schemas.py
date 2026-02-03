"""Pydantic validation schemas for Akosha models.

This module provides structured validation for external data ingestion,
preventing injection attacks, malformed data, and schema violations.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# ============================================================================
# Ingestion Manifest Schemas
# ============================================================================


class SystemMemoryUploadManifest(BaseModel):
    """Validation schema for Session-Buddy memory upload manifests.

    This schema validates the manifest.json file uploaded by Session-Buddy
    systems when pushing memory data to Akosha.

    Security considerations:
    - Filenames are validated to prevent path traversal
    - Counts are bounded to prevent DoS
    - Checksums are validated for format (SHA-256 hex)
    - Timestamps are validated for ISO format
    """

    uploaded_at: datetime = Field(
        ...,
        description="ISO timestamp of when the upload was created",
    )
    conversation_count: int = Field(
        ...,
        ge=0,
        le=1_000_000,
        description="Number of conversations in this upload (max 1M)",
    )
    total_size_bytes: int | None = Field(
        None,
        ge=0,
        le=100_000_000_000,  # 100GB max
        description="Total size of all conversation files in bytes",
    )
    version: str = Field(
        "1.0",
        pattern=r"^\d+\.\d+$",
        description="Manifest format version",
    )
    system_type: str | None = Field(
        None,
        min_length=1,
        max_length=50,
        description="Type of Session-Buddy system that created this upload",
    )
    checksum: str | None = Field(
        None,
        pattern=r"^[a-f0-9]{64}$",
        description="SHA-256 checksum of manifest (64 hex characters)",
    )
    compressed: bool = Field(
        False,
        description="Whether conversation data is compressed",
    )
    compression_format: str | None = Field(
        None,
        pattern=r"^(gzip|zstd|lz4)$",
        description="Compression algorithm used (if compressed=True)",
    )
    files: list[str] = Field(
        default_factory=list,
        description="List of conversation file paths in this upload",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional metadata from Session-Buddy",
    )

    @field_validator("uploaded_at")
    @classmethod
    def validate_timestamp(cls, v: datetime) -> datetime:
        """Validate that timestamp is reasonable.

        Args:
            v: Uploaded timestamp

        Returns:
            Validated timestamp

        Raises:
            ValueError: If timestamp is too far in the future or past
        """
        now = datetime.now(UTC)

        # Timestamp shouldn't be in the future (allow 5 min clock skew)
        if v > now + timedelta(minutes=5):
            raise ValueError("Upload timestamp cannot be in the future")

        # Timestamp shouldn't be ancient (uploads from >1 year ago rejected)
        one_year_ago = now - timedelta(days=365)
        if v < one_year_ago:
            raise ValueError("Upload timestamp is too old (must be within last year)")

        return v

    @field_validator("files")
    @classmethod
    def validate_filenames(cls, v: list[str]) -> list[str]:
        """Validate that filenames are safe and don't contain path traversal.

        Args:
            v: List of filenames

        Returns:
            Validated filename list

        Raises:
            ValueError: If any filename is suspicious
        """
        # Check for path traversal patterns
        dangerous_patterns = ["..", "~", "\x00"]
        allowed_pattern = re.compile(r"^[a-zA-Z0-9_\-\.]+$")

        for filename in v:
            # Check for null bytes
            if "\x00" in filename:
                raise ValueError(f"Filename contains null byte: {filename}")

            # Check for path traversal
            for pattern in dangerous_patterns:
                if pattern in filename:
                    raise ValueError(
                        f"Filename contains dangerous pattern '{pattern}': {filename}"
                    )

            # Check for reasonable length
            if len(filename) > 255:
                raise ValueError(f"Filename too long: {filename}")

            # Validate format
            if not allowed_pattern.match(filename):
                raise ValueError(
                    f"Filename contains invalid characters: {filename}"
                )

        return v

    @field_validator("compression_format")
    @classmethod
    def validate_compression_format(cls, v: str | None) -> str | None:
        """Validate compression format matches compressed flag.

        Args:
            v: Compression format

        Returns:
            Validated compression format

        Raises:
            ValueError: If compression format specified without compressed=True
        """
        # This is validated at model validator level
        return v

    @model_validator(mode="after")
    def validate_compression_consistency(self) -> "SystemMemoryUploadManifest":
        """Validate that compression format is consistent with compressed flag.

        Returns:
            Validated manifest

        Raises:
            ValueError: If compression_format specified but compressed=False
        """
        if self.compression_format is not None and not self.compressed:
            raise ValueError(
                "compression_format specified but compressed=False"
            )

        if self.compressed and self.compression_format is None:
            raise ValueError(
                "compressed=True but compression_format not specified"
            )

        return self


# ============================================================================
# Security Validation Functions
# ============================================================================


def validate_system_id(system_id: str) -> str:
    """Validate system ID format.

    Args:
        system_id: System ID to validate

    Returns:
        Validated system ID

    Raises:
        ValueError: If system_id format is invalid
    """
    if not system_id:
        raise ValueError("system_id cannot be empty")

    # Check length
    if len(system_id) > 100:
        raise ValueError(f"system_id too long: {len(system_id)} characters (max: 100)")

    # Check for valid characters (alphanumeric, dash, underscore)
    pattern = re.compile(r"^[a-zA-Z0-9_-]+$")
    if not pattern.match(system_id):
        raise ValueError(
            f"Invalid system_id format: '{system_id}'. "
            "Must contain only alphanumeric characters, hyphens, and underscores"
        )

    return system_id


def validate_upload_id(upload_id: str) -> str:
    """Validate upload ID format.

    Args:
        upload_id: Upload ID to validate

    Returns:
        Validated upload ID

    Raises:
        ValueError: If upload_id format is invalid
    """
    if not upload_id:
        raise ValueError("upload_id cannot be empty")

    # Check length
    if len(upload_id) > 100:
        raise ValueError(f"upload_id too long: {len(upload_id)} characters (max: 100)")

    # Check for valid characters
    pattern = re.compile(r"^[a-zA-Z0-9_\-\.]+$")
    if not pattern.match(upload_id):
        raise ValueError(
            f"Invalid upload_id format: '{upload_id}'. "
            "Must contain only alphanumeric characters, hyphens, underscores, and periods"
        )

    # Check for path traversal
    if ".." in upload_id or upload_id.startswith("/"):
        raise ValueError(
            f"Invalid upload_id format: '{upload_id}'. Path traversal detected"
        )

    return upload_id


def validate_storage_prefix(prefix: str) -> str:
    """Validate storage prefix for security.

    Args:
        prefix: Storage prefix to validate

    Returns:
        Validated storage prefix

    Raises:
        ValueError: If prefix contains dangerous patterns
    """
    if not prefix:
        raise ValueError("storage_prefix cannot be empty")

    # Check for null bytes
    if "\x00" in prefix:
        raise ValueError("storage_prefix contains null byte")

    # Check for path traversal attempts
    if ".." in prefix:
        raise ValueError("storage_prefix cannot contain '..' (path traversal)")

    # Ensure prefix starts with systems/
    if not prefix.startswith("systems/"):
        raise ValueError(
            f"storage_prefix must start with 'systems/': {prefix}"
        )

    # Validate each path component
    components = prefix.split("/")
    for component in components:
        if not component:
            continue  # Allow empty components from trailing slashes

        # Check for dangerous patterns
        if component in [".", "~", "*", "?"]:
            raise ValueError(
                f"Invalid path component in storage_prefix: '{component}'"
            )

    return prefix


__all__ = [
    # Ingestion schemas
    "SystemMemoryUploadManifest",
    # Validation functions
    "validate_system_id",
    "validate_upload_id",
    "validate_storage_prefix",
]
