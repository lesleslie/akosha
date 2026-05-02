"""Tests for structured security logging.

Tests SecurityLogger, get_security_logger, severity constants,
and event formatting from akosha.observability.security_logging.
"""

import json
import logging
from unittest.mock import patch

import pytest

from akosha.observability.security_logging import (
    SecurityLogger,
    get_security_logger,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def capture_logger():
    """Create a logger that captures INFO messages."""
    log = logging.getLogger("test_security")
    log.setLevel(logging.INFO)
    records = []
    handler = logging.Handler()
    handler.setLevel(logging.INFO)
    handler.emit = lambda r: records.append(r)
    log.addHandler(handler)
    yield log, records
    log.removeHandler(handler)


@pytest.fixture
def sec_logger():
    """Create a SecurityLogger with a capture logger."""
    log = logging.getLogger("sec_test")
    log.setLevel(logging.INFO)
    records = []
    handler = logging.Handler()
    handler.setLevel(logging.INFO)
    handler.emit = lambda r: records.append(r)
    log.addHandler(handler)
    logger = SecurityLogger(logger=log)
    yield logger, records
    log.removeHandler(handler)


# ============================================================================
# Severity constants
# ============================================================================


class TestSeverityConstants:
    """Tests for severity level constants."""

    def test_all_severities_defined(self):
        assert SecurityLogger.SEVERITY_CRITICAL == "CRITICAL"
        assert SecurityLogger.SEVERITY_HIGH == "HIGH"
        assert SecurityLogger.SEVERITY_MEDIUM == "MEDIUM"
        assert SecurityLogger.SEVERITY_LOW == "LOW"
        assert SecurityLogger.SEVERITY_INFO == "INFO"

    def test_severity_on_class(self):
        assert SecurityLogger.SEVERITY_CRITICAL == "CRITICAL"
        assert SecurityLogger.SEVERITY_HIGH == "HIGH"
        assert SecurityLogger.SEVERITY_MEDIUM == "MEDIUM"
        assert SecurityLogger.SEVERITY_LOW == "LOW"
        assert SecurityLogger.SEVERITY_INFO == "INFO"


# ============================================================================
# SecurityLogger initialization
# ============================================================================


class TestSecurityLoggerInit:
    """Tests for SecurityLogger initialization."""

    def test_default_logger_is_security(self):
        logger = SecurityLogger()
        assert logger.logger.name == "security"

    def test_custom_logger(self):
        custom = logging.getLogger("custom_sec")
        logger = SecurityLogger(logger=custom)
        assert logger.logger.name == "custom_sec"

    def test_custom_logger_preserves_level(self):
        custom = logging.getLogger("custom_sec")
        custom.setLevel(logging.WARNING)
        logger = SecurityLogger(logger=custom)
        assert logger.logger.level == logging.WARNING


# ============================================================================
# _log_event internal method
# ============================================================================


class TestLogEvent:
    """Tests for the internal _log_event method."""

    def test_emits_json(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger._log_event("test_event", "INFO", "test message")
        assert len(records) == 1
        msg = records[0].getMessage()
        parsed = json.loads(msg)
        assert parsed["event_type"] == "test_event"
        assert parsed["severity"] == "INFO"
        assert parsed["message"] == "test message"

    def test_includes_timestamp(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger._log_event("test", "INFO", "msg")
        parsed = json.loads(records[0].getMessage())
        assert "timestamp" in parsed
        assert "T" in parsed["timestamp"]  # ISO format

    def test_includes_extra_context(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger._log_event("test", "HIGH", "msg", key1="val1", key2=42)
        parsed = json.loads(records[0].getMessage())
        assert parsed["key1"] == "val1"
        assert parsed["key2"] == 42

    def test_calls_record_counter(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter") as mock_rc:
            logger._log_event("auth_failure", "MEDIUM", "fail")
        mock_rc.assert_called_once_with(
            "security.auth_failure",
            1,
            {"severity": "MEDIUM"},
        )


# ============================================================================
# log_auth_success
# ============================================================================


class TestLogAuthSuccess:
    """Tests for log_auth_success method."""

    def test_logs_success_event(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_auth_success("user123", "10.0.0.1")
        parsed = json.loads(records[0].getMessage())
        assert parsed["event_type"] == "auth_success"
        assert parsed["severity"] == "INFO"
        assert parsed["user_id"] == "user123"
        assert parsed["source_ip"] == "10.0.0.1"
        assert parsed["auth_method"] == "jwt"

    def test_custom_auth_method(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_auth_success("user123", "10.0.0.1", method="api_token")
        parsed = json.loads(records[0].getMessage())
        assert parsed["auth_method"] == "api_token"

    def test_message_contains_user_id(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_auth_success("alice", "1.2.3.4")
        parsed = json.loads(records[0].getMessage())
        assert "alice" in parsed["message"]


# ============================================================================
# log_auth_failure
# ============================================================================


class TestLogAuthFailure:
    """Tests for log_auth_failure method."""

    def test_logs_failure_event(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_auth_failure("bad token", "10.0.0.1")
        parsed = json.loads(records[0].getMessage())
        assert parsed["event_type"] == "auth_failure"
        assert parsed["severity"] == "MEDIUM"
        assert parsed["reason"] == "bad token"
        assert parsed["source_ip"] == "10.0.0.1"

    def test_failure_with_user_id(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_auth_failure("expired", "10.0.0.1", user_id="bob")
        parsed = json.loads(records[0].getMessage())
        assert parsed["user_id"] == "bob"

    def test_failure_without_user_id(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_auth_failure("no token", "10.0.0.1")
        parsed = json.loads(records[0].getMessage())
        assert parsed["user_id"] is None


# ============================================================================
# log_rate_limit_exceeded
# ============================================================================


class TestLogRateLimitExceeded:
    """Tests for log_rate_limit_exceeded method."""

    def test_logs_rate_limit_event(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_rate_limit_exceeded("user1", 100, 5.0)
        parsed = json.loads(records[0].getMessage())
        assert parsed["event_type"] == "rate_limit_exceeded"
        assert parsed["severity"] == "MEDIUM"
        assert parsed["user_id"] == "user1"
        assert parsed["requested_tokens"] == 100
        assert parsed["available_tokens"] == 5.0

    def test_severity_is_medium(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_rate_limit_exceeded("user1", 100, 0)
        parsed = json.loads(records[0].getMessage())
        assert parsed["severity"] == "MEDIUM"


# ============================================================================
# log_sql_injection_attempt
# ============================================================================


class TestLogSQLInjection:
    """Tests for log_sql_injection_attempt method."""

    def test_logs_injection_event(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_sql_injection_attempt(
                "SELECT * FROM users WHERE 1=1",
                "192.168.1.1",
            )
        parsed = json.loads(records[0].getMessage())
        assert parsed["event_type"] == "sql_injection_attempt"
        assert parsed["severity"] == "HIGH"
        assert parsed["source_ip"] == "192.168.1.1"
        assert parsed["query"] == "SELECT * FROM users WHERE 1=1"

    def test_severity_is_high(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_sql_injection_attempt("'; DROP TABLE", "10.0.0.1")
        parsed = json.loads(records[0].getMessage())
        assert parsed["severity"] == "HIGH"

    def test_truncates_long_query(self, sec_logger):
        logger, records = sec_logger
        long_query = "a" * 600
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_sql_injection_attempt(long_query, "10.0.0.1")
        parsed = json.loads(records[0].getMessage())
        assert len(parsed["query"]) == 500

    def test_short_query_not_truncated(self, sec_logger):
        logger, records = sec_logger
        short_query = "SELECT 1"
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_sql_injection_attempt(short_query, "10.0.0.1")
        parsed = json.loads(records[0].getMessage())
        assert parsed["query"] == "SELECT 1"

    def test_with_user_id(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_sql_injection_attempt("DROP TABLE", "10.0.0.1", user_id="attacker")
        parsed = json.loads(records[0].getMessage())
        assert parsed["user_id"] == "attacker"


# ============================================================================
# log_path_traversal_attempt
# ============================================================================


class TestLogPathTraversal:
    """Tests for log_path_traversal_attempt method."""

    def test_logs_traversal_event(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_path_traversal_attempt("../../etc/passwd", "10.0.0.1")
        parsed = json.loads(records[0].getMessage())
        assert parsed["event_type"] == "path_traversal_attempt"
        assert parsed["severity"] == "HIGH"
        assert parsed["path"] == "../../etc/passwd"
        assert parsed["source_ip"] == "10.0.0.1"

    def test_truncates_long_path(self, sec_logger):
        logger, records = sec_logger
        long_path = "a" * 600
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_path_traversal_attempt(long_path, "10.0.0.1")
        parsed = json.loads(records[0].getMessage())
        assert len(parsed["path"]) == 500

    def test_without_user_id(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_path_traversal_attempt("/etc/shadow", "10.0.0.1")
        parsed = json.loads(records[0].getMessage())
        assert parsed["user_id"] is None


# ============================================================================
# log_mcp_tool_access
# ============================================================================


class TestLogMCPToolAccess:
    """Tests for log_mcp_tool_access method."""

    def test_logs_tool_access(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_mcp_tool_access("search_all_systems", "user1", "10.0.0.1", True)
        parsed = json.loads(records[0].getMessage())
        assert parsed["event_type"] == "mcp_tool_access"
        assert parsed["severity"] == "INFO"
        assert parsed["tool_name"] == "search_all_systems"
        assert parsed["user_id"] == "user1"
        assert parsed["success"] is True

    def test_logs_failed_access(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_mcp_tool_access("admin_tool", "user1", "10.0.0.1", False)
        parsed = json.loads(records[0].getMessage())
        assert parsed["success"] is False

    def test_message_includes_tool_and_user(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_mcp_tool_access("my_tool", "alice", "1.2.3.4", True)
        parsed = json.loads(records[0].getMessage())
        assert "my_tool" in parsed["message"]
        assert "alice" in parsed["message"]


# ============================================================================
# log_data_access
# ============================================================================


class TestLogDataAccess:
    """Tests for log_data_access method."""

    def test_logs_data_access(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_data_access("conversation", "conv_123", "user1", "read")
        parsed = json.loads(records[0].getMessage())
        assert parsed["event_type"] == "data_access"
        assert parsed["severity"] == "INFO"
        assert parsed["resource_type"] == "conversation"
        assert parsed["resource_id"] == "conv_123"
        assert parsed["user_id"] == "user1"
        assert parsed["action"] == "read"

    def test_message_includes_action(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_data_access("system", "sys_1", "admin", "delete")
        parsed = json.loads(records[0].getMessage())
        assert "delete" in parsed["message"]
        assert "system" in parsed["message"]


# ============================================================================
# log_schema_validation_failure
# ============================================================================


class TestLogSchemaValidationFailure:
    """Tests for log_schema_validation_failure method."""

    def test_logs_validation_failure(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_schema_validation_failure("manifest", ["field X required"], "upload")
        parsed = json.loads(records[0].getMessage())
        assert parsed["event_type"] == "schema_validation_failure"
        assert parsed["severity"] == "LOW"
        assert parsed["schema_type"] == "manifest"
        assert parsed["errors"] == ["field X required"]
        assert parsed["error_count"] == 1
        assert parsed["source"] == "upload"

    def test_limits_errors_to_five(self, sec_logger):
        logger, records = sec_logger
        errors = [f"error_{i}" for i in range(10)]
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_schema_validation_failure("upload_id", errors, "api")
        parsed = json.loads(records[0].getMessage())
        assert len(parsed["errors"]) == 5
        assert parsed["error_count"] == 10  # Total count preserved


# ============================================================================
# log_ingestion_event
# ============================================================================


class TestLogIngestionEvent:
    """Tests for log_ingestion_event method."""

    def test_logs_info_for_normal_event(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_ingestion_event("started", "sys1", "upl1")
        parsed = json.loads(records[0].getMessage())
        assert parsed["event_type"] == "ingestion_started"
        assert parsed["severity"] == "INFO"
        assert parsed["system_id"] == "sys1"
        assert parsed["upload_id"] == "upl1"

    def test_logs_medium_for_error_event(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_ingestion_event("error_occurred", "sys1", "upl1")
        parsed = json.loads(records[0].getMessage())
        assert parsed["severity"] == "MEDIUM"

    def test_logs_medium_for_failure_event(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_ingestion_event("processing_failure", "sys1", "upl1")
        parsed = json.loads(records[0].getMessage())
        assert parsed["severity"] == "MEDIUM"

    def test_includes_extra_details(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_ingestion_event("completed", "sys1", "upl1", size_bytes=1024, duration_ms=50)
        parsed = json.loads(records[0].getMessage())
        assert parsed["size_bytes"] == 1024
        assert parsed["duration_ms"] == 50

    def test_message_includes_system_and_upload(self, sec_logger):
        logger, records = sec_logger
        with patch("akosha.observability.security_logging.record_counter"):
            logger.log_ingestion_event("completed", "my_sys", "my_upl")
        parsed = json.loads(records[0].getMessage())
        assert "my_sys" in parsed["message"]
        assert "my_upl" in parsed["message"]


# ============================================================================
# get_security_logger singleton
# ============================================================================


class TestGetSecurityLogger:
    """Tests for get_security_logger singleton."""

    def test_returns_security_logger(self):
        logger = get_security_logger()
        assert isinstance(logger, SecurityLogger)

    def test_returns_same_instance(self):
        l1 = get_security_logger()
        l2 = get_security_logger()
        assert l1 is l2

    def test_instance_uses_security_logger_name(self):
        logger = get_security_logger()
        assert logger.logger.name == "security"


# ============================================================================
# __all__ exports
# ============================================================================


class TestExports:
    """Tests for module exports."""

    def test_security_logger_exported(self):
        from akosha.observability.security_logging import SecurityLogger

        assert SecurityLogger is not None

    def test_get_security_logger_exported(self):
        from akosha.observability.security_logging import get_security_logger

        assert callable(get_security_logger)
