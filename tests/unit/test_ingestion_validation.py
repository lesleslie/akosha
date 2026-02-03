"""Tests for ingestion manifest validation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from akosha.models.schemas import (
    SystemMemoryUploadManifest,
    validate_storage_prefix,
    validate_system_id,
    validate_upload_id,
)


class TestSystemMemoryUploadManifest:
    """Test SystemMemoryUploadManifest validation."""

    def test_valid_manifest_minimal(self) -> None:
        """Test valid manifest with minimal fields."""
        now = datetime.now(UTC)
        manifest_dict = {
            "uploaded_at": now.isoformat(),
            "conversation_count": 100,
        }

        manifest = SystemMemoryUploadManifest(**manifest_dict)

        assert manifest.uploaded_at <= now
        assert manifest.conversation_count == 100
        assert manifest.version == "1.0"  # default
        assert manifest.compressed is False  # default

    def test_valid_manifest_complete(self) -> None:
        """Test valid manifest with all fields."""
        now = datetime.now(UTC)
        manifest_dict = {
            "uploaded_at": now.isoformat(),
            "conversation_count": 500,
            "total_size_bytes": 10_485_760,  # 10MB
            "version": "1.0",
            "system_type": "session-buddy",
            "checksum": "a" * 64,  # SHA-256 hex
            "compressed": True,
            "compression_format": "gzip",
            "files": ["conv_1.json", "conv_2.json"],
            "metadata": {"region": "us-west-2"},
        }

        manifest = SystemMemoryUploadManifest(**manifest_dict)

        assert manifest.conversation_count == 500
        assert manifest.total_size_bytes == 10_485_760
        assert manifest.checksum == "a" * 64
        assert manifest.compressed is True
        assert manifest.compression_format == "gzip"
        assert manifest.files == ["conv_1.json", "conv_2.json"]

    def test_conversation_count_out_of_range(self) -> None:
        """Test that conversation_count bounds are enforced."""
        # Test minimum (0 should be allowed)
        SystemMemoryUploadManifest(
            uploaded_at=datetime.now(UTC).isoformat(),
            conversation_count=0,
        )

        # Test negative (should fail)
        with pytest.raises(ValueError):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC).isoformat(),
                conversation_count=-1,
            )

        # Test too large (should fail)
        with pytest.raises(ValueError):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC).isoformat(),
                conversation_count=1_000_001,  # Exceeds max
            )

    def test_total_size_bounds(self) -> None:
        """Test that total_size_bytes bounds are enforced."""
        # Test too large (100GB + 1 byte, should fail)
        with pytest.raises(ValueError):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC).isoformat(),
                conversation_count=1,
                total_size_bytes=100_000_000_001,
            )

    def test_checksum_format(self) -> None:
        """Test that checksum format is validated."""
        # Valid checksum (64 hex chars)
        SystemMemoryUploadManifest(
            uploaded_at=datetime.now(UTC).isoformat(),
            conversation_count=1,
            checksum="a" * 64,
        )

        # Invalid: too short
        with pytest.raises(ValueError):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC).isoformat(),
                conversation_count=1,
                checksum="a" * 63,
            )

        # Invalid: contains non-hex characters
        with pytest.raises(ValueError):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC).isoformat(),
                conversation_count=1,
                checksum="g" * 64,  # 'g' is not hex
            )

    def test_timestamp_validation(self) -> None:
        """Test that timestamp validation works."""
        now = datetime.now(UTC)

        # Valid: current time
        manifest = SystemMemoryUploadManifest(
            uploaded_at=now.isoformat(),
            conversation_count=1,
        )
        assert manifest.uploaded_at <= now

        # Invalid: future timestamp (>5 min ahead)
        future_time = now + timedelta(minutes=6)
        with pytest.raises(ValueError, match="cannot be in the future"):
            SystemMemoryUploadManifest(
                uploaded_at=future_time.isoformat(),
                conversation_count=1,
            )

        # Invalid: too old (>1 year ago)
        old_time = now - timedelta(days=366)
        with pytest.raises(ValueError, match="too old"):
            SystemMemoryUploadManifest(
                uploaded_at=old_time.isoformat(),
                conversation_count=1,
            )

    def test_filename_validation(self) -> None:
        """Test that filenames are validated for security."""
        # Valid filenames
        valid_files = [
            "conv_1.json",
            "conv-2.json",
            "conversations.db",
            "backup_2025-02-03.tar.gz",
        ]

        manifest = SystemMemoryUploadManifest(
            uploaded_at=datetime.now(UTC).isoformat(),
            conversation_count=1,
            files=valid_files,
        )
        assert manifest.files == valid_files

        # Invalid: contains null byte
        with pytest.raises(ValueError, match="null byte"):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC).isoformat(),
                conversation_count=1,
                files=["test\x00file.json"],
            )

        # Invalid: path traversal
        with pytest.raises(ValueError, match="dangerous pattern"):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC).isoformat(),
                conversation_count=1,
                files=["../etc/passwd"],
            )

        # Invalid: too long
        with pytest.raises(ValueError, match="too long"):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC).isoformat(),
                conversation_count=1,
                files=["a" * 256],
            )

        # Invalid: invalid characters
        with pytest.raises(ValueError, match="invalid characters"):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC).isoformat(),
                conversation_count=1,
                files=["file;rm -rf /"],
            )

    def test_compression_consistency(self) -> None:
        """Test compression format consistency validation."""
        # Valid: compressed with format
        manifest = SystemMemoryUploadManifest(
            uploaded_at=datetime.now(UTC).isoformat(),
            conversation_count=1,
            compressed=True,
            compression_format="gzip",
        )
        assert manifest.compressed is True
        assert manifest.compression_format == "gzip"

        # Valid: not compressed
        manifest = SystemMemoryUploadManifest(
            uploaded_at=datetime.now(UTC).isoformat(),
            conversation_count=1,
            compressed=False,
        )
        assert manifest.compressed is False
        assert manifest.compression_format is None

        # Invalid: format without compressed flag
        with pytest.raises(ValueError, match="compression_format specified but compressed"):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC).isoformat(),
                conversation_count=1,
                compressed=False,
                compression_format="gzip",
            )

        # Invalid: compressed without format
        with pytest.raises(ValueError, match="compressed=True but compression_format"):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC).isoformat(),
                conversation_count=1,
                compressed=True,
            )

    def test_compression_format_validation(self) -> None:
        """Test that compression_format values are validated."""
        # Valid formats
        for fmt in ["gzip", "zstd", "lz4"]:
            manifest = SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC).isoformat(),
                conversation_count=1,
                compressed=True,
                compression_format=fmt,
            )
            assert manifest.compression_format == fmt

        # Invalid format
        with pytest.raises(ValueError, match="pattern"):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC).isoformat(),
                conversation_count=1,
                compressed=True,
                compression_format="invalid",
            )


class TestSystemIDValidation:
    """Test system_id validation."""

    def test_valid_system_ids(self) -> None:
        """Test valid system ID formats."""
        valid_ids = [
            "system-1",
            "system_001",
            "System1",
            "my-system",
            "test_system_2025",
        ]

        for system_id in valid_ids:
            result = validate_system_id(system_id)
            assert result == system_id

    def test_empty_system_id(self) -> None:
        """Test that empty system_id is rejected."""
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_system_id("")

    def test_system_id_too_long(self) -> None:
        """Test that overly long system_id is rejected."""
        with pytest.raises(ValueError, match="too long"):
            validate_system_id("a" * 101)

    def test_system_id_invalid_characters(self) -> None:
        """Test that invalid characters are rejected."""
        invalid_ids = [
            "system/1",  # Forward slash
            "system;1",  # Semicolon
            "system@1",  # At-sign
            "system:1",  # Colon
            "system.1",  # Period is valid
            "system 1",  # Space
        ]

        for system_id in invalid_ids:
            if system_id == "system.1":  # Period is valid
                continue
            with pytest.raises(ValueError, match="Invalid system_id"):
                validate_system_id(system_id)


class TestUploadIDValidation:
    """Test upload_id validation."""

    def test_valid_upload_ids(self) -> None:
        """Test valid upload ID formats."""
        valid_ids = [
            "upload-001",
            "upload_2025-02-03",
            "upload.v1",
            "batch_12345",
        ]

        for upload_id in valid_ids:
            result = validate_upload_id(upload_id)
            assert result == upload_id

    def test_empty_upload_id(self) -> None:
        """Test that empty upload_id is rejected."""
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_upload_id("")

    def test_upload_id_too_long(self) -> None:
        """Test that overly long upload_id is rejected."""
        with pytest.raises(ValueError, match="too long"):
            validate_upload_id("a" * 101)

    def test_upload_id_path_traversal(self) -> None:
        """Test that path traversal attempts are rejected."""
        malicious_ids = [
            "../upload",
            "upload/../../etc/passwd",
            "/etc/passwd",
        ]

        for upload_id in malicious_ids:
            # These will fail the character validation first
            with pytest.raises(ValueError, match="Invalid upload_id|Path traversal"):
                validate_upload_id(upload_id)


class TestStoragePrefixValidation:
    """Test storage_prefix validation."""

    def test_valid_storage_prefixes(self) -> None:
        """Test valid storage prefix formats."""
        valid_prefixes = [
            "systems/system-001/",
            "systems/system_001/upload-123/",
            "systems/my-system/",
        ]

        for prefix in valid_prefixes:
            result = validate_storage_prefix(prefix)
            assert result == prefix

    def test_empty_storage_prefix(self) -> None:
        """Test that empty storage_prefix is rejected."""
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_storage_prefix("")

    def test_storage_prefix_null_byte(self) -> None:
        """Test that null bytes are rejected."""
        with pytest.raises(ValueError, match="null byte"):
            validate_storage_prefix("systems/test\x00/")

    def test_storage_prefix_path_traversal(self) -> None:
        """Test that path traversal is rejected."""
        malicious_prefixes = [
            "systems/../etc/",
            "systems/../../../",
            "systems/./secrets/",
        ]

        for prefix in malicious_prefixes:
            with pytest.raises(ValueError, match="cannot contain|Invalid path component"):
                validate_storage_prefix(prefix)

    def test_storage_prefix_missing_systems(self) -> None:
        """Test that prefix must start with systems/."""
        invalid_prefixes = [
            "uploads/",
            "/systems/test/",
            "data/systems/test/",
            "systems1/",  # Missing slash
        ]

        for prefix in invalid_prefixes:
            with pytest.raises(ValueError, match="must start with"):
                validate_storage_prefix(prefix)


class TestJSONSecurity:
    """Test security aspects of JSON validation."""

    def test_prevents_malformed_json(self) -> None:
        """Test that malformed JSON is rejected gracefully."""
        malformed_json = [
            "{invalid json",
            '{"uploaded_at": "invalid-date"',
            '{"conversation_count": "not-a-number"}',
            '{"conversation_count": 100, "checksum": "short"}',
        ]

        for json_str in malformed_json:
            # Should not crash, should raise validation error
            with pytest.raises(Exception):
                manifest_dict = json.loads(json_str)
                SystemMemoryUploadManifest(**manifest_dict)

    def test_prevents_injection_via_filenames(self) -> None:
        """Test that filename injection is prevented."""
        now = datetime.now(UTC)
        malicious_manifests = [
            {
                "uploaded_at": now.isoformat(),
                "conversation_count": 1,
                "files": ["../../etc/passwd"],
            },
            {
                "uploaded_at": now.isoformat(),
                "conversation_count": 1,
                "files": ["config\x00.ini"],
            },
            {
                "uploaded_at": now.isoformat(),
                "conversation_count": 1,
                "files": ["file;rm -rf /"],
            },
        ]

        for manifest_dict in malicious_manifests:
            with pytest.raises(ValueError, match="dangerous pattern|invalid characters|null byte"):
                SystemMemoryUploadManifest(**manifest_dict)

    def test_prevents_dos_via_large_counts(self) -> None:
        """Test that DoS via large counts is prevented."""
        now = datetime.now(UTC)
        dos_manifests = [
            {
                "uploaded_at": now.isoformat(),
                "conversation_count": 1_000_001,  # Exceeds max
            },
            {
                "uploaded_at": now.isoformat(),
                "conversation_count": 100,
                "total_size_bytes": 100_000_000_001,  # 100GB + 1 byte
            },
        ]

        for manifest_dict in dos_manifests:
            with pytest.raises(ValueError):
                SystemMemoryUploadManifest(**manifest_dict)

    def test_prevents_future_timestamp_attack(self) -> None:
        """Test that future timestamps are rejected."""
        now = datetime.now(UTC)
        future_time = now + timedelta(hours=1)

        with pytest.raises(ValueError, match="cannot be in the future"):
            SystemMemoryUploadManifest(
                uploaded_at=future_time.isoformat(),
                conversation_count=1,
            )

    def test_prevents_stale_data_attack(self) -> None:
        """Test that ancient data is rejected."""
        now = datetime.now(UTC)
        old_time = now - timedelta(days=400)

        with pytest.raises(ValueError, match="too old"):
            SystemMemoryUploadManifest(
                uploaded_at=old_time.isoformat(),
                conversation_count=1,
            )

    def test_checksum_validation_format(self) -> None:
        """Test that checksum format is strictly validated."""
        invalid_checksums = [
            "a" * 63,  # Too short
            "a" * 65,  # Too long
            "g" * 64,  # Invalid hex characters
            "0" * 64,  # All zeros (unlikely but valid format)
        ]

        for checksum in invalid_checksums:
            if checksum == "0" * 64:  # All zeros is actually valid hex
                continue
            with pytest.raises(ValueError, match="pattern|format"):
                SystemMemoryUploadManifest(
                    uploaded_at=datetime.now(UTC).isoformat(),
                    conversation_count=1,
                    checksum=checksum,
                )


class TestValidationIntegration:
    """Integration tests for manifest validation."""

    def test_real_world_manifest(self) -> None:
        """Test validation of realistic Session-Buddy manifest."""
        # Simulated real manifest from Session-Buddy
        now = datetime.now(UTC)
        manifest_dict = {
            "uploaded_at": now.isoformat(),
            "conversation_count": 1250,
            "total_size_bytes": 52_428_800,
            "version": "1.0",
            "system_type": "session-buddy",
            "checksum": "a" * 64,  # Valid 64-char hex checksum
            "compressed": True,
            "compression_format": "gzip",
            "files": [
                "conversations_part1.db",
                "conversations_part2.db",
                "conversations_part3.db",
            ],
            "metadata": {
                "session_buddy_version": "2.1.0",
                "export_format": "v1",
            },
        }

        manifest = SystemMemoryUploadManifest(**manifest_dict)

        assert manifest.conversation_count == 1250
        assert manifest.files[0] == "conversations_part1.db"
        assert len(manifest.files) == 3
        assert manifest.metadata["session_buddy_version"] == "2.1.0"

    def test_graceful_handling_of_optional_fields(self) -> None:
        """Test that optional fields are handled gracefully."""
        # Manifest with only required fields
        now = datetime.now(UTC)
        minimal_dict = {
            "uploaded_at": now.isoformat(),
            "conversation_count": 500,
        }

        manifest = SystemMemoryUploadManifest(**minimal_dict)

        assert manifest.total_size_bytes is None
        assert manifest.version == "1.0"
        assert manifest.compressed is False
        assert manifest.files == []
        assert manifest.metadata == {}
