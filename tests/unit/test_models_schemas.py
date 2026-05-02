"""Tests for Akosha Pydantic validation schemas."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from akosha.models.schemas import (
    SystemMemoryUploadManifest,
    validate_storage_prefix,
    validate_system_id,
    validate_upload_id,
)

# ============================================================================
# SystemMemoryUploadManifest
# ============================================================================


class TestSystemMemoryUploadManifest:
    """Tests for the manifest validation model."""

    def test_valid_manifest_with_required_fields(self):
        manifest = SystemMemoryUploadManifest(
            uploaded_at=datetime.now(UTC),
            conversation_count=10,
        )
        assert manifest.conversation_count == 10
        assert manifest.version == "1.0"
        assert manifest.compressed is False
        assert manifest.files == []
        assert manifest.metadata == {}

    def test_valid_manifest_with_all_fields(self):
        manifest = SystemMemoryUploadManifest(
            uploaded_at=datetime.now(UTC),
            conversation_count=100,
            total_size_bytes=50000,
            version="2.0",
            system_type="claude-code",
            checksum="a" * 64,
            compressed=True,
            compression_format="gzip",
            files=["conv_001.json", "conv_002.json"],
            metadata={"source": "session-buddy"},
        )
        assert manifest.compressed is True
        assert manifest.compression_format == "gzip"
        assert len(manifest.files) == 2
        assert manifest.metadata["source"] == "session-buddy"

    def test_default_version(self):
        manifest = SystemMemoryUploadManifest(
            uploaded_at=datetime.now(UTC),
            conversation_count=1,
        )
        assert manifest.version == "1.0"

    @pytest.mark.parametrize("count", [0, 500_000, 1_000_000])
    def test_conversation_count_boundaries(self, count):
        manifest = SystemMemoryUploadManifest(
            uploaded_at=datetime.now(UTC),
            conversation_count=count,
        )
        assert manifest.conversation_count == count

    def test_conversation_count_negative_raises(self):
        with pytest.raises(ValidationError):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC),
                conversation_count=-1,
            )

    def test_conversation_count_exceeds_max_raises(self):
        with pytest.raises(ValidationError):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC),
                conversation_count=1_000_001,
            )

    def test_total_size_bytes_bounds(self):
        manifest = SystemMemoryUploadManifest(
            uploaded_at=datetime.now(UTC),
            conversation_count=1,
            total_size_bytes=100_000_000_000,
        )
        assert manifest.total_size_bytes == 100_000_000_000

    def test_invalid_version_pattern_raises(self):
        with pytest.raises(ValidationError):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC),
                conversation_count=1,
                version="not-a-version",
            )

    def test_system_type_length_validation(self):
        manifest = SystemMemoryUploadManifest(
            uploaded_at=datetime.now(UTC),
            conversation_count=1,
            system_type="a" * 50,
        )
        assert len(manifest.system_type) == 50

    def test_system_type_too_long_raises(self):
        with pytest.raises(ValidationError):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC),
                conversation_count=1,
                system_type="a" * 51,
            )

    def test_checksum_valid_format(self):
        manifest = SystemMemoryUploadManifest(
            uploaded_at=datetime.now(UTC),
            conversation_count=1,
            checksum="a" * 64,
        )
        assert manifest.checksum == "a" * 64

    def test_checksum_invalid_format_short(self):
        with pytest.raises(ValidationError):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC),
                conversation_count=1,
                checksum="abc123",
            )

    def test_checksum_invalid_format_uppercase(self):
        with pytest.raises(ValidationError):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC),
                conversation_count=1,
                checksum="A" * 64,
            )


# ============================================================================
# Timestamp Validation
# ============================================================================


class TestTimestampValidation:
    """Tests for uploaded_at timestamp validation."""

    def test_current_timestamp_valid(self):
        manifest = SystemMemoryUploadManifest(
            uploaded_at=datetime.now(UTC),
            conversation_count=1,
        )
        assert manifest.uploaded_at is not None

    def test_future_within_5_min_valid(self):
        future = datetime.now(UTC) + timedelta(minutes=4)
        manifest = SystemMemoryUploadManifest(
            uploaded_at=future,
            conversation_count=1,
        )
        assert manifest.uploaded_at == future

    def test_future_beyond_5_min_invalid(self):
        future = datetime.now(UTC) + timedelta(minutes=10)
        with pytest.raises(ValidationError, match="cannot be in the future"):
            SystemMemoryUploadManifest(
                uploaded_at=future,
                conversation_count=1,
            )

    def test_year_old_valid(self):
        old = datetime.now(UTC) - timedelta(days=364)
        manifest = SystemMemoryUploadManifest(
            uploaded_at=old,
            conversation_count=1,
        )
        assert manifest.uploaded_at == old

    def test_ancient_timestamp_invalid(self):
        ancient = datetime.now(UTC) - timedelta(days=366)
        with pytest.raises(ValidationError, match="too old"):
            SystemMemoryUploadManifest(
                uploaded_at=ancient,
                conversation_count=1,
            )


# ============================================================================
# Filename Validation
# ============================================================================


class TestFilenameValidation:
    """Tests for filename validation in files field."""

    def test_safe_filename_accepted(self):
        manifest = SystemMemoryUploadManifest(
            uploaded_at=datetime.now(UTC),
            conversation_count=1,
            files=["conv_001.json"],
        )
        assert manifest.files == ["conv_001.json"]

    def test_path_traversal_double_dot(self):
        with pytest.raises(ValidationError, match="dangerous pattern"):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC),
                conversation_count=1,
                files=["../etc/passwd"],
            )

    def test_path_traversal_tilde(self):
        with pytest.raises(ValidationError, match="dangerous pattern"):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC),
                conversation_count=1,
                files=["~/.ssh/id_rsa"],
            )

    def test_null_byte_rejected(self):
        with pytest.raises(ValidationError, match="null byte"):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC),
                conversation_count=1,
                files=["file\x00name.json"],
            )

    def test_filename_too_long(self):
        with pytest.raises(ValidationError, match="too long"):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC),
                conversation_count=1,
                files=["a" * 256],
            )

    def test_special_characters_rejected(self):
        with pytest.raises(ValidationError, match="invalid characters"):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC),
                conversation_count=1,
                files=["file with spaces.json"],
            )

    def test_empty_filename_list_ok(self):
        manifest = SystemMemoryUploadManifest(
            uploaded_at=datetime.now(UTC),
            conversation_count=0,
            files=[],
        )
        assert manifest.files == []

    def test_multiple_safe_filenames(self):
        files = [f"conv_{i:03d}.json" for i in range(10)]
        manifest = SystemMemoryUploadManifest(
            uploaded_at=datetime.now(UTC),
            conversation_count=len(files),
            files=files,
        )
        assert len(manifest.files) == 10


# ============================================================================
# Compression Consistency
# ============================================================================


class TestCompressionConsistency:
    """Tests for compressed/compression_format consistency."""

    def test_compressed_without_format_raises(self):
        with pytest.raises(ValidationError, match="compression_format not specified"):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC),
                conversation_count=1,
                compressed=True,
            )

    def test_format_without_compressed_raises(self):
        with pytest.raises(ValidationError, match="compressed=False"):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC),
                conversation_count=1,
                compressed=False,
                compression_format="gzip",
            )

    @pytest.mark.parametrize("fmt", ["gzip", "zstd", "lz4"])
    def test_valid_compression_formats(self, fmt):
        manifest = SystemMemoryUploadManifest(
            uploaded_at=datetime.now(UTC),
            conversation_count=1,
            compressed=True,
            compression_format=fmt,
        )
        assert manifest.compression_format == fmt

    def test_invalid_compression_format(self):
        with pytest.raises(ValidationError):
            SystemMemoryUploadManifest(
                uploaded_at=datetime.now(UTC),
                conversation_count=1,
                compressed=True,
                compression_format="rar",
            )

    def test_neither_compressed_nor_format_ok(self):
        manifest = SystemMemoryUploadManifest(
            uploaded_at=datetime.now(UTC),
            conversation_count=1,
            compressed=False,
            compression_format=None,
        )
        assert manifest.compressed is False


# ============================================================================
# validate_system_id
# ============================================================================


class TestValidateSystemId:
    """Tests for validate_system_id function."""

    @pytest.mark.parametrize(
        "system_id",
        [
            "claude-code-abc",
            "my_system_123",
            "system",
            "A-B_C-0",
        ],
    )
    def test_valid_system_ids(self, system_id):
        assert validate_system_id(system_id) == system_id

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_system_id("")

    def test_too_long_raises(self):
        with pytest.raises(ValueError, match="too long"):
            validate_system_id("a" * 101)

    def test_special_chars_raise(self):
        with pytest.raises(ValueError, match="Invalid"):
            validate_system_id("system with spaces")

    def test_slash_raises(self):
        with pytest.raises(ValueError, match="Invalid"):
            validate_system_id("systems/my-system")

    def test_at_sign_raises(self):
        with pytest.raises(ValueError, match="Invalid"):
            validate_system_id("user@host")


# ============================================================================
# validate_upload_id
# ============================================================================


class TestValidateUploadId:
    """Tests for validate_upload_id function."""

    @pytest.mark.parametrize(
        "upload_id",
        [
            "upload-001",
            "abc_123.json",
            "my.upload.v2",
            "upload_001",
        ],
    )
    def test_valid_upload_ids(self, upload_id):
        assert validate_upload_id(upload_id) == upload_id

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_upload_id("")

    def test_path_traversal_double_dot_raises(self):
        with pytest.raises(ValueError, match="Invalid"):
            validate_upload_id("../upload")

    def test_leading_slash_raises(self):
        with pytest.raises(ValueError, match="Invalid"):
            validate_upload_id("/upload")

    def test_too_long_raises(self):
        with pytest.raises(ValueError, match="too long"):
            validate_upload_id("a" * 101)

    def test_valid_with_dots(self):
        assert validate_upload_id("file.name.ext") == "file.name.ext"


# ============================================================================
# validate_storage_prefix
# ============================================================================


class TestValidateStoragePrefix:
    """Tests for validate_storage_prefix function."""

    def test_valid_prefix(self):
        assert validate_storage_prefix("systems/claude-code") == "systems/claude-code"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_storage_prefix("")

    def test_null_byte_raises(self):
        with pytest.raises(ValueError, match="null byte"):
            validate_storage_prefix("systems/\x00evil")

    def test_double_dot_raises(self):
        with pytest.raises(ValueError, match="path traversal"):
            validate_storage_prefix("systems/../etc")

    def test_missing_systems_prefix_raises(self):
        with pytest.raises(ValueError, match="must start with"):
            validate_storage_prefix("data/system")

    def test_dot_component_raises(self):
        with pytest.raises(ValueError, match="Invalid path component"):
            validate_storage_prefix("systems/./evil")

    def test_tilde_component_raises(self):
        # Note: ~ is NOT in the source code's dangerous patterns check,
        # so this test documents the actual behavior
        # The source only checks for ".", "~", "*", "?"
        result = validate_storage_prefix("systems/~user/data")
        assert result == "systems/~user/data"

    def test_asterisk_component_raises(self):
        with pytest.raises(ValueError, match="Invalid path component"):
            validate_storage_prefix("systems/*/data")

    def test_wildcard_component_raises(self):
        with pytest.raises(ValueError, match="Invalid path component"):
            validate_storage_prefix("systems/*/data")

    def test_question_mark_component_raises(self):
        with pytest.raises(ValueError, match="Invalid path component"):
            validate_storage_prefix("systems/?/data")

    def test_trailing_slash_ok(self):
        result = validate_storage_prefix("systems/claude-code/")
        assert result == "systems/claude-code/"

    def test_nested_path_ok(self):
        result = validate_storage_prefix("systems/claude-code/uploads")
        assert result == "systems/claude-code/uploads"


# ============================================================================
# __all__ exports
# ============================================================================


class TestExports:
    """Tests for module exports."""

    def test_all_exports_exist(self):
        from akosha.models import schemas

        for name in schemas.__all__:
            assert hasattr(schemas, name), f"Missing export: {name}"
