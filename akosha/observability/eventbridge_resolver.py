"""Production wiring entry point for the Akosha EventBridge publisher.

The ``akosha.observability.eventbridge_publisher`` module uses a
module-level global set via ``set_eventbridge_publisher``. This module
provides the resolver that ``AksoshaApplication.start()`` calls to
construct and inject the publisher at app startup.

Wiring is opt-in:
- ``cfg.eventbridge.enabled=False`` (default) -> returns None; existing
  module-level publisher (if any) is reset to None so a previously
  wired bridge stops emitting.
- ``cfg.eventbridge.dry_run=True`` (default) -> returns None; safety
  override, no live bridge is wired.
- ``cfg.eventbridge.enabled=True`` AND ``cfg.eventbridge.dry_run=False``
  AND ``bridge`` provided -> constructs ``EventBridgePublisher(bridge)``,
  calls ``set_eventbridge_publisher(...)`` with it, and returns the
  new publisher.

When ``bridge`` is None (the pre-Oneiric-runtime case), the resolver
returns None -- the full Oneiric runtime initialization is deferred;
this resolver is the seam where production code will pass the live
bridge once that wiring exists.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from akosha.observability.eventbridge_adapter import EventBridgePublisher
from akosha.observability.eventbridge_publisher import set_eventbridge_publisher

if TYPE_CHECKING:
    from akosha.config import AkoshaConfig


def wire_eventbridge_publisher(
    cfg: AkoshaConfig,
    *,
    bridge: Any | None = None,
) -> EventBridgePublisher | None:
    """Construct an EventBridgePublisher (when opted in) and wire it.

    Args:
        cfg: Loaded Akosha settings. The ``eventbridge`` block controls
            whether wiring happens.
        bridge: Pre-constructed Oneiric EventBridge instance. If None,
            the resolver returns None and clears any existing
            module-level publisher (so operators can toggle off).

    Returns:
        The new ``EventBridgePublisher`` (also set as the module-level
        global via ``set_eventbridge_publisher``) when wiring happens;
        ``None`` otherwise.
    """
    eb = cfg.eventbridge

    if not eb.enabled or eb.dry_run or bridge is None:
        # Either opt-out or runtime unavailable. Clear any pre-existing
        # publisher so a previously-wired bridge stops emitting.
        set_eventbridge_publisher(None)
        return None

    publisher = EventBridgePublisher(bridge)
    set_eventbridge_publisher(publisher)
    return publisher


__all__ = ["wire_eventbridge_publisher"]
