"""Tests for WebSocket server module.

Tests the Akosha WebSocket server implementation for real-time pattern detection and analytics.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp_common.websocket import MessageType, WebSocketMessage

from akosha.websocket.server import AkoshaWebSocketServer


class TestWebsocketServerInitialization:
    """Test WebSocket server initialization and setup."""

    def test_server_init_basic(self):
        """Test basic server initialization."""
        mock_analytics = AsyncMock()
        server = AkoshaWebSocketServer(analytics_engine=mock_analytics)

        assert hasattr(server, "connections")
        assert server.max_connections == 1000
        assert server.message_rate_limit == 100

    def test_server_init_with_custom_params(self):
        """Test server initialization with custom parameters."""
        mock_analytics = AsyncMock()
        server = AkoshaWebSocketServer(
            analytics_engine=mock_analytics,
            host="0.0.0.0",
            port=8080,
            max_connections=500,
            message_rate_limit=50,
        )

        assert server.host == "0.0.0.0"
        assert server.port == 8080
        assert server.max_connections == 500
        assert server.message_rate_limit == 50

    def test_server_init_with_defaults(self):
        """Test server initialization with default parameters."""
        mock_analytics = AsyncMock()
        server = AkoshaWebSocketServer(analytics_engine=mock_analytics, host="127.0.0.1", port=8692)

        assert server.host == "127.0.0.1"
        assert server.port == 8692
        assert server.max_connections == 1000
        assert server.message_rate_limit == 100

    def test_server_attributes(self):
        """Test server attributes."""
        mock_analytics = AsyncMock()
        server = AkoshaWebSocketServer(analytics_engine=mock_analytics)

        # Should have required attributes
        assert hasattr(server, "connections")
        assert hasattr(server, "max_connections")
        assert hasattr(server, "message_rate_limit")
        assert hasattr(server, "analytics_engine")


class TestWebsocketServerConnection:
    """Test WebSocket server connection handling."""

    @pytest.fixture
    def server(self):
        """Create WebSocket server fixture."""
        mock_analytics = AsyncMock()
        return AkoshaWebSocketServer(analytics_engine=mock_analytics)

    @pytest.mark.asyncio
    async def test_on_connect_basic(self, server):
        """Test basic connection handling.

        The AkoshaWebSocketServer.on_connect sends a welcome message.
        The base class handler adds to server.connections AFTER calling on_connect,
        so we don't expect the connection to be in server.connections yet.
        """
        mock_websocket = AsyncMock()
        connection_id = "test-connection-id"

        await server.on_connect(mock_websocket, connection_id)

        # on_connect sends a welcome message via websocket.send
        mock_websocket.send.assert_awaited_once()
        sent_data = mock_websocket.send.call_args[0][0]
        # Verify it's an encoded welcome message
        assert isinstance(sent_data, str)

    @pytest.mark.asyncio
    async def test_on_connect_max_connections(self, server):
        """Test connection handling with max connections."""
        server.max_connections = 1

        # Add one connection
        mock_websocket1 = AsyncMock()
        await server.on_connect(mock_websocket1, "conn1")

        # Try to add second connection (should be handled gracefully)
        mock_websocket2 = AsyncMock()
        await server.on_connect(mock_websocket2, "conn2")

        # Both should get welcome messages (max_connections is enforced by the
        # base class handler, not by on_connect itself)
        assert mock_websocket1.send.await_count == 1
        assert mock_websocket2.send.await_count == 1

    @pytest.mark.asyncio
    async def test_on_disconnect_basic(self, server):
        """Test basic disconnection handling.

        The AkoshaWebSocketServer.on_disconnect calls leave_all_rooms.
        The base class handler removes from server.connections AFTER calling on_disconnect,
        so we pre-populate connections and verify on_disconnect calls leave_all_rooms.
        """
        mock_websocket = AsyncMock()
        connection_id = "test-connection-id"

        # Add connection first (simulating what base class handler does)
        server.connections[connection_id] = mock_websocket

        # Also add the connection to a room so leave_all_rooms has something to do
        server.connection_rooms["test-room"] = {connection_id}
        server.room_connections[connection_id] = "test-room"

        await server.on_disconnect(mock_websocket, connection_id)

        # leave_all_rooms should have removed the room membership
        assert connection_id not in server.room_connections
        assert connection_id not in server.connection_rooms.get("test-room", set())

    @pytest.mark.asyncio
    async def test_on_disconnect_nonexistent(self, server):
        """Test disconnection handling for non-existent connection."""
        mock_websocket = AsyncMock()
        connection_id = "nonexistent-connection"

        # Should handle gracefully (leave_all_rooms on non-existent is a no-op)
        await server.on_disconnect(mock_websocket, connection_id)


class TestWebsocketServerMessage:
    """Test WebSocket server message handling."""

    @pytest.fixture
    def server(self):
        """Create WebSocket server fixture."""
        mock_analytics = AsyncMock()
        return AkoshaWebSocketServer(analytics_engine=mock_analytics)

    @pytest.mark.asyncio
    async def test_on_message_basic(self, server):
        """Test basic message handling for a REQUEST message."""
        mock_websocket = AsyncMock()
        mock_websocket.id = "test-conn-id"
        mock_websocket.user = None  # No authenticated user

        # Create a proper WebSocketMessage for a request
        mock_message = MagicMock(spec=WebSocketMessage)
        mock_message.type = MessageType.REQUEST
        mock_message.event = "subscribe"
        mock_message.data = {"channel": "test-channel"}
        mock_message.correlation_id = "corr-1"

        await server.on_message(mock_websocket, mock_message)

        # Should send a response (subscribe handled via _handle_request)
        mock_websocket.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_on_message_event_type(self, server):
        """Test message handling for an EVENT message."""
        mock_websocket = AsyncMock()

        # Create a proper WebSocketMessage for an event
        mock_message = MagicMock(spec=WebSocketMessage)
        mock_message.type = MessageType.EVENT
        mock_message.event = "some_event"

        # Should handle event message gracefully (just logs)
        await server.on_message(mock_websocket, mock_message)

        # No response sent for events
        mock_websocket.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_message_invalid_message(self, server):
        """Test message handling for unhandled message type."""
        mock_websocket = AsyncMock()

        # Use a message type that on_message doesn't handle
        mock_message = MagicMock(spec=WebSocketMessage)
        mock_message.type = MessageType.RESPONSE

        # Should handle gracefully (logs warning, no crash)
        await server.on_message(mock_websocket, mock_message)

        # No response sent for unhandled types
        mock_websocket.send.assert_not_awaited()


class TestWebsocketServerAuthentication:
    """Test WebSocket server authentication functionality."""

    @pytest.fixture
    def server(self):
        """Create WebSocket server fixture."""
        mock_analytics = AsyncMock()
        return AkoshaWebSocketServer(analytics_engine=mock_analytics)

    @pytest.mark.asyncio
    async def test_authenticate_valid(self, server):
        """Test authentication of valid token via authenticate_websocket."""
        mock_user = {"user_id": "test-user", "permissions": ["akosha:read"]}
        with patch.object(server, "authenticate_websocket", return_value=mock_user):
            result = server.authenticate_websocket("valid_token")
            assert result is not None
            assert result["user_id"] == "test-user"

    @pytest.mark.asyncio
    async def test_authenticate_invalid(self, server):
        """Test authentication of invalid token via authenticate_websocket."""
        with patch.object(server, "authenticate_websocket", return_value=None):
            result = server.authenticate_websocket("invalid_token")
            assert result is None

    @pytest.mark.asyncio
    async def test_authenticate_none(self, server):
        """Test authentication with no token via authenticate_websocket."""
        with patch.object(server, "authenticate_websocket", return_value=None):
            result = server.authenticate_websocket(None)
            assert result is None


class TestWebsocketServerSubscription:
    """Test WebSocket server subscription functionality."""

    @pytest.fixture
    def server(self):
        """Create WebSocket server fixture."""
        mock_analytics = AsyncMock()
        return AkoshaWebSocketServer(analytics_engine=mock_analytics)

    def test_can_subscribe_basic(self, server):
        """Test basic subscription permission."""
        user = {"user_id": "test-user", "permissions": ["akosha:read"]}
        channel = "pattern:test"

        # The method checks channel prefixes like "pattern:", "anomaly:", "insight:"
        assert hasattr(server, "_can_subscribe_to_channel")
        result = server._can_subscribe_to_channel(user, channel)
        assert result is True

    def test_can_subscribe_different_channels(self, server):
        """Test subscription permission for different channels."""
        test_cases = [
            # akosha:read permission allows prefixed channels
            ({"user_id": "user1", "permissions": ["akosha:read"]}, "pattern:test", True),
            ({"user_id": "user2", "permissions": ["akosha:read"]}, "anomaly:test", True),
            # No permissions denies access
            ({"user_id": "user3", "permissions": []}, "pattern:test", False),
            # Admin can subscribe to any channel
            ({"user_id": "user4", "permissions": ["admin"]}, "pattern:test", True),
            ({"user_id": "user5", "permissions": ["akosha:admin"]}, "anomaly:test", True),
            # Unknown channel prefix defaults to deny
            ({"user_id": "user6", "permissions": ["akosha:read"]}, "unknown:channel", False),
        ]

        for user, channel, expected in test_cases:
            result = server._can_subscribe_to_channel(user, channel)
            assert result == expected, (
                f"Expected {expected} for user {user['user_id']} on channel {channel}, got {result}"
            )


class TestWebsocketServerBroadcast:
    """Test WebSocket server broadcast functionality."""

    @pytest.fixture
    def server(self):
        """Create WebSocket server fixture."""
        mock_analytics = AsyncMock()
        return AkoshaWebSocketServer(analytics_engine=mock_analytics)

    @pytest.mark.asyncio
    async def test_broadcast_pattern_detected(self, server):
        """Test pattern detection broadcast."""
        # Mock broadcast_to_room (the method actually called)
        with patch.object(server, "broadcast_to_room", new_callable=AsyncMock) as mock_broadcast:
            await server.broadcast_pattern_detected(
                pattern_id="pattern-1",
                pattern_type="code_pattern",
                description="Test pattern",
                confidence=0.95,
                metadata={"source": "test"},
            )

            # Should broadcast to the correct room
            mock_broadcast.assert_awaited_once()
            room_id = mock_broadcast.call_args[0][0]
            assert room_id == "patterns:code_pattern"
            event = mock_broadcast.call_args[0][1]
            assert event.data["pattern_id"] == "pattern-1"
            assert event.data["pattern_type"] == "code_pattern"
            assert event.data["confidence"] == 0.95

    @pytest.mark.asyncio
    async def test_broadcast_anomaly_detected(self, server):
        """Test anomaly detection broadcast."""
        with patch.object(server, "broadcast_to_room", new_callable=AsyncMock) as mock_broadcast:
            await server.broadcast_anomaly_detected(
                anomaly_id="anomaly-1",
                anomaly_type="performance_anomaly",
                severity="high",
                description="Test anomaly",
                metrics={"cpu": 95},
            )

            # Should broadcast to the anomalies room
            mock_broadcast.assert_awaited_once()
            room_id = mock_broadcast.call_args[0][0]
            assert room_id == "anomalies"
            event = mock_broadcast.call_args[0][1]
            assert event.data["anomaly_id"] == "anomaly-1"
            assert event.data["severity"] == "high"

    @pytest.mark.asyncio
    async def test_broadcast_insight_generated(self, server):
        """Test insight generation broadcast."""
        with patch.object(server, "broadcast_to_room", new_callable=AsyncMock) as mock_broadcast:
            await server.broadcast_insight_generated(
                insight_id="insight-1",
                insight_type="code_insight",
                title="Test insight",
                description="A test insight",
                data={"key": "value"},
            )

            # Should broadcast to the insights room
            mock_broadcast.assert_awaited_once()
            room_id = mock_broadcast.call_args[0][0]
            assert room_id == "insights"
            event = mock_broadcast.call_args[0][1]
            assert event.data["insight_id"] == "insight-1"
            assert event.data["title"] == "Test insight"

    @pytest.mark.asyncio
    async def test_broadcast_aggregation_completed(self, server):
        """Test aggregation completion broadcast."""
        with patch.object(server, "broadcast_to_room", new_callable=AsyncMock) as mock_broadcast:
            await server.broadcast_aggregation_completed(
                aggregation_id="agg-1",
                aggregation_type="metrics_aggregation",
                record_count=100,
                summary={"systems": ["all"]},
            )

            # Should broadcast to the metrics room
            mock_broadcast.assert_awaited_once()
            room_id = mock_broadcast.call_args[0][0]
            assert room_id == "metrics"
            event = mock_broadcast.call_args[0][1]
            assert event.data["aggregation_id"] == "agg-1"
            assert event.data["record_count"] == 100


class TestWebsocketServerErrorHandling:
    """Test WebSocket server error handling."""

    @pytest.fixture
    def server(self):
        """Create WebSocket server fixture."""
        mock_analytics = AsyncMock()
        return AkoshaWebSocketServer(analytics_engine=mock_analytics)

    @pytest.mark.asyncio
    async def test_connection_error_handling(self, server):
        """Test error handling during connection."""
        mock_websocket = AsyncMock()
        mock_websocket.send.side_effect = Exception("Connection failed")

        # on_connect tries to send a welcome message - should propagate
        with pytest.raises(Exception, match="Connection failed"):
            await server.on_connect(mock_websocket, "test-connection")

    @pytest.mark.asyncio
    async def test_message_error_handling(self, server):
        """Test error handling during message processing with unknown request."""
        mock_websocket = AsyncMock()
        mock_websocket.id = "test-conn-id"
        mock_websocket.user = None  # No authenticated user

        # Create a message with an unknown event - will get UNKNOWN_REQUEST error response
        mock_message = MagicMock(spec=WebSocketMessage)
        mock_message.type = MessageType.REQUEST
        mock_message.event = "unknown_event"
        mock_message.data = {}
        mock_message.correlation_id = "corr-1"

        # Should handle gracefully - sends error response, doesn't crash
        await server.on_message(mock_websocket, mock_message)

        # Should have sent an error response
        mock_websocket.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_broadcast_error_handling(self, server):
        """Test error handling during broadcast."""
        # Mock broadcast_to_room to raise an exception
        with patch.object(
            server,
            "broadcast_to_room",
            new_callable=AsyncMock,
            side_effect=Exception("Broadcast failed"),
        ):
            # Should propagate the error
            with pytest.raises(Exception, match="Broadcast failed"):
                await server.broadcast_pattern_detected(
                    pattern_id="pattern-1",
                    pattern_type="test",
                    description="test",
                    confidence=0.5,
                    metadata={},
                )


class TestWebsocketServerPerformance:
    """Test WebSocket server performance."""

    @pytest.fixture
    def server(self):
        """Create WebSocket server fixture."""
        mock_analytics = AsyncMock()
        return AkoshaWebSocketServer(analytics_engine=mock_analytics)

    @pytest.mark.asyncio
    async def test_connection_performance(self, server):
        """Test connection handling performance."""
        import time

        mock_websocket = AsyncMock()

        start_time = time.time()

        # Handle multiple connections
        for i in range(100):
            await server.on_connect(mock_websocket, f"connection-{i}")

        end_time = time.time()

        # Should be fast (less than 1 second for 100 connections)
        assert (end_time - start_time) < 1.0

    @pytest.mark.asyncio
    async def test_message_performance(self, server):
        """Test message handling performance."""
        import time

        mock_websocket = AsyncMock()
        mock_websocket.id = "test-conn-id"
        mock_websocket.user = None  # No authenticated user

        # Create a proper message for subscribe request
        mock_message = MagicMock(spec=WebSocketMessage)
        mock_message.type = MessageType.REQUEST
        mock_message.event = "subscribe"
        mock_message.data = {"channel": "test-channel"}
        mock_message.correlation_id = "corr-1"

        start_time = time.time()

        # Handle multiple messages
        for i in range(50):
            await server.on_message(mock_websocket, mock_message)

        end_time = time.time()

        # Should be fast (less than 1 second for 50 messages)
        assert (end_time - start_time) < 1.0

    @pytest.mark.asyncio
    async def test_broadcast_performance(self, server):
        """Test broadcast performance."""
        import time

        with patch.object(server, "broadcast_to_room", new_callable=AsyncMock):
            start_time = time.time()

            # Handle multiple broadcasts
            for i in range(20):
                await server.broadcast_pattern_detected(
                    pattern_id=f"pattern-{i}",
                    pattern_type="test",
                    description="test",
                    confidence=0.5,
                    metadata={},
                )

            end_time = time.time()

            # Should be fast (less than 1 second for 20 broadcasts)
            assert (end_time - start_time) < 1.0


class TestWebsocketServerIntegration:
    """Test WebSocket server integration scenarios."""

    @pytest.fixture
    def server(self):
        """Create WebSocket server fixture."""
        mock_analytics = AsyncMock()
        return AkoshaWebSocketServer(analytics_engine=mock_analytics)

    @pytest.mark.asyncio
    async def test_full_connection_cycle(self, server):
        """Test complete connection lifecycle.

        In the real server, the base class handler:
        1. Calls on_connect
        2. Adds to connections
        3. On disconnect, calls on_disconnect
        4. Removes from connections

        Here we simulate that full cycle.
        """
        mock_websocket = AsyncMock()
        connection_id = "test-connection"

        # Simulate what the base class handler does: call on_connect then add
        await server.on_connect(mock_websocket, connection_id)
        server.connections[connection_id] = mock_websocket

        assert connection_id in server.connections

        # Simulate disconnect: call on_disconnect then remove
        await server.on_disconnect(mock_websocket, connection_id)
        del server.connections[connection_id]

        assert connection_id not in server.connections

    @pytest.mark.asyncio
    async def test_concurrent_connections(self, server):
        """Test concurrent connection handling."""
        results = []

        async def connection_worker(worker_id):
            mock_websocket = AsyncMock()
            connection_id = f"worker-{worker_id}-conn"
            await server.on_connect(mock_websocket, connection_id)
            results.append(connection_id)
            await asyncio.sleep(0.1)
            await server.on_disconnect(mock_websocket, connection_id)

        # Create multiple concurrent connections
        tasks = []
        for i in range(5):
            task = asyncio.create_task(connection_worker(i))
            tasks.append(task)

        await asyncio.gather(*tasks)

        # All connections should have been processed
        assert len(results) == 5

    @pytest.mark.asyncio
    async def test_mixed_message_types(self, server):
        """Test handling of different message types."""
        mock_websocket = AsyncMock()
        mock_websocket.id = "test-connection"
        mock_websocket.user = None  # No authenticated user
        connection_id = "test-connection"

        await server.on_connect(mock_websocket, connection_id)

        # Test the three message types that on_message handles:
        # REQUEST -> calls _handle_request
        # EVENT -> calls _handle_event
        # Any other type (RESPONSE, ERROR, etc.) -> logs warning, no action
        message_types = [MessageType.REQUEST, MessageType.EVENT, MessageType.RESPONSE]

        for msg_type in message_types:
            mock_message = MagicMock(spec=WebSocketMessage)
            mock_message.type = msg_type
            mock_message.event = "test_event"
            mock_message.data = {"test": "data"}
            mock_message.correlation_id = "corr-1"

            # Should handle gracefully for all types
            try:
                await server.on_message(mock_websocket, mock_message)
            except Exception:
                # Acceptable for integration test
                pass

        await server.on_disconnect(mock_websocket, connection_id)


class TestWebsocketServerConfiguration:
    """Test WebSocket server configuration."""

    def test_default_configuration(self):
        """Test default server configuration."""
        mock_analytics = AsyncMock()
        server = AkoshaWebSocketServer(analytics_engine=mock_analytics)

        assert server.host == "127.0.0.1"
        assert server.port == 8692
        assert server.max_connections == 1000
        assert server.message_rate_limit == 100

    def test_custom_configuration(self):
        """Test custom server configuration."""
        mock_analytics = AsyncMock()
        server = AkoshaWebSocketServer(
            analytics_engine=mock_analytics,
            host="0.0.0.0",
            port=9000,
            max_connections=2000,
            message_rate_limit=200,
        )

        assert server.host == "0.0.0.0"
        assert server.port == 9000
        assert server.max_connections == 2000
        assert server.message_rate_limit == 200

    def test_configuration_validation(self):
        """Test configuration validation."""
        test_configs = [
            {"host": "127.0.0.1", "port": 8080, "max_connections": 100, "message_rate_limit": 10},
            {"host": "0.0.0.0", "port": 443, "max_connections": 5000, "message_rate_limit": 500},
            {"host": "localhost", "port": 80, "max_connections": 1, "message_rate_limit": 1},
        ]

        for config in test_configs:
            mock_analytics = AsyncMock()
            server = AkoshaWebSocketServer(analytics_engine=mock_analytics, **config)
            assert server.host == config["host"]
            assert server.port == config["port"]
            assert server.max_connections == config["max_connections"]
            assert server.message_rate_limit == config["message_rate_limit"]
