"""Tests for Akosha real-time alerting system."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from akosha.alerting import (
    Alert,
    AlertDeduplicator,
    AlertManager,
    AlertRouter,
    AlertSeverity,
    AlertType,
    PatternDetector,
    check_metric_and_alert,
    configure_alert_threshold,
    register_alert_webhook,
    send_alert,
)


class TestAlert:
    """Test Alert dataclass."""

    def test_alert_creation(self):
        """Test creating an alert with default values."""
        alert = Alert()

        assert alert.alert_type == AlertType.ANOMALY_DETECTED
        assert alert.severity == AlertSeverity.INFO
        assert alert.message == ""
        assert alert.metadata == {}
        assert alert.pattern_data == {}
        assert alert.webhook_urls == []
        assert isinstance(alert.id, str)
        assert len(alert.id) > 0

    def test_alert_with_values(self):
        """Test creating an alert with specific values."""
        now = datetime.now(UTC)
        alert = Alert(
            alert_type=AlertType.HIGH_LATENCY,
            severity=AlertSeverity.WARNING,
            message="High latency detected",
            metadata={"service": "api"},
            pattern_data={"latency_ms": 1500},
            timestamp=now,
        )

        assert alert.alert_type == AlertType.HIGH_LATENCY
        assert alert.severity == AlertSeverity.WARNING
        assert alert.message == "High latency detected"
        assert alert.metadata == {"service": "api"}
        assert alert.pattern_data == {"latency_ms": 1500}
        assert alert.timestamp == now

    def test_to_dict(self):
        """Test converting alert to dictionary for webhook transmission."""
        now = datetime.now(UTC)
        alert = Alert(
            id="test-alert-123",
            alert_type=AlertType.HIGH_LATENCY,
            severity=AlertSeverity.WARNING,
            message="Test alert",
            metadata={"key": "value"},
            pattern_data={"metric": 123.45},
            timestamp=now,
        )

        result = alert.to_dict()

        assert result["id"] == "test-alert-123"
        assert result["alert_type"] == "high_latency"
        assert result["severity"] == "warning"
        assert result["message"] == "Test alert"
        assert result["metadata"] == {"key": "value"}
        assert result["pattern_data"] == {"metric": 123.45}
        assert "timestamp" in result


class TestAlertRouter:
    """Test alert routing to webhooks."""

    @pytest.fixture
    def router(self):
        """Create AlertRouter instance."""
        return AlertRouter()

    def test_register_webhook(self, router):
        """Test registering webhook URL for alert type."""
        router.register_webhook(AlertType.HIGH_LATENCY, "http://example.com/webhook")

        webhooks = router.get_webhooks(AlertType.HIGH_LATENCY)
        assert len(webhooks) == 1
        assert webhooks[0] == "http://example.com/webhook"

    def test_register_multiple_webhooks(self, router):
        """Test registering multiple webhooks for same alert type."""
        router.register_webhook(AlertType.HIGH_LATENCY, "http://example.com/webhook1")
        router.register_webhook(AlertType.HIGH_LATENCY, "http://example.com/webhook2")

        webhooks = router.get_webhooks(AlertType.HIGH_LATENCY)
        assert len(webhooks) == 2
        assert "http://example.com/webhook1" in webhooks
        assert "http://example.com/webhook2" in webhooks

    def test_get_webhooks_empty(self, router):
        """Test getting webhooks for alert type with no registered webhooks."""
        webhooks = router.get_webhooks(AlertType.HIGH_LATENCY)
        assert webhooks == []

    def test_get_webhooks_different_types(self, router):
        """Test that webhooks are segregated by alert type."""
        router.register_webhook(AlertType.HIGH_LATENCY, "http://example.com/latency")
        router.register_webhook(AlertType.LOW_HIT_RATE, "http://example.com/hitrate")

        latency_webhooks = router.get_webhooks(AlertType.HIGH_LATENCY)
        hitrate_webhooks = router.get_webhooks(AlertType.LOW_HIT_RATE)

        assert len(latency_webhooks) == 1
        assert latency_webhooks[0] == "http://example.com/latency"
        assert len(hitrate_webhooks) == 1
        assert hitrate_webhooks[0] == "http://example.com/hitrate"


class TestAlertDeduplicator:
    """Test alert deduplication to prevent spam."""

    @pytest.fixture
    def deduplicator(self):
        """Create AlertDeduplicator instance."""
        return AlertDeduplicator(window_minutes=5)

    def test_should_send_first_time(self, deduplicator):
        """Test that alert is sent first time."""
        alert = Alert(
            alert_type=AlertType.HIGH_LATENCY,
            pattern_data={"latency_ms": 1500},
        )

        assert deduplicator.should_send(alert) is True

    def test_should_not_send_duplicate(self, deduplicator):
        """Test that duplicate alert is not sent within window."""
        alert = Alert(
            alert_type=AlertType.HIGH_LATENCY,
            pattern_data={"latency_ms": 1500},
        )

        # First time should send
        assert deduplicator.should_send(alert) is True

        # Second time should not send (within window)
        assert deduplicator.should_send(alert) is False

    def test_should_send_after_window(self, deduplicator):
        """Test that alert is sent again after deduplication window."""
        alert = Alert(
            alert_type=AlertType.HIGH_LATENCY,
            pattern_data={"latency_ms": 1500},
        )

        # First time
        assert deduplicator.should_send(alert) is True

        # Simulate time passing beyond window
        old_time = datetime.now(UTC) - timedelta(minutes=10)
        deduplicator._sent_alerts[
            f"{alert.alert_type.value}:{hash(str(alert.pattern_data))}"
        ] = old_time

        # Should send again
        assert deduplicator.should_send(alert) is True

    def test_different_pattern_data_sends(self, deduplicator):
        """Test that alerts with different pattern data are sent."""
        alert1 = Alert(
            alert_type=AlertType.HIGH_LATENCY,
            pattern_data={"latency_ms": 1500},
        )

        alert2 = Alert(
            alert_type=AlertType.HIGH_LATENCY,
            pattern_data={"latency_ms": 2000},
        )

        # Both should send (different pattern data)
        assert deduplicator.should_send(alert1) is True
        assert deduplicator.should_send(alert2) is True

    def test_cleanup_old_entries(self, deduplicator):
        """Test that old entries are cleaned up."""
        alert = Alert(
            alert_type=AlertType.HIGH_LATENCY,
            pattern_data={"latency_ms": 1500},
        )

        # Mark as sent
        deduplicator.should_send(alert)

        # Manually add old entry
        old_key = "old_alert_key"
        deduplicator._sent_alerts[old_key] = datetime.now(UTC) - timedelta(minutes=20)

        # Trigger cleanup
        deduplicator._cleanup_old_entries()

        # Old entry should be removed
        assert old_key not in deduplicator._sent_alerts


class TestPatternDetector:
    """Test pattern detection with thresholds."""

    @pytest.fixture
    def detector(self):
        """Create PatternDetector instance."""
        return PatternDetector()

    def test_default_thresholds(self, detector):
        """Test default threshold values."""
        assert detector.thresholds[AlertType.HIGH_LATENCY] == 1000.0
        assert detector.thresholds[AlertType.LOW_HIT_RATE] == 0.5
        assert detector.thresholds[AlertType.SPIKE_IN_ERRORS] == 10.0
        assert detector.thresholds[AlertType.ANOMALY_DETECTED] == 2.0

    def test_set_threshold(self, detector):
        """Test setting custom threshold."""
        detector.set_threshold(AlertType.HIGH_LATENCY, 2000.0)
        assert detector.thresholds[AlertType.HIGH_LATENCY] == 2000.0

    def test_check_threshold_exceeded(self, detector):
        """Test checking threshold when value exceeds."""
        alert = detector.check_threshold(
            AlertType.HIGH_LATENCY,
            1500.0,
            metadata={"service": "api"},
        )

        assert alert is not None
        assert alert.alert_type == AlertType.HIGH_LATENCY
        assert alert.severity == AlertSeverity.WARNING
        assert "latency" in alert.message.lower()
        assert alert.pattern_data["threshold"] == 1000.0
        assert alert.pattern_data["actual_value"] == 1500.0

    def test_check_threshold_not_exceeded(self, detector):
        """Test checking threshold when value is below."""
        alert = detector.check_threshold(
            AlertType.HIGH_LATENCY,
            500.0,
        )

        assert alert is None

    def test_check_threshold_low_hit_rate(self, detector):
        """Test threshold check for low hit rate (inverted logic)."""
        # Hit rate below threshold should trigger
        alert = detector.check_threshold(
            AlertType.LOW_HIT_RATE,
            0.3,  # 30% (below 50% threshold)
        )

        assert alert is not None
        assert alert.alert_type == AlertType.LOW_HIT_RATE
        assert "hit rate" in alert.message.lower()

    def test_check_threshold_no_threshold_set(self, detector):
        """Test checking threshold for alert type with no threshold set."""
        alert = detector.check_threshold(
            AlertType.TREND_CHANGE,
            100.0,
        )

        assert alert is None

    def test_alert_message_formatting(self, detector):
        """Test that alert messages are properly formatted."""
        alert = detector.check_threshold(
            AlertType.HIGH_LATENCY,
            1234.56,
        )

        assert "1234.56ms" in alert.message
        assert "1000.00ms" in alert.message


class TestAlertManager:
    """Test main alert management system."""

    @pytest.fixture
    def manager(self):
        """Create AlertManager instance."""
        return AlertManager()

    def test_manager_initialization(self, manager):
        """Test that manager initializes components."""
        assert manager.router is not None
        assert manager.deduplicator is not None
        assert manager.detector is not None

    def test_register_webhook(self, manager):
        """Test registering webhook through manager."""
        manager.register_webhook(
            AlertType.HIGH_LATENCY,
            "http://example.com/webhook",
        )

        webhooks = manager.router.get_webhooks(AlertType.HIGH_LATENCY)
        assert len(webhooks) == 1

    def test_set_threshold(self, manager):
        """Test setting threshold through manager."""
        manager.set_threshold(AlertType.HIGH_LATENCY, 2000.0)

        assert manager.detector.thresholds[AlertType.HIGH_LATENCY] == 2000.0

    @pytest.mark.asyncio
    async def test_send_alert_no_webhooks(self, manager):
        """Test sending alert with no registered webhooks."""
        alert = Alert(
            alert_type=AlertType.HIGH_LATENCY,
            message="Test alert",
        )

        result = await manager.send_alert(alert)

        assert result["status"] == "no_webhooks"
        assert "alert_id" in result

    @pytest.mark.asyncio
    async def test_send_alert_deduplicated(self, manager):
        """Test that duplicate alerts are not sent."""
        manager.register_webhook(AlertType.HIGH_LATENCY, "http://example.com/webhook")

        alert = Alert(
            alert_type=AlertType.HIGH_LATENCY,
            message="Test alert",
            pattern_data={"key": "value"},
        )

        # First send
        result1 = await manager.send_alert(alert)
        assert result1["status"] == "complete"

        # Second send (deduplicated)
        result2 = await manager.send_alert(alert)
        assert result2["status"] == "deduplicated"

    @pytest.mark.asyncio
    async def test_send_alert_success(self, manager):
        """Test successful alert sending to webhook."""
        manager.register_webhook(AlertType.HIGH_LATENCY, "http://example.com/webhook")

        alert = Alert(
            alert_type=AlertType.HIGH_LATENCY,
            message="Test alert",
        )

        # Mock HTTP client
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await manager.send_alert(alert)

        assert result["status"] == "complete"
        assert result["webhooks_notified"] == 1
        assert len(result["results"]) == 1
        assert result["results"][0]["status"] == "sent"

    @pytest.mark.asyncio
    async def test_send_alert_failure(self, manager):
        """Test handling webhook failure."""
        manager.register_webhook(AlertType.HIGH_LATENCY, "http://example.com/webhook")

        alert = Alert(
            alert_type=AlertType.HIGH_LATENCY,
            message="Test alert",
        )

        # Mock HTTP client with error
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.HTTPError("Connection failed")

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await manager.send_alert(alert)

        assert result["status"] == "complete"
        assert result["webhooks_notified"] == 0
        assert result["results"][0]["status"] == "failed"
        assert "error" in result["results"][0]

    @pytest.mark.asyncio
    async def test_check_and_alert_threshold_not_exceeded(self, manager):
        """Test check_and_alert when threshold not exceeded."""
        result = await manager.check_and_alert(
            AlertType.HIGH_LATENCY,
            500.0,  # Below threshold
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_check_and_alert_threshold_exceeded(self, manager):
        """Test check_and_alert when threshold exceeded."""
        manager.register_webhook(AlertType.HIGH_LATENCY, "http://example.com/webhook")

        # Mock successful webhook send
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await manager.check_and_alert(
                AlertType.HIGH_LATENCY,
                1500.0,  # Above threshold
                metadata={"service": "api"},
            )

        assert result is not None
        assert result["status"] == "complete"
        assert result["webhooks_notified"] == 1


class TestConvenienceFunctions:
    """Test convenience functions for alert operations."""

    @pytest.mark.asyncio
    async def test_send_alert_convenience(self):
        """Test send_alert convenience function."""
        # Mock webhook
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        # Register webhook
        manager = AlertManager()
        manager.register_webhook(AlertType.HIGH_LATENCY, "http://example.com/webhook")

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await send_alert(
                alert_type=AlertType.HIGH_LATENCY,
                message="Test alert",
                severity=AlertSeverity.WARNING,
                metadata={"service": "api"},
                pattern_data={"latency_ms": 1500},
            )

        assert result["status"] == "complete"

    @pytest.mark.asyncio
    async def test_check_metric_and_alert_convenience(self):
        """Test check_metric_and_alert convenience function."""
        # Mock webhook
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        # Register webhook
        manager = AlertManager()
        manager.register_webhook(AlertType.HIGH_LATENCY, "http://example.com/webhook")

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await check_metric_and_alert(
                alert_type=AlertType.HIGH_LATENCY,
                value=1500.0,
                metadata={"service": "api"},
            )

        assert result is not None
        assert result["status"] == "complete"

    def test_configure_alert_threshold_convenience(self):
        """Test configure_alert_threshold convenience function."""
        configure_alert_threshold(AlertType.HIGH_LATENCY, 2000.0)

        manager = AlertManager()
        # Check threshold was set globally
        assert manager.detector.thresholds[AlertType.HIGH_LATENCY] == 2000.0

    def test_register_alert_webhook_convenience(self):
        """Test register_alert_webhook convenience function."""
        register_alert_webhook(
            AlertType.HIGH_LATENCY,
            "http://example.com/webhook",
        )

        manager = AlertManager()
        webhooks = manager.router.get_webhooks(AlertType.HIGH_LATENCY)
        assert len(webhooks) == 1
