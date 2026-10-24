"""Resource-attribute validator for the OTLP receiver (spec v7 §13.2, PR-A6).

The contract this module implements, in three layers:

1. **Allowlist gate (per span, absolute).** A span whose ``service.namespace``
   is not in :data:`ALLOWED_NAMESPACES` is rejected. The rejection carries the
   namespace that was *seen* and the namespaces that *would* have been
   accepted. "namespace drift" without the offending value is unactionable —
   an operator cannot go and fix a namespace they are not told.

2. **Rate-based drift detection (aggregate, per producer).** Two tracks:

   - **P1 spike** — more than :data:`SPIKE_THRESHOLD` *distinct* drifted
     namespaces from a *single producer* inside a rolling
     :data:`ROLLING_WINDOW_SECONDS`-second window. Alerts once per window.
   - **P2 sustained** — more than :data:`SUSTAINED_THRESHOLD_HR` distinct
     drifted namespaces per hour, sustained over a
     :data:`SUSTAINED_WINDOW_SECONDS` (24h) window. Alerts once per day.

   Both tracks count *distinct* namespaces, never raw events, and both are
   *rates over a window* rather than absolute counts. The v2 rule (count
   drifts per hour) was misclassified by startup bursts and blind to slow
   drift — the spec's decision log records both failures. Counting events
   instead of distinct values would reintroduce the first failure in a new
   shape: a one-off burst of twenty rejections would cross the daily
   threshold and be reported as sustained pressure.

3. **Startup burst tolerated.** The receiver's own start-up can produce a
   legitimate burst: every producer in the fleet re-announces itself within
   seconds of the receiver binding, and a validator with no history sees that
   burst as a sudden explosion of new values. During
   :data:`STARTUP_GRACE_SECONDS` after construction, drift is still rejected
   by the allowlist and still counted in the raw counters, but it is kept out
   of the rate windows so it cannot raise a P1 or a P2. The grace is the
   difference between "the fleet came up" and "the fleet is drifting".

Why the allowlist is not the spec's §13.2 value
------------------------------------------------

The spec was written before PR-A7 landed and asks for::

    ALLOWED_NAMESPACES_BRIDGE  = {"mcp-ecosystem", "bodai"}
    ALLOWED_NAMESPACES_POST_FLIP = {"bodai"}
    ALLOWED_NAMESPACES = ALLOWED_NAMESPACES_BRIDGE

That is now stale, and shipping it would be actively wrong:

* **PR-A7 has already shipped.** mcp-common 0.31.2 is released and installed
  fleet-wide, and ``FastMCPOpenTelemetryMiddleware.service_namespace`` now
  defaults to ``bodai`` (``mcp_common/server/telemetry.py``). No producer
  emits ``mcp-ecosystem`` any more. Keeping it on the allowlist would
  silently permit a namespace that the fleet abandoned — and would mask a
  producer that regressed to the pre-flip default, which is exactly the
  drift this validator exists to catch.
* **``cli-tools`` is a live namespace the spec never knew about.** Claude
  Code, Codex and Qwen run with ``OTEL_RESOURCE_ATTRIBUTES=service.name=cli-tools``
  in ``~/.zshrc``. They are on the allowlist so the CLI-tool fleet is not
  quarantined as drift the moment one of them starts emitting a namespace.

So this module binds the *post-flip* set directly, keeping the spec's
one-line-flip shape (``ALLOWED_NAMESPACES = ALLOWED_NAMESPACES_POST_FLIP``)
and dropping the bridge constant, whose window is closed.

Not a drift signal
------------------

A namespace that stays *out* of the allowlist forever is a stuck producer,
not drift: the cardinality set does not grow. That shows up in
``akosha_otlp_resource_rejected_total`` and in the receiver's own rejection
counter, and it does not need the P1/P2 machinery.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from prometheus_client import Counter, Gauge

from akosha.observability.prometheus_metrics import get_metrics_registry

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

logger = logging.getLogger(__name__)

_REGISTRY = get_metrics_registry()

# ── allowlist ────────────────────────────────────────────────────────────

#: Namespaces the fleet is allowed to emit *after* the PR-A7 flip.
#:
#: ``bodai`` — every MCP server, via mcp-common 0.31.2+ (``service_namespace``
#: defaults to ``bodai``).
#: ``cli-tools`` — the Claude Code / Codex / Qwen fleet, which the shell
#: exports as ``service.name=cli-tools``.
#:
#: The spec's §13.2 bridge set (``{"mcp-ecosystem", "bodai"}``) is deliberately
#: absent: PR-A7 has shipped, so ``mcp-ecosystem`` is drift, not a permitted
#: value. See the module docstring.
ALLOWED_NAMESPACES_POST_FLIP: frozenset[str] = frozenset({"bodai", "cli-tools"})

#: The live allowlist. Bound directly to the post-flip set; the spec's
#: one-line-flip shape is preserved, the closed bridge window is not.
ALLOWED_NAMESPACES: frozenset[str] = ALLOWED_NAMESPACES_POST_FLIP

# ── rate thresholds (§13.2) ──────────────────────────────────────────────

#: Length of the rolling window the P1 spike track looks at.
ROLLING_WINDOW_SECONDS: int = 300

#: More than this many distinct drifted namespaces in the rolling window is
#: a P1 spike. The spec writes it as ">3 in rolling window = P1 spike".
SPIKE_THRESHOLD: int = 3

#: A drift rate above this (per hour) sustained over 24h is a P2.
SUSTAINED_THRESHOLD_HR: float = 0.5

#: The P2 track's window. 24h, per the spec.
SUSTAINED_WINDOW_SECONDS: int = 86_400

#: Grace period after construction during which drift is rejected but not
#: rate-tracked. Covers the receiver's own start-up burst.
STARTUP_GRACE_SECONDS: int = 300

# ── metrics ──────────────────────────────────────────────────────────────

resource_rejected_total: Counter = Counter(
    name="akosha_otlp_resource_rejected_total",
    documentation=(
        "Spans rejected by the §13.2 resource-attribute validator, labelled "
        "by the reason ('namespace_not_allowed' | 'namespace_missing'). "
        "Deliberately NOT labelled by the namespace value: a cardinality-"
        "explosion detector must not itself be a cardinality explosion."
    ),
    labelnames=["reason"],
    registry=_REGISTRY,
)

resource_drift_alert_total: Counter = Counter(
    name="akosha_otlp_resource_drift_alert_total",
    documentation=(
        "Drift alerts raised by the §13.2 validator, labelled by track "
        "('P1_spike' | 'P2_sustained'). P1 fires once per rolling window; "
        "P2 fires once per 24h. Alerting is the receiver's job -- this "
        "counter is the record that the validator asked for one."
    ),
    labelnames=["alert_class"],
    registry=_REGISTRY,
)

resource_drift_values: Gauge = Gauge(
    name="akosha_otlp_resource_drift_values",
    documentation=(
        "Distinct drifted service.namespace values currently inside the "
        "sustained 24h window, fleet-wide. The cardinality signal the P1 and "
        "P2 tracks are computed from."
    ),
    registry=_REGISTRY,
)


@dataclass(frozen=True)
class ResourceValidationResult:
    """The verdict for one span's resource attributes.

    Frozen: this is a receipt. The receiver hands it straight to the DLQ
    writer, and a verdict a later call could mutate is a verdict that can
    disagree with the row it was issued for.
    """

    accepted: bool
    """True when the span's namespace is on the allowlist."""

    reason: str | None
    """Why the span was rejected (``None`` when accepted).

    ``"namespace_not_allowed"`` — the span named a namespace that is not on
    the allowlist. ``"namespace_missing"`` — the span named no namespace at
    all.
    """

    seen_namespace: str | None
    """The namespace the span carried, verbatim. ``None`` when it carried none."""

    allowed_namespaces: frozenset[str]
    """The allowlist that was applied. Carried so a rejection is actionable
    without the operator having to look the set up."""

    producer: str
    """Who the span was attributed to (``service_name``, else the namespace)."""

    alert_class: str | None
    """``"P1_spike"``, ``"P2_sustained"``, or ``None``.

    The classification of the drift, independent of whether this particular
    event is the one that notifies — see :attr:`alert_raised`.
    """

    alert_raised: bool
    """True only for the event that should actually notify.

    P1 notifies once per rolling window and P2 once per 24h, so the
    classification can hold for many consecutive events while only the
    first one of each window is a notification.
    """

    window_drift_values: int
    """Distinct drifted namespaces for this producer in the rolling window."""

    sustained_drift_values_per_hour: float
    """This producer's drift rate over the sustained window: distinct drifted
    namespaces per hour, averaged across the 24h window."""

    message: str
    """Operator-readable summary. Names the offending value and the allowed set."""


class ResourceAttributeValidator:
    """Allowlist gate plus rate-based drift detector for resource attributes.

    One instance per receiver. The instance owns the drift history, so it
    has to be shared across the whole ingest path: a validator constructed
    per row has no history and never detects anything.

    The class is deliberately synchronous and dependency-free apart from the
    shared Prometheus registry — it is called once per span on the ingest
    hot path, and everything it does is arithmetic on a clock.
    """

    def __init__(
        self,
        *,
        allowed_namespaces: frozenset[str] = ALLOWED_NAMESPACES,
        rolling_window_seconds: int = ROLLING_WINDOW_SECONDS,
        spike_threshold: int = SPIKE_THRESHOLD,
        sustained_threshold_hr: float = SUSTAINED_THRESHOLD_HR,
        sustained_window_seconds: int = SUSTAINED_WINDOW_SECONDS,
        startup_grace_seconds: int = STARTUP_GRACE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Build a validator.

        Args:
            allowed_namespaces: The namespaces that are permitted.
            rolling_window_seconds: P1's window.
            spike_threshold: Distinct drifted namespaces in the P1 window
                above which a spike is declared.
            sustained_threshold_hr: P2's threshold, in distinct drifted
                namespaces per hour.
            sustained_window_seconds: P2's window.
            startup_grace_seconds: How long after construction the start-up
                burst is tolerated. Set to 0 to disable the grace.
            clock: Monotonic time source. Injected so the windows are
                testable without sleeping.
        """
        self._allowed = frozenset(allowed_namespaces)
        self._rolling_window_seconds = rolling_window_seconds
        self._spike_threshold = spike_threshold
        self._sustained_threshold_hr = sustained_threshold_hr
        self._sustained_window_seconds = sustained_window_seconds
        self._startup_grace_seconds = startup_grace_seconds
        self._clock = clock
        self._events: dict[str, deque[tuple[float, str]]] = {}
        self._last_spike_at: dict[str, float] = {}
        self._last_sustained_at: dict[str, float] = {}
        self._started_at = clock()

        # Plain int counters, mirroring the receiver's idiom: the in-process
        # source of truth, mirrored into the registry.
        self.drift_events_total = 0
        self.spike_alerts_total = 0
        self.sustained_alerts_total = 0

    # ── public API ───────────────────────────────────────────────────────

    def validate(self, row: Mapping[str, Any]) -> ResourceValidationResult:
        """Check one span's resource attributes.

        Args:
            row: A substrate row as the receiver's proto mapping produces it.
                ``service_namespace`` is the attribute under test;
                ``service_name`` identifies the producer the drift is
                attributed to.

        Returns:
            The verdict. Never raises: a validator that throws on the ingest
            hot path takes the receiver down with it.
        """
        raw = row.get("service_namespace")
        namespace = raw if isinstance(raw, str) else None
        producer = self._producer_of(row, namespace)
        now = self._clock()

        if not namespace:
            # proto3 cannot distinguish an unset string from an empty one --
            # both arrive as "". An empty namespace is not a namespace.
            #
            # The drift value is the empty string, so a producer that never
            # declares a namespace contributes ONE cardinality value, not one
            # per span: "it does not say" is one defect, repeated.
            return self._reject(
                producer=producer,
                namespace=None,
                reason="namespace_missing",
                drift_value="",
                now=now,
            )
        if namespace not in self._allowed:
            return self._reject(
                producer=producer,
                namespace=namespace,
                reason="namespace_not_allowed",
                drift_value=namespace,
                now=now,
            )

        window_values, rate = self._producer_state(producer, now)
        return ResourceValidationResult(
            accepted=True,
            reason=None,
            seen_namespace=namespace,
            allowed_namespaces=self._allowed,
            producer=producer,
            alert_class=None,
            alert_raised=False,
            window_drift_values=window_values,
            sustained_drift_values_per_hour=rate,
            message=f"service.namespace {namespace!r} is allowlisted",
        )

    def reset(self) -> None:
        """Drop the drift history and restart the start-up grace.

        For the receiver's restart path and for tests. The counters are
        deliberately *not* zeroed: they are process-lifetime totals.
        """
        self._events.clear()
        self._last_spike_at.clear()
        self._last_sustained_at.clear()
        self._started_at = self._clock()
        resource_drift_values.set(0)

    # ── internals ────────────────────────────────────────────────────────

    def _reject(
        self,
        *,
        producer: str,
        namespace: str | None,
        reason: str,
        drift_value: str,
        now: float,
    ) -> ResourceValidationResult:
        """Record a rejection, classify the drift, and build the verdict."""
        self.drift_events_total += 1
        resource_rejected_total.labels(reason=reason).inc()

        alert_class, alert_raised, window_values, rate = self._record_drift(
            producer, drift_value, now
        )
        if alert_raised and alert_class is not None:
            resource_drift_alert_total.labels(alert_class=alert_class).inc()
            logger.warning(
                "akosha.resource_validator.alert class=%s producer=%s namespace=%s "
                "window_drift_values=%s sustained_values_per_hour=%.3f",
                alert_class,
                producer,
                namespace,
                window_values,
                rate,
            )

        if reason == "namespace_missing":
            message = (
                f"span carries no service.namespace; allowed namespaces are {sorted(self._allowed)}"
            )
        else:
            message = (
                f"service.namespace {namespace!r} is not allowlisted; "
                f"allowed namespaces are {sorted(self._allowed)}"
            )

        return ResourceValidationResult(
            accepted=False,
            reason=reason,
            seen_namespace=namespace,
            allowed_namespaces=self._allowed,
            producer=producer,
            alert_class=alert_class,
            alert_raised=alert_raised,
            window_drift_values=window_values,
            sustained_drift_values_per_hour=rate,
            message=message,
        )

    def _record_drift(
        self, producer: str, drift_value: str, now: float
    ) -> tuple[str | None, bool, int, float]:
        """Fold one drift event into the windows and classify it.

        Returns:
            ``(alert_class, alert_raised, window_drift_values, rate_per_hour)``.
        """
        if now - self._started_at < self._startup_grace_seconds:
            # Start-up burst. Counted in the raw counters, deliberately kept
            # out of the rate windows: the receiver has just bound, every
            # producer in the fleet is re-announcing itself, and a validator
            # with no history would otherwise read that burst as a
            # cardinality explosion. The allowlist gate still rejected the
            # span -- the grace suppresses the *alarm*, not the rejection.
            return None, False, 0, 0.0

        events = self._events.setdefault(producer, deque())
        events.append((now, drift_value))
        self._prune(events, now)

        window_cutoff = now - self._rolling_window_seconds
        # Both tracks count DISTINCT values, and that is the whole point of
        # the rate-based rework. A cardinality explosion is a burst of NEW
        # namespaces; a producer emitting the same bad namespace a thousand
        # times is one bad value and a thousand rejected spans, and the
        # rejection counter says so. Counting events here would make a
        # one-off burst look like sustained pressure the moment the event
        # count crossed the daily threshold, which is precisely the
        # confusion the two tracks exist to avoid.
        window_values = len({value for ts, value in events if ts >= window_cutoff})
        sustained_values = len({value for _, value in events})
        rate = sustained_values / (self._sustained_window_seconds / 3600.0)
        resource_drift_values.set(self._fleet_drift_values())

        alert_class: str | None = None
        alert_raised = False
        if window_values > self._spike_threshold:
            alert_class = "P1_spike"
            last = self._last_spike_at.get(producer)
            if last is None or now - last >= self._rolling_window_seconds:
                self._last_spike_at[producer] = now
                self.spike_alerts_total += 1
                alert_raised = True
        elif rate > self._sustained_threshold_hr:
            # P1 takes precedence on the same event: a producer already
            # paging on a spike does not also need a daily digest.
            alert_class = "P2_sustained"
            last = self._last_sustained_at.get(producer)
            if last is None or now - last >= self._sustained_window_seconds:
                self._last_sustained_at[producer] = now
                self.sustained_alerts_total += 1
                alert_raised = True
        return alert_class, alert_raised, window_values, rate

    def _producer_state(self, producer: str, now: float) -> tuple[int, float]:
        """Current ``(window_drift_values, values_per_hour)`` for a producer."""
        events = self._events.get(producer)
        if not events:
            return 0, 0.0
        self._prune(events, now)
        window_cutoff = now - self._rolling_window_seconds
        window_values = len({value for ts, value in events if ts >= window_cutoff})
        sustained_values = len({value for _, value in events})
        rate = sustained_values / (self._sustained_window_seconds / 3600.0)
        return window_values, rate

    def _prune(self, events: deque[tuple[float, str]], now: float) -> None:
        """Drop events that have fallen out of the sustained window."""
        cutoff = now - self._sustained_window_seconds
        while events and events[0][0] < cutoff:
            events.popleft()

    def _fleet_drift_values(self) -> int:
        """Distinct drifted namespaces across every producer, fleet-wide."""
        return len({value for events in self._events.values() for _, value in events})

    @staticmethod
    def _producer_of(row: Mapping[str, Any], namespace: str | None) -> str:
        """The identity a drift is attributed to.

        ``service_name`` when present, else the namespace, else a constant.
        Attribution matters: the P1 track is per producer, so lumping
        anonymous producers together would manufacture spikes out of unrelated
        services.
        """
        name = row.get("service_name")
        if isinstance(name, str) and name:
            return name
        return namespace or "unknown"


__all__ = [
    "ALLOWED_NAMESPACES",
    "ALLOWED_NAMESPACES_POST_FLIP",
    "ROLLING_WINDOW_SECONDS",
    "SPIKE_THRESHOLD",
    "STARTUP_GRACE_SECONDS",
    "SUSTAINED_THRESHOLD_HR",
    "SUSTAINED_WINDOW_SECONDS",
    "ResourceAttributeValidator",
    "ResourceValidationResult",
    "resource_drift_alert_total",
    "resource_drift_values",
    "resource_rejected_total",
]
