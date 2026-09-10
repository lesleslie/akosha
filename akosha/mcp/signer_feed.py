"""Lifespan-owned signer feed state for Akosha's ``/health`` aggregator.

The :class:`SignerFeedState` is the lifespan's single source of truth
for what the skills_signer feed reports to ``/health``. It bundles:

- the manifest itself (pure data, lives in :mod:`akosha.skills_signer`)
- the four mandatory feed signals required by
  ``mcp-backend-wiring-discipline.md`` (``feed_entities_count``,
  ``feed_last_updated_timestamp``, ``cycles_total``, ``errors_total``)
- a ``generation`` token so concurrent-app teardown checks can
  verify ownership before clearing state

Why this lives in ``akosha/mcp/`` (not ``akosha/skills_signer/``):

The ``skills_signer`` package is supposed to replicate byte-for-byte
across the 5 Bodai servers. Lifecycle state (counters, timestamps,
generation tokens) is per-server MCP wiring concern. By keeping the
package pure data and adding the feed state here, the cross-server
package stays trivial to copy-paste and the per-server wiring
contract is explicit.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from akosha.skills_signer import PubkeyManifest

logger = logging.getLogger(__name__)


@dataclass
class SignerFeedState:
    """Lifespan-owned state for the skills_signer ``/health`` feed.

    Constructed at lifespan startup after the keypair is loaded/persisted
    and the manifest is built. Mutation happens only via the
    :meth:`record_cycle` / :meth:`record_error` helpers so the
    ``last_updated_timestamp`` stays consistent with the cycle counter.

    Attributes:
        manifest: the :class:`PubkeyManifest` published in ``/health``.
        last_updated_timestamp: unix timestamp of the most recent update
            (initial creation or last :meth:`record_cycle`).
        cycles_total: count of successful feed update cycles since startup.
        errors_total: count of failed feed update cycles since startup.
        generation: monotonic token so teardown can detect
            cross-app state contamination. Incremented whenever the
            state is rebuilt (e.g., after a key load failure).
    """

    manifest: PubkeyManifest
    last_updated_timestamp: float = field(default_factory=time.time)
    cycles_total: int = 0
    errors_total: int = 0
    generation: int = 0

    def record_cycle(self) -> None:
        """Mark a successful feed update. Bumps ``cycles_total`` and
        ``last_updated_timestamp``.
        """
        self.cycles_total += 1
        self.last_updated_timestamp = time.time()

    def record_error(self) -> None:
        """Mark a failed feed update. Bumps ``errors_total`` and
        ``last_updated_timestamp`` (the timestamp is updated even on
        errors so operators can see the feed is still being polled).
        """
        self.errors_total += 1
        self.last_updated_timestamp = time.time()

    def is_ok(self) -> bool:
        """True when the feed has at least one entry. Empty manifests
        return False so the ``/health`` endpoint returns 503.
        """
        return not self.manifest.is_empty()

    def as_dict(self) -> dict[str, object]:
        """Serialize for the ``/health`` payload.

        Returns a flat dict compatible with the other Akosha feed
        entries (``ok``, ``feed_entities_count``, ``feed_last_updated_timestamp``,
        ``cycles_total``, ``errors_total``). The manifest data is
        nested under ``key_count`` / ``pubkeys`` for backwards compat
        with Phase 2/6 installers that already parse those fields.
        """
        manifest_dict = self.manifest.as_dict()
        return {
            "ok": self.is_ok(),
            "feed": "skills_signer",
            "feed_entities_count": manifest_dict["key_count"],
            "feed_last_updated_timestamp": self.last_updated_timestamp,
            "cycles_total": self.cycles_total,
            "errors_total": self.errors_total,
            "generation": self.generation,
            "key_count": manifest_dict["key_count"],
            "pubkeys": manifest_dict["pubkeys"],
        }


__all__ = ["SignerFeedState"]
