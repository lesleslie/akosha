"""WebSocket server for Akosha pattern detection and analytics.

This module implements a WebSocket server that broadcasts real-time updates
about pattern detection, anomaly alerts, and analytics aggregation.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Optional

from mcp_common.websocket import (
    EventTypes,
    MessageType,
    WebSocketMessage,
    WebSocketProtocol,
    WebSocketServer,
)

# Import authentication
from akosha.websocket.auth import get_authenticator

# Import TLS configuration
from akosha.websocket.tls_config import load_ssl_context, get_websocket_tls_config

logger = logging.getLogger(__name__)


class AkoshaWebSocketServer(WebSocketServer):
    """WebSocket server for Akosha analytics and pattern detection.

    Broadcasts real-time events for:
    - Pattern detection (patterns discovered in data)
    - Anomaly detection (unusual behavior identified)
    - Insight generation (new insights from analytics)
    - Aggregation completion (batch processing finished)

    Channels:
    - patterns:{category} - Pattern detection updates by category
    - anomalies - Anomaly detection alerts
    - insights - Generated insights
    - metrics - Real-time analytics metrics

    Attributes:
        analytics_engine: AnalyticsEngine instance for data processing
        host: Server host address
        port: Server port number (default: 8692)

    Example:
        >>> from akosha.websocket import AkoshaWebSocketServer
        >>>
        >>> analytics = AnalyticsEngine()
        >>> server = AkoshaWebSocketServer(analytics_engine=analytics)
        >>> await server.start()

    With TLS:
        >>> server = AkoshaWebSocketServer(
        ...     analytics_engine=analytics,
        ...     cert_file="/path/to/cert.pem",
        ...     key_file="/path/to/key.pem"
        ... )
        >>> await server.start()

    With auto-generated development certificate:
        >>> server = AkoshaWebSocketServer(
        ...     analytics_engine=analytics,
        ...     tls_enabled=True
        ... )
        >>> await server.start()
    """

    def __init__(
        self,
        analytics_engine: Any,
        host: str = "127.0.0.1",
        port: int = 8692,
        max_connections: int = 1000,
        message_rate_limit: int = 100,
        require_auth: bool = False,
        cert_file: str | None = None,
        key_file: str | None = None,
        ca_file: str | None = None,
        tls_enabled: bool = False,
        verify_client: bool = False,
        auto_cert: bool = False,
    ):
        """Initialize Akosha WebSocket server.

        Args:
            analytics_engine: AnalyticsEngine instance for data processing
            host: Server host address (default: "127.0.0.1")
            port: Server port number (default: 8692)
            max_connections: Maximum concurrent connections (default: 1000)
            message_rate_limit: Messages per second per connection (default: 100)
            require_auth: Require JWT authentication for connections
            cert_file: Path to TLS certificate file (PEM format)
            key_file: Path to TLS private key file (PEM format)
            ca_file: Path to CA file for client verification
            tls_enabled: Enable TLS (generates self-signed cert if no cert provided)
            verify_client: Verify client certificates
            auto_cert: Auto-generate self-signed certificate for development
        """
        authenticator = get_authenticator()

        # Load TLS configuration if enabled
        ssl_context = None
        if tls_enabled or cert_file or key_file:
            tls_config = load_ssl_context(
                cert_file=cert_file,
                key_file=key_file,
                ca_file=ca_file,
                verify_client=verify_client,
            )
            ssl_context = tls_config["ssl_context"]

        # If TLS enabled but no context yet, check environment
        if tls_enabled and ssl_context is None:
            env_config = get_websocket_tls_config()
            if env_config["tls_enabled"] and env_config["cert_file"]:
                tls_config = load_ssl_context()
                ssl_context = tls_config["ssl_context"]

        super().__init__(
            host=host,
            port=port,
            max_connections=max_connections,
            message_rate_limit=message_rate_limit,
            authenticator=authenticator,
            require_auth=require_auth,
            ssl_context=ssl_context,
            cert_file=cert_file,
            key_file=key_file,
            ca_file=ca_file,
            tls_enabled=tls_enabled,
            verify_client=verify_client,
            auto_cert=auto_cert,
        )

        self.analytics_engine = analytics_engine
        logger.info(
            f"AkoshaWebSocketServer initialized: {host}:{port} "
            f"(TLS: {ssl_context is not None})"
        )

    async def on_connect(self, websocket: Any, connection_id: str) -> None:
        """Handle new WebSocket connection.

        Args:
            websocket: WebSocket connection object
            connection_id: Unique connection identifier
        """
        user = getattr(websocket, "user", None)
        user_id = user.get("user_id") if user else "anonymous"

        logger.info(f"Client connected: {connection_id} (user: {user_id})")

        # Send welcome message
        welcome = WebSocketProtocol.create_event(
            EventTypes.SESSION_CREATED,
            {
                "connection_id": connection_id,
                "server": "akosha",
                "message": "Connected to Akosha pattern detection",
                "authenticated": user is not None,
                "secure": self.ssl_context is not None,
            },
        )
        await websocket.send(WebSocketProtocol.encode(welcome))

    async def on_disconnect(self, websocket: Any, connection_id: str) -> None:
        """Handle WebSocket disconnection.

        Args:
            websocket: WebSocket connection object
            connection_id: Unique connection identifier
        """
        logger.info(f"Client disconnected: {connection_id}")
        await self.leave_all_rooms(connection_id)

    async def on_message(self, websocket: Any, message: WebSocketMessage) -> None:
        """Handle incoming WebSocket message.

        Args:
            websocket: WebSocket connection object
            message: Decoded message
        """
        if message.type == MessageType.REQUEST:
            await self._handle_request(websocket, message)
        elif message.type == MessageType.EVENT:
            await self._handle_event(websocket, message)
        else:
            logger.warning(f"Unhandled message type: {message.type}")

    async def _handle_request(
        self, websocket: Any, message: WebSocketMessage
    ) -> None:
        """Handle request message (expects response).

        Args:
            websocket: WebSocket connection object
            message: Request message
        """
        # Get authenticated user from connection
        user = getattr(websocket, "user", None)

        if message.event == "subscribe":
            channel = message.data.get("channel")

            # Check authorization for this channel
            if user and not self._can_subscribe_to_channel(user, channel):
                error = WebSocketProtocol.create_error(
                    error_code="FORBIDDEN",
                    error_message=f"Not authorized to subscribe to {channel}",
                    correlation_id=message.correlation_id,
                )
                await websocket.send(WebSocketProtocol.encode(error))
                return

            if channel:
                connection_id = getattr(websocket, "id", str(uuid.uuid4()))
                await self.join_room(channel, connection_id)

                response = WebSocketProtocol.create_response(
                    message,
                    {"status": "subscribed", "channel": channel}
                )
                await websocket.send(WebSocketProtocol.encode(response))

        elif message.event == "unsubscribe":
            channel = message.data.get("channel")
            if channel:
                connection_id = getattr(websocket, "id", str(uuid.uuid4()))
                await self.leave_room(channel, connection_id)

                response = WebSocketProtocol.create_response(
                    message,
                    {"status": "unsubscribed", "channel": channel}
                )
                await websocket.send(WebSocketProtocol.encode(response))

        elif message.event == "get_patterns":
            category = message.data.get("category")
            patterns = await self._get_patterns(category)
            response = WebSocketProtocol.create_response(message, patterns)
            await websocket.send(WebSocketProtocol.encode(response))

        elif message.event == "get_anomalies":
            anomalies = await self._get_anomalies()
            response = WebSocketProtocol.create_response(message, anomalies)
            await websocket.send(WebSocketProtocol.encode(response))

        else:
            # Unknown request
            error = WebSocketProtocol.create_error(
                error_code="UNKNOWN_REQUEST",
                error_message=f"Unknown request event: {message.event}",
                correlation_id=message.correlation_id,
            )
            await websocket.send(WebSocketProtocol.encode(error))

    async def _handle_event(self, websocket: Any, message: WebSocketMessage) -> None:
        """Handle event message (no response expected).

        Args:
            websocket: WebSocket connection object
            message: Event message
        """
        logger.debug(f"Received client event: {message.event}")

    def _can_subscribe_to_channel(self, user: dict[str, Any], channel: str) -> bool:
        """Check if user can subscribe to channel.

        Args:
            user: User payload from JWT
            channel: Channel name

        Returns:
            True if authorized, False otherwise
        """
        permissions = user.get("permissions", [])

        # Admin can subscribe to any channel
        if "akosha:admin" in permissions or "admin" in permissions:
            return True

        # Check channel-specific permissions
        if channel.startswith("pattern:"):
            return "akosha:read" in permissions

        if channel.startswith("anomaly:"):
            return "akosha:read" in permissions

        if channel.startswith("insight:"):
            return "akosha:read" in permissions

        if channel == "metrics":
            return "akosha:read" in permissions

        # Default: deny
        return False

    async def _get_patterns(self, category: Optional[str]) -> dict:
        """Get detected patterns from analytics engine.

        Args:
            category: Pattern category filter (optional)

        Returns:
            Patterns dictionary
        """
        try:
            # Query patterns from analytics engine
            # This is a placeholder - implementation depends on analytics storage
            return {
                "category": category or "all",
                "patterns": [],
                "count": 0,
            }
        except Exception as e:
            logger.error(f"Error getting patterns: {e}")
            return {"category": category, "error": str(e)}

    async def _get_anomalies(self) -> dict:
        """Get detected anomalies from analytics engine.

        Returns:
            Anomalies dictionary
        """
        try:
            # Query anomalies from analytics engine
            return {
                "anomalies": [],
                "count": 0,
            }
        except Exception as e:
            logger.error(f"Error getting anomalies: {e}")
            return {"error": str(e)}

    # Broadcast methods for analytics events

    async def broadcast_pattern_detected(
        self,
        pattern_id: str,
        pattern_type: str,
        description: str,
        confidence: float,
        metadata: dict,
    ) -> None:
        """Broadcast pattern detected event.

        Args:
            pattern_id: Pattern identifier
            pattern_type: Type of pattern
            description: Pattern description
            confidence: Detection confidence (0.0-1.0)
            metadata: Additional pattern metadata
        """
        event = WebSocketProtocol.create_event(
            EventTypes.PATTERN_DETECTED,
            {
                "pattern_id": pattern_id,
                "pattern_type": pattern_type,
                "description": description,
                "confidence": confidence,
                "timestamp": datetime.now(UTC).isoformat(),
                **metadata,
            },
            room=f"patterns:{pattern_type}",
        )
        await self.broadcast_to_room(f"patterns:{pattern_type}", event)

    async def broadcast_anomaly_detected(
        self,
        anomaly_id: str,
        anomaly_type: str,
        severity: str,
        description: str,
        metrics: dict,
    ) -> None:
        """Broadcast anomaly detected event.

        Args:
            anomaly_id: Anomaly identifier
            anomaly_type: Type of anomaly
            severity: Severity level (low, medium, high, critical)
            description: Anomaly description
            metrics: Anomaly metrics
        """
        event = WebSocketProtocol.create_event(
            EventTypes.ANOMALY_DETECTED,
            {
                "anomaly_id": anomaly_id,
                "anomaly_type": anomaly_type,
                "severity": severity,
                "description": description,
                "metrics": metrics,
                "timestamp": datetime.now(UTC).isoformat(),
            },
            room="anomalies",
        )
        await self.broadcast_to_room("anomalies", event)

    async def broadcast_insight_generated(
        self,
        insight_id: str,
        insight_type: str,
        title: str,
        description: str,
        data: dict,
    ) -> None:
        """Broadcast insight generated event.

        Args:
            insight_id: Insight identifier
            insight_type: Type of insight
            title: Insight title
            description: Insight description
            data: Insight data
        """
        event = WebSocketProtocol.create_event(
            EventTypes.INSIGHT_GENERATED,
            {
                "insight_id": insight_id,
                "insight_type": insight_type,
                "title": title,
                "description": description,
                "data": data,
                "timestamp": datetime.now(UTC).isoformat(),
            },
            room="insights",
        )
        await self.broadcast_to_room("insights", event)

    async def broadcast_aggregation_completed(
        self,
        aggregation_id: str,
        aggregation_type: str,
        record_count: int,
        summary: dict,
    ) -> None:
        """Broadcast aggregation completed event.

        Args:
            aggregation_id: Aggregation identifier
            aggregation_type: Type of aggregation
            record_count: Number of records aggregated
            summary: Aggregation summary
        """
        event = WebSocketProtocol.create_event(
            EventTypes.AGGREGATION_COMPLETED,
            {
                "aggregation_id": aggregation_id,
                "aggregation_type": aggregation_type,
                "record_count": record_count,
                "summary": summary,
                "timestamp": datetime.now(UTC).isoformat(),
            },
            room="metrics",
        )
        await self.broadcast_to_room("metrics", event)
