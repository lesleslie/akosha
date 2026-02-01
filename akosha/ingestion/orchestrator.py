"""Bootstrap orchestrator for autonomous operation when Mahavishnu unavailable."""

import logging
from datetime import datetime, UTC

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
                if hasattr(self.mahavishnu_client, 'trigger_workflow'):
                    await self.mahavishnu_client.trigger_workflow(
                        workflow_name="akosha-daily-ingest"
                    )
                elif hasattr(self.mahavishnu_client, 'call_tool'):
                    await self.mahavishnu_client.call_tool(
                        tool_name="workflow-trigger",
                        arguments={"workflow": "akosha-daily-ingest"}
                    )

                # Update heartbeat on successful contact
                self.last_heartbeat = datetime.now(UTC)
                self.logger.info("Successfully triggered ingestion via Mahavishnu")
                return True

        except Exception as e:
            self.logger.warning(
                f"Failed to trigger ingestion via Mahavishnu: {str(e)}. "
                f"Switching to fallback mode."
            )

        # Switch to fallback mode if Mahavishnu is unavailable
        if not self.fallback_mode:
            self.fallback_mode = True
            self.logger.warning(
                "Mahavishnu unavailable. Activating fallback mode for "
                "autonomous operation."
            )

        # Return True to allow local scheduling to handle the ingestion
        return True

    async def report_health(self) -> dict[str, Any]:
        """Report health status.

        Returns:
            Dict with: status, fallback_mode, last_mahavishnu_contact
        """
        status = "fallback" if self.fallback_mode else "normal"

        return {
            "status": status,
            "fallback_mode": self.fallback_mode,
            "last_mahavishnu_contact": self.last_heartbeat.isoformat(),
            "timestamp": datetime.now(UTC).isoformat()
        }