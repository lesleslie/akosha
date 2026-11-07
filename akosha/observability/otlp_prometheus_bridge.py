"""OTLP metric data points → ``prometheus_client`` instruments.

The receiver turns an OTLP MetricsService export into two things: durable
rows in ``metrics_substrate`` (Phase 1's store) and live series on akosha's
shared ``CollectorRegistry`` (this module). The registry is what the
``/metrics`` route on :8682 already renders, so a metric that reaches here
is queryable through Prometheus without a new scrape target and without a
second telemetry pipeline.

Two producers, one registry
----------------------------

mahavishnu's ``OTLPMetricExporter`` and oneiric's ``OTLPObservabilityAdapter``
both speak OTLP MetricsService to :4317 and are **indistinguishable on the
wire** — the plan's Finding 10. They can emit the same instrument name, so
the exposed series are namespaced by the resource's ``service.name``.
Without that, the second producer's metric either collides with the first's
in the shared registry (a hard ``ValueError`` from ``prometheus_client``) or
silently overwrites it.

The namespace is a **prefix**, not a label. A label would still leave both
producers sharing one instrument, and the two producers can disagree about
the instrument *type* (one's gauge, the other's counter) and about a
histogram's bucket boundaries — neither of which a label can reconcile. A
prefix gives each producer its own instrument.

The one wrinkle a prefix introduces: a producer whose instrument name
already begins with its service name would be double-prefixed
(``mahavishnu_mahavishnu_workflows_executed_total``), which breaks the
plan's own ``grep mahavishnu_observability`` acceptance check. The prefix is
therefore skipped when the metric name already starts with the service name
— the convention the OTel SDK's own instrumentation follows.

Pre-aggregated data
--------------------

OTLP carries **aggregated** data points: a histogram arrives as per-bucket
counts, a counter as an absolute cumulative value. ``prometheus_client``'s
instruments are built around *observations* — ``Histogram.observe(value)``
and ``Counter.inc(delta)`` — so importing aggregated data needs two things
the public API does not offer:

* a histogram's per-bucket counts have to be written into the instrument's
  bucket values, and
* a cumulative counter has to become the *delta* since the last value this
  bridge saw, because ``Counter.inc`` adds.

Both are handled here. The histogram's buckets are written through
``Histogram``'s bucket values (the only route ``prometheus_client`` offers
for pre-aggregated counts) and the temporality state is kept per series, so
a cumulative export becomes a delta rather than being added twice. A
cumulative value that goes *backwards* is treated as a producer restart and
re-baselined, which is what the Prometheus counter-reset convention expects.
The histogram path applies the SAME rule: a cumulative bucket ladder that
moves backwards is a reset, and the whole current ladder is the increment
since the reset. Clamping the deltas to zero instead would lose that
interval for ever.

Replays
-------

A metric data point's identity is the series plus the instant, so the same
point arriving twice is the same point — and the substrate treats the second
arrival as a no-op. The bridge has to agree: a *delta* point is an
increment, and applying the same increment twice would double-count it
while the substrate counted one row. The bridge therefore remembers the
identity of the last delta point it applied per series and skips a repeat.
Cumulative points need no such guard — their delta is already zero on a
replay.

Cardinality
-----------

A received data point's attributes become Prometheus labels, and a producer
that puts a request id in an attribute would otherwise mint an unbounded
number of series in a registry that is rendered on every scrape. Three
caps bound that growth, and each one DROPS the point from the exposure and
counts it, so the cap is visible rather than silent:

* ``MAX_SERIES_PER_METRIC`` — distinct label-value combinations within one
  metric family.
* ``MAX_METRIC_FAMILIES`` — distinct metric families. Nothing else bounds
  this: one instrument is registered per distinct metric name, so a
  producer that invents a new metric name per request would grow the
  registry without limit.
* ``MAX_TARGET_INFO_SERIES`` / ``MAX_SCOPE_INFO_SERIES`` — the children of
  the ``target_info`` and ``otel_scope_info`` gauges, which are keyed by
  the producer's resource and scope and are therefore as unbounded as the
  number of producers and instances.

In every case the point is still persisted in the substrate, which holds the
attributes verbatim — only the *exposure* is dropped.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prometheus_client import Counter, Gauge, Histogram, Summary

from akosha.observability.prometheus_metrics import get_metrics_registry

if TYPE_CHECKING:
    from collections.abc import Mapping

    from prometheus_client import CollectorRegistry

logger = logging.getLogger(__name__)

#: Distinct label-value combinations allowed per metric family.
MAX_SERIES_PER_METRIC: int = 100

#: Distinct metric families allowed on the shared registry. A family is one
#: instrument, and nothing else bounds how many a producer can mint: a
#: producer that names a metric per request would otherwise register an
#: instrument per request, and the registry is rendered on every scrape.
MAX_METRIC_FAMILIES: int = 500

#: Distinct ``target_info`` children allowed. One per producer resource.
MAX_TARGET_INFO_SERIES: int = 100

#: Distinct ``otel_scope_info`` children allowed. One per instrumentation
#: scope.
MAX_SCOPE_INFO_SERIES: int = 100

#: Label names Prometheus reserves for its own use. A data-point attribute
#: with one of these names would collide with the instrument's own label and
#: is renamed rather than dropped.
RESERVED_LABEL_NAMES: frozenset[str] = frozenset({"le", "quantile"})

#: Characters Prometheus does not allow in a metric or label name.
_INVALID_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_]")

#: The resource attributes that become ``target_info`` labels. A fixed set,
#: because a metric family in ``prometheus_client`` has one label set for
#: every series it holds — a resource that carries an extra attribute
#: cannot mint a second ``target_info``.
TARGET_INFO_LABELS: tuple[str, ...] = (
    "service_name",
    "service_namespace",
    "service_version",
    "service_instance_id",
    "deployment_environment",
)

#: The scope attributes that become ``otel_scope_info`` labels.
SCOPE_INFO_LABELS: tuple[str, ...] = (
    "otel_scope_name",
    "otel_scope_version",
    "service_name",
)

#: Resource attribute → ``target_info`` label.
#:
#: ORDERED, and the order is load-bearing: several attributes map onto the
#: same label (``deployment.environment`` and ``deployment.environment.name``
#: both become ``deployment_environment``), and a producer sends one of them
#: or the other. The first attribute that carries a non-empty value wins the
#: label. Keying the result by LABEL instead would let the absent attribute
#: overwrite the present one with an empty string — which is exactly what
#: oneiric's adapter's ``deployment.environment`` used to hit.
_RESOURCE_TO_TARGET_LABEL: dict[str, str] = {
    "service.name": "service_name",
    "service.namespace": "service_namespace",
    "service.version": "service_version",
    "service.instance.id": "service_instance_id",
    "deployment.environment": "deployment_environment",
    "deployment.environment.name": "deployment_environment",
}


@dataclass
class _Family:
    """One exposed metric family and the state of its series.

    ``boundaries`` is the histogram's bucket layout, fixed when the family
    is created. A later export that carries different boundaries is
    divergent: the plan's risk table says to accept the producer's
    boundaries on first sight and ignore later divergences rather than
    resetting the histogram.

    ``adopted`` records that the instrument was REUSED from the registry
    rather than created by this bridge. A reused instrument already holds a
    value — the previous bridge's — so the temporality state starts from
    that value instead of from zero.
    """

    kind: str
    instrument: Any
    labelnames: tuple[str, ...]
    boundaries: tuple[float, ...] | None = None
    adopted: bool = False
    series: dict[tuple[str, ...], dict[str, Any]] = field(default_factory=dict)


def sanitize_name(name: str) -> str:
    """Prometheus-identifier-safe form of ``name``.

    Prometheus metric and label names match ``[a-zA-Z_][a-zA-Z0-9_]*``. An
    OTel instrument name is dotted (``mahavishnu.workflows.executed``) and an
    attribute key is arbitrary, so both are rewritten rather than dropped —
    a metric that cannot be named cannot be exposed, and an unexposed metric
    is the "strictly worse than an honest UNIMPLEMENTED" outcome the plan
    warns about.
    """
    cleaned = _INVALID_NAME_CHARS.sub("_", name)
    if not cleaned:
        return "unnamed"
    if cleaned[0].isdigit():
        return f"_{cleaned}"
    return cleaned


def sanitize_label_name(key: str) -> str:
    """Label-name-safe form of an attribute key.

    A key that collides with a reserved label name (``le`` on a histogram,
    ``quantile`` on a summary) is suffixed rather than dropped: the
    attribute still carries information, and Prometheus refuses to register
    a metric whose label set contains a reserved name.
    """
    cleaned = sanitize_name(key)
    if cleaned in RESERVED_LABEL_NAMES:
        return f"{cleaned}_"
    return cleaned


def series_name(service_name: str, metric_name: str) -> str:
    """The exposed series name for ``metric_name`` produced by ``service_name``.

    The service name is a prefix, so two producers cannot mint the same
    instrument in the shared registry. The prefix is skipped when the metric
    name already begins with the service name — otherwise mahavishnu's own
    instruments would be exposed as
    ``mahavishnu_mahavishnu_observability_drift_detected_total`` and the
    plan's ``grep mahavishnu_observability`` check would find nothing.
    """
    service = sanitize_name(service_name)
    metric = sanitize_name(metric_name)
    if not service or metric == service or metric.startswith(f"{service}_"):
        return metric
    return f"{service}_{metric}"


def label_value(value: Any) -> str:
    """Render an attribute value as a Prometheus label value."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def label_signature(labelnames: tuple[str, ...]) -> str:
    """A short, stable signature of an attribute key set.

    Used to name a SIBLING family when a producer changes the attribute
    keys it reports for one metric name. The signature has to be stable
    across processes and restarts (``hash()`` is salted per interpreter),
    so it is a digest of the joined key names.
    """
    digest = hashlib.blake2b("\x00".join(labelnames).encode("utf-8"), digest_size=4)
    return digest.hexdigest()


class OTLPPrometheusBridge:
    """Expose received OTLP data points on a shared ``CollectorRegistry``.

    One instance per receiver. The temporality state is per series and per
    bridge: a *cumulative* export carries an absolute total, and turning it
    into a ``Counter.inc`` delta needs to know what the previous export
    said. Keeping that state on the bridge (rather than on a module-level
    singleton) means a restarted receiver re-baselines instead of adding a
    stale delta to whatever the previous receiver left in the registry.
    """

    def __init__(
        self,
        registry: CollectorRegistry | None = None,
        *,
        max_series_per_metric: int = MAX_SERIES_PER_METRIC,
        max_metric_families: int = MAX_METRIC_FAMILIES,
        max_target_info_series: int = MAX_TARGET_INFO_SERIES,
        max_scope_info_series: int = MAX_SCOPE_INFO_SERIES,
    ) -> None:
        """Bind the bridge to a registry.

        Args:
            registry: The registry to expose on. Defaults to akosha's
                shared registry — the one the ``/metrics`` route renders.
            max_series_per_metric: Distinct label-value combinations allowed
                per metric family before further series are dropped.
            max_metric_families: Distinct metric families allowed before
                further families are dropped.
            max_target_info_series: Distinct ``target_info`` children
                allowed.
            max_scope_info_series: Distinct ``otel_scope_info`` children
                allowed.
        """
        self._registry = registry if registry is not None else get_metrics_registry()
        self._max_series = max_series_per_metric
        self._max_families = max_metric_families
        self._max_target_info = max_target_info_series
        self._max_scope_info = max_scope_info_series
        self._families: dict[str, _Family] = {}
        # Instrument names that are PERMANENTLY unusable, mapped to the
        # reason. Only a genuine instrument conflict belongs here: a
        # shape-dependent failure (a histogram with no explicit bounds, an
        # unknown metric kind) must not prevent a later, representable
        # point from creating the family.
        self._unsupported: dict[str, str] = {}
        self._target_info: Gauge | None = None
        self._scope_info: Gauge | None = None
        self._target_info_keys: set[tuple[str, ...]] = set()
        self._scope_info_keys: set[tuple[str, ...]] = set()
        # The drops, broken down by reason. The receiver snapshots this
        # around a batch to work out how many drops that batch caused.
        self.dropped_by_reason: dict[str, int] = {}

    # ── public API ───────────────────────────────────────────────────────

    def export(self, point: Mapping[str, Any]) -> bool:
        """Expose one validated data point.

        Args:
            point: A substrate row as the receiver's metric mapping produces
                it, plus the keys the substrate's projection ignores and the
                bridge needs: ``temporality`` and ``scope_version``.

        Returns:
            True when the point reached the registry, False when it was
            dropped (cardinality, divergent buckets, an unusable label set).
            A dropped point is still persisted — the substrate holds the
            attributes verbatim.
        """
        metric_name = str(point.get("metric_name") or "")
        service_name = str(point.get("service_name") or "")
        base_name = series_name(service_name, metric_name)
        kind = str(point.get("data_type") or "")
        labels = self._labels_of(point)
        labelnames = tuple(sorted(labels))

        factory, family_kind, boundaries = self._instrument_kind(kind, point)
        if factory is None:
            # A shape-dependent failure: the point cannot be represented as
            # a Prometheus instrument at all (a histogram with no explicit
            # bounds, an unknown metric kind). It is NOT cached — a later,
            # representable point for the same metric name must still be
            # able to create the family.
            self._drop("unsupported", metric_name)
            return False

        instrument_name = self._instrument_name(base_name, factory, labelnames)
        cached = self._unsupported.get(instrument_name)
        if cached is not None:
            self._drop(cached, metric_name)
            return False

        family = self._families.get(instrument_name)
        if family is None:
            if len(self._families) >= self._max_families:
                self._drop("family_cap", metric_name)
                return False
            family, reason = self._create_family(
                instrument_name, factory, family_kind, labelnames, boundaries, point
            )
            if family is None:
                if reason == "instrument_conflict":
                    # Permanent: the name is held by an instrument that
                    # cannot serve this point.
                    self._unsupported[instrument_name] = reason
                self._drop(reason, metric_name)
                return False
            self._families[instrument_name] = family

        cap_reason = self._record_resource(point)
        if cap_reason:
            self._drop(cap_reason, metric_name)
            return False

        key = tuple(labels[label] for label in family.labelnames)
        state = family.series.get(key)
        if state is None:
            if len(family.series) >= self._max_series:
                self._drop("cardinality", metric_name)
                return False
            state = {}
            if family.adopted:
                self._seed_adopted_baseline(family, point, state)
            family.series[key] = state

        identity: tuple[int, str] | None = None
        if str(point.get("temporality") or "cumulative") == "delta":
            identity = self._point_identity(point)
            if state.get("last_delta_identity") == identity:
                # A replay of a delta point. The substrate treats the
                # repeat as a no-op — the point's identity is already
                # stored — so the bridge must not add the increment a
                # second time. The two halves of the same handler have to
                # agree about what a replay is.
                self._drop("replay", metric_name)
                return False

        try:
            exposed = self._apply(family, state, point)
        except Exception:
            logger.exception(
                "akosha.otlp_metrics.expose_failed metric=%s kind=%s", metric_name, kind
            )
            self._drop("expose_failed", metric_name)
            return False
        if not exposed:
            return False
        if identity is not None:
            state["last_delta_identity"] = identity
        return True

    # ── internals ────────────────────────────────────────────────────────

    def _instrument_kind(
        self,
        kind: str,
        point: Mapping[str, Any],
    ) -> tuple[Any, str, tuple[float, ...] | None]:
        """The instrument factory, the family kind, and the bucket layout.

        Returns ``(None, "", None)`` when the point cannot be represented
        as a Prometheus instrument at all.
        """
        if kind == "sum" and bool(point.get("is_monotonic")):
            return (Counter, "counter", None)
        if kind in {"gauge", "sum"}:
            # A non-monotonic sum is an "up-down counter": Prometheus has no
            # such type, and the OpenTelemetry convention is to expose it as
            # a Gauge.
            return (Gauge, "gauge", None)
        if kind in {"histogram", "exponential_histogram"}:
            boundaries = self._histogram_boundaries(point)
            if not boundaries:
                return (None, "", None)
            return (Histogram, "histogram", boundaries)
        if kind == "summary":
            return (Summary, "summary", None)
        logger.warning("akosha.otlp_metrics.unknown_kind kind=%s", kind)
        return (None, "", None)

    def _instrument_name(
        self,
        base_name: str,
        factory: Any,
        labelnames: tuple[str, ...],
    ) -> str:
        """The registry name for a family.

        The base name is used when it is free, or when the instrument
        already registered under it is the same KIND with the same label
        set (the reuse path). Otherwise a SIBLING name is minted from the
        attribute key-set's signature.

        The sibling is what keeps a producer that changes its attribute key
        set from poisoning the metric: the first key set keeps the base
        name, the second gets its own instrument, and neither is dropped
        for ever.
        """
        existing = self._existing_collector(base_name)
        if existing is None:
            return base_name
        if (
            isinstance(existing, factory)
            and tuple(getattr(existing, "_labelnames", ())) == labelnames
        ):
            return base_name
        return f"{base_name}__{label_signature(labelnames)}"

    def _create_family(
        self,
        instrument_name: str,
        factory: Any,
        family_kind: str,
        labelnames: tuple[str, ...],
        boundaries: tuple[float, ...] | None,
        point: Mapping[str, Any],
    ) -> tuple[_Family | None, str]:
        """Create and register the instrument for a metric family.

        Returns the family and, when there is none, the reason the point
        could not be exposed.
        """
        documentation = str(point.get("description") or "") or f"OTLP metric {instrument_name}"
        unit = str(point.get("unit") or "")
        if unit:
            documentation = f"{documentation} (unit: {unit})"

        if family_kind == "histogram":
            existing = self._existing_collector(instrument_name)
            if existing is not None:
                # The instrument was registered by an earlier bridge (or an
                # earlier point). Its bucket layout is the one that counts:
                # writing the NEW point's boundaries against the OLD
                # instrument would put counts in the wrong ``le`` slots, or
                # raise an IndexError. A genuine divergence is a drop, not
                # a mislabelled write.
                existing_bounds = self._instrument_boundaries(existing)
                if existing_bounds != boundaries:
                    logger.warning(
                        "akosha.otlp_metrics.bucket_divergence name=%s existing=%s wanted=%s",
                        instrument_name,
                        existing_bounds,
                        boundaries,
                    )
                    return (None, "bucket_divergence")
                boundaries = existing_bounds

        kwargs: dict[str, Any] = {"documentation": documentation, "labelnames": labelnames}
        if family_kind == "histogram":
            kwargs["buckets"] = list(boundaries or ())
        instrument, reused = self._register(factory, instrument_name, **kwargs)
        if instrument is None:
            return (None, "instrument_conflict")
        return (
            _Family(
                kind=family_kind,
                instrument=instrument,
                labelnames=labelnames,
                boundaries=boundaries,
                adopted=reused,
            ),
            "",
        )

    def _register(self, factory: Any, name: str, **kwargs: Any) -> tuple[Any | None, bool]:
        """Register an instrument, or reuse the one already registered.

        ``prometheus_client`` refuses to register the same name twice, and
        the registry is a process-wide singleton: a second receiver (a test
        fixture, a second receiver in the same interpreter) would otherwise
        fail to expose a metric that is already there. The existing
        collector is reused when the registry holds one under that name —
        but only when it is the same KIND of instrument with the same label
        set, because a Gauge reused for a Counter would silently record
        the wrong thing and a differently-labelled instrument would raise
        on every ``labels()`` call.

        Returns ``(instrument, reused)``. ``reused`` is what tells the
        caller that the instrument already carried a value from another
        bridge, so the temporality state has to start from that value.
        """
        try:
            return (factory(name, **kwargs, registry=self._registry), False)
        except ValueError:
            existing = self._existing_collector(name)
            if existing is None or not isinstance(existing, factory):
                logger.warning("akosha.otlp_metrics.instrument_conflict name=%s", name)
                return (None, False)
            existing_labels = tuple(getattr(existing, "_labelnames", ()))
            if existing_labels != tuple(kwargs.get("labelnames", ())):
                logger.warning(
                    "akosha.otlp_metrics.label_set_conflict name=%s existing=%s wanted=%s",
                    name,
                    existing_labels,
                    tuple(kwargs.get("labelnames", ())),
                )
                return (None, False)
            logger.debug("akosha.otlp_metrics.reusing_instrument name=%s", name)
            return (existing, True)

    def _existing_collector(self, name: str) -> Any | None:
        """The collector already registered under ``name``, if any."""
        names = getattr(self._registry, "_names_to_collectors", None)
        if not isinstance(names, dict):
            return None
        return names.get(name)

    @staticmethod
    def _instrument_boundaries(instrument: Any) -> tuple[float, ...]:
        """A registered histogram's explicit bucket boundaries.

        ``prometheus_client`` appends the implicit ``+Inf`` bucket to the
        layout it is given, so the instrument's ``_upper_bounds`` carries
        one more entry than the producer's ``explicit_bounds``. The
        trailing infinity is stripped so the two are comparable.
        """
        bounds = tuple(float(bound) for bound in getattr(instrument, "_upper_bounds", ()) or ())
        if bounds and math.isinf(bounds[-1]):
            return bounds[:-1]
        return bounds

    def _histogram_boundaries(self, point: Mapping[str, Any]) -> tuple[float, ...] | None:
        """The explicit bucket boundaries for a histogram point.

        Returns ``None`` when the point cannot be represented — an
        exponential histogram with negative buckets, which a Prometheus
        histogram cannot express (its bounds would have to start below
        zero, and ``prometheus_client`` then omits the count and sum
        samples), or a histogram with no explicit bounds at all.
        """
        buckets = point.get("buckets_json") or {}
        if point.get("data_type") == "exponential_histogram":
            negative = buckets.get("negative") or {}
            if negative.get("bucket_counts"):
                logger.warning(
                    "akosha.otlp_metrics.negative_buckets_unsupported metric=%s",
                    point.get("metric_name"),
                )
                return None
            return self._exponential_boundaries(buckets)
        bounds = buckets.get("explicit_bounds") or []
        try:
            return tuple(float(bound) for bound in bounds)
        except TypeError, ValueError:
            return None

    @staticmethod
    def _exponential_boundaries(buckets: Mapping[str, Any]) -> tuple[float, ...] | None:
        """Explicit bounds for an exponential histogram's positive buckets.

        An exponential histogram's bucket ``i`` covers
        ``[base**(offset+i), base**(offset+i+1))`` with
        ``base = 2**(2**-scale)``. Prometheus histograms are keyed by upper
        bounds, so the boundary of bucket ``i`` is ``base**(offset+i+1)``.
        """
        positive = buckets.get("positive") or {}
        counts = positive.get("bucket_counts") or []
        if not counts:
            return None
        try:
            scale = int(buckets.get("scale", 0))
            offset = int(positive.get("offset", 0))
        except TypeError, ValueError:
            return None
        base = 2.0 ** (2.0**-scale)
        try:
            return tuple(base ** (offset + index + 1) for index in range(len(counts)))
        except OverflowError:
            return None

    def _apply(
        self,
        family: _Family,
        state: dict[str, Any],
        point: Mapping[str, Any],
    ) -> bool:
        """Write one data point into its instrument."""
        if family.kind == "counter":
            return self._apply_counter(family, state, point)
        if family.kind == "gauge":
            return self._apply_gauge(family, state, point)
        if family.kind == "histogram":
            return self._apply_histogram(family, state, point)
        return self._apply_summary(family, state, point)

    def _apply_counter(
        self,
        family: _Family,
        state: dict[str, Any],
        point: Mapping[str, Any],
    ) -> bool:
        """Add a monotonic sum's delta to its Counter."""
        value = float(point.get("value_sum") or 0.0)
        temporality = str(point.get("temporality") or "cumulative")
        child = self._child(family, point)
        if temporality == "delta":
            # A delta point is an increment, not a level: it accumulates.
            child.inc(value)
            state["applied"] = float(child._value.get())
            return True
        delta = self._delta(state, value, temporality)
        current = float(child._value.get())
        applied = float(state.get("applied") or 0.0)
        if current < applied:
            # The instrument was reset behind the bridge's back —
            # ``prometheus_metrics.reset_all_metrics()`` zeroes the shared
            # registry without touching the bridge's temporality state. A
            # delta computed against the pre-reset baseline would leave the
            # counter reading 0 until the producer passed that value again.
            # Re-anchor on the producer's absolute value instead: the
            # counter means "the producer's cumulative total", and that is
            # what it now says.
            child._value.set(value)
            state["last"] = value
            state["applied"] = value
            return True
        child.inc(delta)
        state["applied"] = current + delta
        return True

    def _apply_gauge(
        self,
        family: _Family,
        _state: dict[str, Any],
        point: Mapping[str, Any],
    ) -> bool:
        """Set a gauge (or a non-monotonic sum) to its latest value.

        A gauge is a LEVEL, not a total: there is no temporality to
        reconcile, so the latest value simply wins.
        """
        value = float(point.get("value_sum") or 0.0)
        self._child(family, point).set(value)
        return True

    def _apply_histogram(
        self,
        family: _Family,
        state: dict[str, Any],
        point: Mapping[str, Any],
    ) -> bool:
        """Write a histogram's per-bucket counts into the instrument.

        The instrument's buckets hold the count of the observations that
        fell in that bucket; ``prometheus_client`` accumulates them into
        the cumulative ``le`` values Prometheus renders. Writing the
        per-bucket counts (rather than the cumulative ones OTLP also
        carries) is what makes the rendered ``le`` ladder correct.
        """
        try:
            counts = self._histogram_counts(point)
        except TypeError, ValueError:
            self._drop("bucket_divergence", str(point.get("metric_name")))
            return False
        if family.boundaries is None or len(counts) != len(family.boundaries) + 1:
            self._drop("bucket_divergence", str(point.get("metric_name")))
            return False
        reported_sum = float(point.get("value_sum") or 0.0)
        last_counts = state.get("counts")
        last_sum = float(state.get("sum") or 0.0)
        if str(point.get("temporality") or "cumulative") == "delta" or last_counts is None:
            delta_counts = counts
            delta_sum = reported_sum
        elif sum(counts) < sum(last_counts):
            # The cumulative ladder moved BACKWARDS: the producer restarted
            # and its counters are back near zero. This is the same rule
            # the counter path applies in ``_delta`` — a value that went
            # backwards is re-baselined, not clamped to zero. Clamping
            # would discard this whole interval's observations for ever.
            delta_counts = list(counts)
            delta_sum = reported_sum
        else:
            delta_counts = [
                max(0.0, current - previous)
                for current, previous in zip(counts, last_counts, strict=True)
            ]
            delta_sum = max(0.0, reported_sum - last_sum)
        accumulated = [
            previous + delta
            for previous, delta in zip(
                state.get("accumulated_counts") or [0.0] * len(counts),
                delta_counts,
                strict=True,
            )
        ]
        accumulated_sum = float(state.get("accumulated_sum") or 0.0) + delta_sum
        state["counts"] = list(counts)
        state["sum"] = reported_sum
        state["accumulated_counts"] = accumulated
        state["accumulated_sum"] = accumulated_sum
        child = self._child(family, point)
        for index, value in enumerate(accumulated):
            child._buckets[index].set(value)
        child._sum.set(accumulated_sum)
        return True

    def _apply_summary(
        self,
        family: _Family,
        state: dict[str, Any],
        point: Mapping[str, Any],
    ) -> bool:
        """Write a summary's count and sum into the instrument.

        ``prometheus_client``'s ``Summary`` renders a count and a sum and
        has no quantile support, so an OTLP summary's quantile values are
        not exposed. The substrate's summary row keeps the count and the
        sum for the same reason: its schema has no column for quantiles.
        """
        count = float(point.get("value_count") or 0.0)
        total = float(point.get("value_sum") or 0.0)
        last_count = state.get("count")
        last_sum = float(state.get("sum") or 0.0)
        if str(point.get("temporality") or "cumulative") == "delta" or last_count is None:
            delta_count, delta_sum = count, total
        else:
            delta_count = max(0.0, count - float(last_count))
            delta_sum = max(0.0, total - last_sum)
        state["count"] = count
        state["sum"] = total
        child = self._child(family, point)
        child._count.inc(delta_count)
        child._sum.inc(delta_sum)
        return True

    @staticmethod
    def _histogram_counts(point: Mapping[str, Any]) -> list[float]:
        """The per-bucket counts of a histogram point.

        An explicit histogram carries ``bucket_counts`` directly. An
        exponential histogram carries its counts under ``positive``, with
        the zero bucket folded into the FIRST explicit bucket: a zero
        falls below the lowest positive boundary, and a Prometheus
        histogram has exactly one bucket per boundary plus an implicit
        ``+Inf`` overflow bucket.
        """
        buckets = point.get("buckets_json") or {}
        if point.get("data_type") == "exponential_histogram":
            positive = buckets.get("positive") or {}
            counts = [float(count) for count in (positive.get("bucket_counts") or [])]
            if not counts:
                return []
            counts[0] += float(buckets.get("zero_count") or 0.0)
            counts.append(0.0)
            return counts
        return [float(count) for count in (buckets.get("bucket_counts") or [])]

    def _child(self, family: _Family, point: Mapping[str, Any]) -> Any:
        """The instrument's child for this point's label values.

        A metric with no attributes has no labels, and
        ``prometheus_client`` refuses ``labels()`` on an instrument that
        was constructed without a label set — for such a metric the child
        IS the instrument.
        """
        if not family.labelnames:
            return family.instrument
        return family.instrument.labels(**self._label_kwargs(family, point))

    def _seed_adopted_baseline(
        self,
        family: _Family,
        point: Mapping[str, Any],
        state: dict[str, Any],
    ) -> None:
        """Start the temporality state from an ADOPTED instrument's value.

        A second bridge in the same process reuses the collector the first
        one registered, and that collector already holds a value. Starting
        the temporality state from zero would make the first cumulative
        point look like a fresh baseline and be ADDED on top of the value
        the previous receiver left behind — 100 becoming 200. Reading the
        instrument's current value as the baseline is the same
        "re-baseline instead of adding a stale delta" rule the class
        docstring describes.
        """
        child = self._child(family, point)
        if family.kind == "counter":
            current = float(child._value.get())
            state["last"] = current
            state["applied"] = current
        elif family.kind == "histogram":
            counts = [float(bucket.get()) for bucket in child._buckets]
            total = float(child._sum.get())
            state["counts"] = counts
            state["sum"] = total
            state["accumulated_counts"] = list(counts)
            state["accumulated_sum"] = total
        elif family.kind == "summary":
            state["count"] = float(child._count.get())
            state["sum"] = float(child._sum.get())

    def _delta(self, state: dict[str, Any], value: float, temporality: Any) -> float:
        """The increment a cumulative or delta value contributes.

        A cumulative value is an absolute total, so only the difference
        since the previous export may be added. A value that went
        *backwards* is a producer restart: the whole value is the increment
        from zero, which is also what a Prometheus counter reset means.
        """
        last = state.get("last")
        if str(temporality) == "delta" or last is None:
            delta = value
        elif value >= float(last):
            delta = value - float(last)
        else:
            delta = value
        state["last"] = value
        return delta

    @staticmethod
    def _point_identity(point: Mapping[str, Any]) -> tuple[int, str]:
        """The substrate identity of a point: its instant and its attributes.

        This is the same identity the substrate dedupes on — the series
        plus the instant, with the attributes as a function of their
        content alone — so the bridge's replay guard and the substrate's
        dedupe agree about what "the same point twice" means.
        """
        timestamp = int(point.get("ts") or 0)
        attributes = point.get("attributes") or {}
        return (timestamp, json.dumps(attributes, sort_keys=True, default=str))

    def _labels_of(self, point: Mapping[str, Any]) -> dict[str, str]:
        """The point's attributes as Prometheus label values."""
        attributes = point.get("attributes") or {}
        return {
            sanitize_label_name(str(key)): label_value(value) for key, value in attributes.items()
        }

    def _label_kwargs(self, family: _Family, point: Mapping[str, Any]) -> dict[str, str]:
        """The label values for this point, in the family's label order."""
        labels = self._labels_of(point)
        return {label: labels.get(label, "") for label in family.labelnames}

    def _record_resource(self, point: Mapping[str, Any]) -> str:
        """Publish ``target_info`` and ``otel_scope_info`` for the point.

        Both are the OpenTelemetry Collector's resource and scope
        conversions, and both are what makes a series attributable:
        ``target_info`` says which service a series came from,
        ``otel_scope_info`` which instrumentation scope produced it.

        Returns the drop reason when a cap was reached, and ``""``
        otherwise. The caps exist because these two gauges are keyed by
        the producer's resource and scope: without them, every distinct
        resource instance would mint a permanent ``target_info`` series.
        """
        resource = point.get("resource") or {}
        target_values = self._target_values(resource)
        target_key = tuple(target_values.get(label, "") for label in TARGET_INFO_LABELS)
        scope_key = (
            str(point.get("scope_name") or ""),
            str(point.get("scope_version") or ""),
            str(point.get("service_name") or ""),
        )
        if (
            target_key not in self._target_info_keys
            and len(self._target_info_keys) >= self._max_target_info
        ):
            return "target_info_cap"
        if (
            scope_key not in self._scope_info_keys
            and len(self._scope_info_keys) >= self._max_scope_info
        ):
            return "scope_info_cap"

        self._target_info_keys.add(target_key)
        self._scope_info_keys.add(scope_key)

        if self._target_info is None:
            self._target_info = self._register(
                Gauge,
                "target_info",
                documentation="Resource metadata of the producer, as labels. Always 1.",
                labelnames=list(TARGET_INFO_LABELS),
            )[0]
        if self._target_info is not None:
            self._target_info.labels(**dict(zip(TARGET_INFO_LABELS, target_key, strict=True))).set(
                1
            )

        if self._scope_info is None:
            self._scope_info = self._register(
                Gauge,
                "otel_scope_info",
                documentation=(
                    "Instrumentation scope metadata of the producer, as labels. Always 1."
                ),
                labelnames=list(SCOPE_INFO_LABELS),
            )[0]
        if self._scope_info is not None:
            self._scope_info.labels(
                otel_scope_name=scope_key[0],
                otel_scope_version=scope_key[1],
                service_name=scope_key[2],
            ).set(1)
        return ""

    @staticmethod
    def _target_values(resource: Mapping[str, Any]) -> dict[str, str]:
        """The resource's attributes as ``target_info`` label values.

        The mapping is ORDERED and the first attribute that carries a
        non-empty value wins the label. Two attributes map onto
        ``deployment_environment`` — ``deployment.environment`` and
        ``deployment.environment.name`` — and a producer sends one of them
        or the other. Keying the result by LABEL would let the absent
        attribute overwrite the present one with an empty string, which is
        what oneiric's ``deployment.environment`` used to hit.
        """
        values: dict[str, str] = {}
        for attribute, label in _RESOURCE_TO_TARGET_LABEL.items():
            value = label_value(resource.get(attribute, ""))
            if value and not values.get(label):
                values[label] = value
        return values

    def _drop(self, reason: str, metric_name: str) -> None:
        """Record that a point was not exposed."""
        self.dropped_by_reason[reason] = self.dropped_by_reason.get(reason, 0) + 1
        logger.warning(
            "akosha.otlp_metrics.exposure_dropped reason=%s metric=%s", reason, metric_name
        )


__all__ = [
    "MAX_METRIC_FAMILIES",
    "MAX_SCOPE_INFO_SERIES",
    "MAX_SERIES_PER_METRIC",
    "MAX_TARGET_INFO_SERIES",
    "RESERVED_LABEL_NAMES",
    "SCOPE_INFO_LABELS",
    "TARGET_INFO_LABELS",
    "OTLPPrometheusBridge",
    "label_signature",
    "label_value",
    "sanitize_label_name",
    "sanitize_name",
    "series_name",
]
