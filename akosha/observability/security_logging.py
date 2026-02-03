"""Structured security logging for SIEM integration.

Provides centralized security event logging with JSON formatting
for integration with Security Information and Event Management (SIEM) systems.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from akosha.observability import record_counter

logger = logging.getLogger(__name__)


class SecurityLogger:
    """Structured security event logger.

    Logs security-relevant events in JSON format for SIEM consumption.
    All events include severity, timestamp, and structured context.
    """

    # Severity levels for security events
    SEVERITY_CRITICAL = "CRITICAL"
    SEVERITY_HIGH = "HIGH"
    SEVERITY_MEDIUM = "MEDIUM"
    SEVERITY_LOW = "LOW"
    SEVERITY_INFO = "INFO"

    def __init__(self, logger: logging.Logger | None = None) -> None:
        """Initialize security logger.

        Args:
            logger: Logger instance (defaults to 'security' logger)
        """
        self.logger = logger or logging.getLogger("security")

    def _log_event(
        self,
        event_type: str,
        severity: str,
        message: str,
        **context: Any,
    ) -> None:
        """Log security event in structured JSON format.

        Args:
            event_type: Type of security event
            severity: Severity level (CRITICAL, HIGH, MEDIUM, LOW, INFO)
            message: Human-readable message
            **context: Additional event context
        """
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "severity": severity,
            "event_type": event_type,
            "message": message,
            **context,
        }

        # Log as JSON for SIEM parsing
        self.logger.info(json.dumps(event))

        # Record metrics
        record_counter(
            f"security.{event_type}",
            1,
            {"severity": severity},
        )

    def log_auth_success(
        self,
        user_id: str,
        source_ip: str,
        method: str = "jwt",
    ) -> None:
        """Log successful authentication.

        Args:
            user_id: Authenticated user ID
            source_ip: Source IP address
            method: Auth method (jwt, api_token, etc.)
        """
        self._log_event(
            event_type="auth_success",
            severity=self.SEVERITY_INFO,
            message=f"Successful authentication for user {user_id}",
            user_id=user_id,
            source_ip=source_ip,
            auth_method=method,
        )

    def log_auth_failure(
        self,
        reason: str,
        source_ip: str,
        user_id: str | None = None,
    ) -> None:
        """Log failed authentication.

        Args:
            reason: Failure reason
            source_ip: Source IP address
            user_id: User ID if provided (None otherwise)
        """
        self._log_event(
            event_type="auth_failure",
            severity=self.SEVERITY_MEDIUM,
            message=f"Authentication failed: {reason}",
            source_ip=source_ip,
            user_id=user_id,
            reason=reason,
        )

    def log_rate_limit_exceeded(
        self,
        user_id: str,
        requested_tokens: int,
        available_tokens: float,
    ) -> None:
        """Log rate limit violation.

        Args:
            user_id: User ID that exceeded limit
            requested_tokens: Number of tokens requested
            available_tokens: Tokens available
        """
        self._log_event(
            event_type="rate_limit_exceeded",
            severity=self.SEVERITY_MEDIUM,
            message=f"Rate limit exceeded for user {user_id}",
            user_id=user_id,
            requested_tokens=requested_tokens,
            available_tokens=available_tokens,
        )

    def log_sql_injection_attempt(
        self,
        query: str,
        source_ip: str,
        user_id: str | None = None,
    ) -> None:
        """Log SQL injection attack attempt.

        Args:
            query: Suspicious query string
            source_ip: Source IP address
            user_id: User ID if authenticated
        """
        # Sanitize query for logging (truncate if too long)
        safe_query = query[:500] if len(query) > 500 else query

        self._log_event(
            event_type="sql_injection_attempt",
            severity=self.SEVERITY_HIGH,
            message="SQL injection attempt detected",
            source_ip=source_ip,
            user_id=user_id,
            query=safe_query,
        )

    def log_path_traversal_attempt(
        self,
        path: str,
        source_ip: str,
        user_id: str | None = None,
    ) -> None:
        """Log path traversal attack attempt.

        Args:
            path: Suspicious path string
            source_ip: Source IP address
            user_id: User ID if authenticated
        """
        self._log_event(
            event_type="path_traversal_attempt",
            severity=self.SEVERITY_HIGH,
            message="Path traversal attempt detected",
            source_ip=source_ip,
            user_id=user_id,
            path=path[:500],  # Truncate if too long
        )

    def log_mcp_tool_access(
        self,
        tool_name: str,
        user_id: str,
        source_ip: str,
        success: bool,
    ) -> None:
        """Log MCP tool access for audit trail.

        Args:
            tool_name: Name of MCP tool accessed
            user_id: User ID accessing tool
            source_ip: Source IP address
            success: Whether access was successful
        """
        self._log_event(
            event_type="mcp_tool_access",
            severity=self.SEVERITY_INFO,
            message=f"MCP tool {tool_name} accessed by {user_id}",
            tool_name=tool_name,
            user_id=user_id,
            source_ip=source_ip,
            success=success,
        )

    def log_data_access(
        self,
        resource_type: str,
        resource_id: str,
        user_id: str,
        action: str,
    ) -> None:
        """Log data access for audit trail.

        Args:
            resource_type: Type of resource (conversation, system, etc.)
            resource_id: Resource identifier
            user_id: User accessing data
            action: Action performed (read, write, delete)
        """
        self._log_event(
            event_type="data_access",
            severity=self.SEVERITY_INFO,
            message=f"{action} {resource_type} {resource_id}",
            resource_type=resource_type,
            resource_id=resource_id,
            user_id=user_id,
            action=action,
        )

    def log_schema_validation_failure(
        self,
        schema_type: str,
        errors: list[str],
        source: str,
    ) -> None:
        """Log schema validation failure.

        Args:
            schema_type: Type of schema (manifest, upload_id, etc.)
            errors: List of validation errors
            source: Source of validation failure
        """
        self._log_event(
            event_type="schema_validation_failure",
            severity=self.SEVERITY_LOW,
            message=f"Schema validation failed for {schema_type}",
            schema_type=schema_type,
            errors=errors[:5],  # Limit to first 5 errors
            error_count=len(errors),
            source=source,
        )

    def log_ingestion_event(
        self,
        event_type: str,
        system_id: str,
        upload_id: str,
        **details: Any,
    ) -> None:
        """Log ingestion-related event.

        Args:
            event_type: Type of ingestion event
            system_id: System identifier
            upload_id: Upload identifier
            **details: Additional event details
        """
        severity = self.SEVERITY_INFO
        if "error" in event_type.lower() or "failure" in event_type.lower():
            severity = self.SEVERITY_MEDIUM

        self._log_event(
            event_type=f"ingestion_{event_type}",
            severity=severity,
            message=f"Ingestion {event_type} for {system_id}/{upload_id}",
            system_id=system_id,
            upload_id=upload_id,
            **details,
        )


# Global security logger instance
_global_security_logger: SecurityLogger | None = None


def get_security_logger() -> SecurityLogger:
    """Get global security logger instance.

    Returns:
        SecurityLogger singleton
    """
    global _global_security_logger

    if _global_security_logger is None:
        _global_security_logger = SecurityLogger()

    return _global_security_logger


__all__ = [
    "SecurityLogger",
    "get_security_logger",
]
