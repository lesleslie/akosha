"""WebSocket server for Akosha pattern detection and analytics.

This module provides real-time updates for:
- Pattern detection events
- Anomaly detection alerts
- Insight generation
- Analytics aggregation

Example:
    >>> from akosha.websocket import AkoshaWebSocketServer
    >>> server = AkoshaWebSocketServer(analytics_engine)
    >>> await server.start()
"""

from .server import AkoshaWebSocketServer

__all__ = ["AkoshaWebSocketServer"]
