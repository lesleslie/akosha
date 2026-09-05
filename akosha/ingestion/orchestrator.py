"""Bootstrap orchestrator for autonomous operation when Mahavishnu unavailable."""

import logging
from datetime import UTC, datetime
from typing import Any


class BootstrapOrchestrator:
    """Fallback orchestrator for autonomous operation when Mahavishnu unavailable."""

    def __init__(self, mahavishnu_client: Any = None) -> None:
        """Initialize bootstrap orchestrator.

        Args:
            mahavishnu_client: Mahavishnu MCP client (optional)
        """
        self.mahavishnu_client = mahavishnu_client
        self.fallback_mode = False
        self.last_heartbeat = datetime.now(UTC)
        self.logger = logging.getLogger(__name__)

    async def trigger_ingestion(self) -> bool:
        """Trigger ingestion workflow.

        Logic:
            1. If Mahavishnu client exists and not in fallback mode:
               - Try to trigger workflow via Mahavishnu
               - Update last_heartbeat
               - Return True
            2. If Mahavishnu fails or unavailable:
               - Set fallback_mode = True
               - Log warning
               - Return True (local scheduling will handle it)

        Returns:
            True if trigger successful (or fallback activated)
        """
        try:
            # If we have Mahavishnu client and not already in fallback mode
            if self.mahavishnu_client and not self.fallback_mode:
                # Try to trigger workflow via Mahavishnu
                if hasattr(self.mahavishnu_client, "trigger_workflow"):
                    await self.mahavishnu_client.trigger_workflow(
                        workflow_name="akosha-daily-ingest"
                    )
                elif hasattr(self.mahavishnu_client, "call_tool"):
                    await self.mahavishnu_client.call_tool(
                        tool_name="workflow-trigger", arguments={"workflow": "akosha-daily-ingest"}
                    )

                # Update heartbeat on successful contact
                self.last_heartbeat = datetime.now(UTC)
                self.logger.info("Successfully triggered ingestion via Mahavishnu")
                return True

        except Exception as e:
            self.logger.warning(
                f"Failed to trigger ingestion via Mahavishnu: {e!s}. Switching to fallback mode."
            )

        # Switch to fallback mode if Mahavishnu is unavailable
        if not self.fallback_mode:
            self.fallback_mode = True
            self.logger.warning(
                "Mahavishnu unavailable. Activating fallback mode for autonomous operation."
            )

        # Return True to allow local scheduling to handle the ingestion
        return True

    async def report_health(self) -> dict[str, Any]:
        """Active health probe — pings the injected mahavishnu client.

        Returns a dict with:
        - ``status``: "normal" | "fallback" | "degraded"
            (degraded = ping attempted but failed)
        - ``fallback_mode``: bool, mirrors self.fallback_mode
        - ``last_mahavishnu_contact``: ISO timestamp of the most recent
            *self-attested* heartbeat (legacy field; ``last_actual_ping``
            is the new ground truth)
        - ``last_actual_ping``: ISO timestamp of the most recent
            successful ping, or ``None`` if no client is configured
            or the last ping failed
        - ``ping_result``: dict from the ping call (when successful)
        - ``ping_error``: error string (when ping failed)
        - ``timestamp``: ISO timestamp of this ``report_health`` call
        """
        result: dict[str, Any] = {
            "status": "fallback" if self.fallback_mode else "normal",
            "fallback_mode": self.fallback_mode,
            "last_mahavishnu_contact": self.last_heartbeat.isoformat(),
            "last_actual_ping": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        client = self.mahavishnu_client
        ping = getattr(client, "ping", None) if client is not None else None
        if not callable(ping):
            return result

        try:
            ping_result = await ping()
        except Exception as exc:
            # Audit M3: surface the failure as status="degraded".
            # KeyboardInterrupt and other BaseException subclasses
            # are NOT caught here (Exception, not BaseException).
            result["status"] = "degraded"
            result["ping_error"] = str(exc)
            return result

        result["last_actual_ping"] = datetime.now(UTC).isoformat()
        result["ping_result"] = ping_result
        if result["status"] == "fallback":
            result["status"] = "fallback"  # already correct
        return result
