"""Real-time alerting system for Akosha analytics.

This module provides webhook-based alert notifications when patterns are detected
in the aggregated memory system, enabling proactive monitoring and response.

Features:
- Webhook-based alert notifications
- Pattern detection thresholds
- Alert routing and deduplication
- Multiple pattern types supported
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from logging import INFO as LOG_LEVEL
from typing import Any

import httpx
from pydantic import BaseModel, Field, HttpUrl


def get_logger():
    """Get a logger instance."""
    import logging
    return logging.getLogger(__name__)

logger = get_logger()
logger.setLevel(LOG_LEVEL)


class AlertSeverity(str, Enum):
    """Alert severity levels."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AlertType(str, Enum):
    """Types of patterns that can trigger alerts."""

    # Anomaly detection
    ANOMALY_DETECTED = "anomaly_detected"
    SPIKE_IN_ERRORS = "spike_in_errors"
    UNUSUAL_TRAFFIC = "unusual_traffic"

    # Trend analysis
    TREND_CHANGE = "trend_change"
    PREDICTION_THRESHOLD = "prediction_threshold"

    # System health
    HIGH_LATENCY = "high_latency"
    LOW_HIT_RATE = "low_hit_rate"
    STORAGE_CAPACITY = "storage_capacity"

    # Knowledge graph
    NEW_ENTITY_DETECTED = "new_entity_detected"
    RELATIONSHIP_CHANGE = "relationship_change"


@dataclass
class Alert:
    """Represents an alert notification."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    alert_type: AlertType = AlertType.ANOMALY_DETECTED
    severity: AlertSeverity = AlertSeverity.INFO
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    pattern_data: dict[str, Any] = field(default_factory=dict)
    webhook_urls: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for webhook transmission."""
        return {
            "id": self.id,
            "alert_type": self.alert_type.value,
            "severity": self.severity.value,
            "message": self.message,
            "metadata": self.metadata,
            "timestamp": self.timestamp.isoformat(),
            "pattern_data": self.pattern_data,
        }


class AlertRouter:
    """Routes alerts to appropriate webhook endpoints."""

    def __init__(self):
        """Initialize alert router."""
        self._routes: dict[AlertType, list[str]] = {}

    def register_webhook(self, alert_type: AlertType, webhook_url: str) -> None:
        """Register webhook URL for an alert type.

        Args:
            alert_type: Type of alert to route
            webhook_url: Webhook endpoint to call
        """
        if alert_type not in self._routes:
            self._routes[alert_type] = []
        self._routes[alert_type].append(webhook_url)
        logger.info(f"Registered webhook for {alert_type.value}: {webhook_url}")

    def get_webhooks(self, alert_type: AlertType) -> list[str]:
        """Get registered webhooks for an alert type.

        Args:
            alert_type: Type of alert

        Returns:
            List of webhook URLs
        """
        return self._routes.get(alert_type, [])


class AlertDeduplicator:
    """Prevents duplicate alerts from being sent.

    Deduplication window: 5 minutes
    """

    def __init__(self, window_minutes: int = 5):
        """Initialize deduplicator.

        Args:
            window_minutes: Deduplication window in minutes
        """
        self.window_minutes = window_minutes
        self._sent_alerts: dict[str, datetime] = {}

    def should_send(self, alert: Alert) -> bool:
        """Check if alert should be sent (not recently sent).

        Args:
            alert: Alert to check

        Returns:
            True if alert should be sent, False if recently sent
        """
        # Create deduplication key
        key = f"{alert.alert_type.value}:{hash(str(alert.pattern_data))}"

        # Check if recently sent
        if key in self._sent_alerts:
            last_sent = self._sent_alerts[key]
            age = (datetime.now(UTC) - last_sent).total_seconds()

            # Skip if sent within deduplication window
            if age < self.window_minutes * 60:
                logger.debug(f"Alert {key} sent {age}s ago, skipping")
                return False

        # Mark as sent
        self._sent_alerts[key] = datetime.now(UTC)

        # Clean old entries
        self._cleanup_old_entries()

        return True

    def _cleanup_old_entries(self) -> None:
        """Remove old entries from sent alerts tracking."""
        cutoff = datetime.now(UTC) - timedelta(minutes=self.window_minutes * 2)

        old_keys = [
            key
            for key, timestamp in self._sent_alerts.items()
            if timestamp < cutoff
        ]

        for key in old_keys:
            del self._sent_alerts[key]

        if old_keys:
            logger.debug(f"Cleaned up {len(old_keys)} old alert entries")


class PatternDetector:
    """Detects patterns in analytics data that should trigger alerts."""

    def __init__(self):
        """Initialize pattern detector with default thresholds."""
        self.thresholds = {
            AlertType.HIGH_LATENCY: 1000.0,  # milliseconds
            AlertType.LOW_HIT_RATE: 0.5,  # 50% hit rate
            AlertType.SPIKE_IN_ERRORS: 10.0,  # 10x error rate
            AlertType.ANOMALY_DETECTED: 2.0,  # 2 standard deviations
        }

    def set_threshold(self, alert_type: AlertType, threshold: float) -> None:
        """Set threshold for an alert type.

        Args:
            alert_type: Type of alert
            threshold: Threshold value
        """
        self.thresholds[alert_type] = threshold
        logger.info(f"Set {alert_type.value} threshold to {threshold}")

    def check_threshold(
        self, alert_type: AlertType, value: float, metadata: dict[str, Any] | None = None
    ) -> Alert | None:
        """Check if value exceeds threshold and create alert.

        Args:
            alert_type: Type of alert to check
            value: Current metric value
            metadata: Additional context about the alert

        Returns:
            Alert if threshold exceeded, None otherwise
        """
        threshold = self.thresholds.get(alert_type)

        if threshold is None:
            return None

        # Check if threshold exceeded
        triggered = value > threshold if alert_type != AlertType.LOW_HIT_RATE else value < threshold

        if triggered:
            return Alert(
                alert_type=alert_type,
                severity=self._get_severity(alert_type),
                message=self._format_message(alert_type, value, threshold),
                metadata=metadata or {},
                pattern_data={"threshold": threshold, "actual_value": value},
            )

        return None

    def _get_severity(self, alert_type: AlertType) -> AlertSeverity:
        """Get severity level for an alert type.

        Args:
            alert_type: Type of alert

        Returns:
            Severity level
        """
        severity_map = {
            AlertType.CRITICAL: AlertSeverity.CRITICAL,
            AlertType.HIGH_LATENCY: AlertSeverity.WARNING,
            AlertType.LOW_HIT_RATE: AlertSeverity.WARNING,
            AlertType.SPIKE_IN_ERRORS: AlertSeverity.ERROR,
            AlertType.ANOMALY_DETECTED: AlertSeverity.WARNING,
        }

        return severity_map.get(alert_type, AlertSeverity.INFO)

    def _format_message(
        self, alert_type: AlertType, value: float, threshold: float
    ) -> str:
        """Format alert message.

        Args:
            alert_type: Type of alert
            value: Current metric value
            threshold: Threshold value

        Returns:
            Formatted message
        """
        if alert_type == AlertType.HIGH_LATENCY:
            return f"High latency detected: {value:.2f}ms (threshold: {threshold:.2f}ms)"
        elif alert_type == AlertType.LOW_HIT_RATE:
            return f"Low cache hit rate: {value:.1%} (threshold: {threshold:.1%})"
        elif alert_type == AlertType.SPIKE_IN_ERRORS:
            return f"Error rate spike: {value:.1f}x (threshold: {threshold:.1f}x)"
        elif alert_type == AlertType.ANOMALY_DETECTED:
            return f"Anomaly detected: {value:.2f}σ (threshold: {threshold:.2f}σ)"
        else:
            return f"Alert triggered: {alert_type.value} (value: {value}, threshold: {threshold})"


class AlertManager:
    """Main alert management system.

    Coordinates pattern detection, deduplication, routing, and webhook delivery.
    """

    def __init__(self):
        """Initialize alert manager."""
        self.router = AlertRouter()
        self.deduplicator = AlertDeduplicator()
        self.detector = PatternDetector()
        self._webhook_timeout = 10.0  # seconds
        logger.info("AlertManager initialized")

    def register_webhook(self, alert_type: AlertType, webhook_url: str) -> None:
        """Register webhook URL for alerts.

        Args:
            alert_type: Type of alert to route
            webhook_url: Webhook endpoint to call
        """
        self.router.register_webhook(alert_type, webhook_url)

    def set_threshold(self, alert_type: AlertType, threshold: float) -> None:
        """Configure threshold for alert type.

        Args:
            alert_type: Type of alert
            threshold: Threshold value
        """
        self.detector.set_threshold(alert_type, threshold)

    async def send_alert(self, alert: Alert) -> dict[str, Any]:
        """Send alert to registered webhooks.

        Args:
            alert: Alert to send

        Returns:
            Dictionary with send results
        """
        # Get webhooks for this alert type
        webhook_urls = self.router.get_webhooks(alert.alert_type)

        if not webhook_urls:
            logger.debug(f"No webhooks registered for {alert.alert_type.value}")
            return {"status": "no_webhooks", "alert_id": alert.id}

        # Check deduplication
        if not self.deduplicator.should_send(alert):
            return {"status": "deduplicated", "alert_id": alert.id}

        # Send to all webhooks
        results = []
        async with httpx.AsyncClient(timeout=self._webhook_timeout) as client:
            for url in webhook_urls:
                try:
                    response = await client.post(
                        url,
                        json=alert.to_dict(),
                        timeout=5.0,
                    )
                    response.raise_for_status()

                    results.append({
                        "url": url,
                        "status": "sent",
                        "status_code": response.status_code,
                    })

                    logger.info(f"Alert {alert.id} sent to {url}")

                except httpx.HTTPError as e:
                    logger.error(f"Failed to send alert to {url}: {e}")
                    results.append({
                        "url": url,
                        "status": "failed",
                        "error": str(e),
                    })

        return {
            "status": "complete",
            "alert_id": alert.id,
            "results": results,
            "webhooks_notified": len([r for r in results if r["status"] == "sent"]),
        }

    async def check_and_alert(
        self,
        alert_type: AlertType,
        value: float,
        metadata: dict[str, Any] | None = None,
        webhook_urls: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Check threshold, create alert, and send to webhooks.

        This is the main entry point for alerting.

        Args:
            alert_type: Type of alert
            value: Current metric value
            metadata: Additional context
            webhook_urls: Optional override of registered webhooks

        Returns:
            Send results if alert triggered, None otherwise
        """
        # Check if alert should be triggered
        alert = self.detector.check_threshold(alert_type, value, metadata)

        if alert is None:
            return None

        # Add webhook URLs if provided
        if webhook_urls:
            alert.webhook_urls = webhook_urls

        # Send alert
        return await self.send_alert(alert)


# Global alert manager instance
_alert_manager = AlertManager()


def get_alert_manager() -> AlertManager:
    """Get global alert manager instance.

    Returns:
        Global AlertManager singleton
    """
    return _alert_manager


# Convenience functions for common alert operations

async def send_alert(
    alert_type: AlertType,
    message: str,
    severity: AlertSeverity = AlertSeverity.INFO,
    metadata: dict[str, Any] | None = None,
    pattern_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send an alert to registered webhooks.

    Args:
        alert_type: Type of alert
        message: Alert message
        severity: Alert severity level
        metadata: Additional context
        pattern_data: Pattern detection data

    Returns:
        Send results
    """
    alert = Alert(
        alert_type=alert_type,
        severity=severity,
        message=message,
        metadata=metadata or {},
        pattern_data=pattern_data or {},
    )

    manager = get_alert_manager()
    return await manager.send_alert(alert)


async def check_metric_and_alert(
    alert_type: AlertType,
    value: float,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Check metric against threshold and send alert if needed.

    Args:
        alert_type: Type of alert
        value: Metric value
        metadata: Additional context

    Returns:
        Send results if alert triggered, None otherwise
    """
    manager = get_alert_manager()
    return await manager.check_and_alert(alert_type, value, metadata)


def configure_alert_threshold(alert_type: AlertType, threshold: float) -> None:
    """Configure threshold for an alert type.

    Args:
        alert_type: Type of alert
        threshold: Threshold value
    """
    manager = get_alert_manager()
    manager.set_threshold(alert_type, threshold)


def register_alert_webhook(alert_type: AlertType, webhook_url: str) -> None:
    """Register webhook URL for alerts.

    Args:
        alert_type: Type of alert
        webhook_url: Webhook endpoint to call
    """
    manager = get_alert_manager()
    manager.register_webhook(alert_type, webhook_url)
