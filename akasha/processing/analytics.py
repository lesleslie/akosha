"""Time-series analytics for trend detection and cross-system insights.

This module provides analytics capabilities for detecting patterns, anomalies,
and correlations across multiple Session-Buddy systems.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import numpy.typing as npt

from akasha.observability import add_span_attributes, record_counter, record_histogram, traced

logger = logging.getLogger(__name__)


@dataclass
class DataPoint:
    """Single data point in time series."""

    timestamp: datetime
    value: float
    system_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TrendAnalysis:
    """Results of trend analysis."""

    metric_name: str
    trend_direction: str  # "increasing", "decreasing", "stable"
    trend_strength: float  # 0 to 1, higher = stronger trend
    percent_change: float
    confidence: float  # 0 to 1
    time_range: tuple[datetime, datetime]


@dataclass
class AnomalyDetection:
    """Results of anomaly detection."""

    metric_name: str
    anomalies: list[dict[str, Any]]
    threshold: float
    total_points: int
    anomaly_count: int
    anomaly_rate: float


@dataclass
class CorrelationResult:
    """Results of cross-system correlation analysis."""

    metric_name: str
    system_pairs: list[dict[str, Any]]
    correlation_matrix: npt.NDArray[np.float64]
    systems: list[str]
    time_range: tuple[datetime, datetime]


class TimeSeriesAnalytics:
    """Time-series analytics for cross-system intelligence.

    Provides:
    - Trend detection (increasing/decreasing/stable)
    - Anomaly detection (statistical outliers)
    - Cross-system correlation analysis
    - Pattern discovery
    """

    def __init__(self) -> None:
        """Initialize analytics service."""
        self._metrics_cache: dict[str, list[DataPoint]] = defaultdict(list)
        logger.info("Time-series analytics service initialized")

    @traced("analytics_add_metric")
    async def add_metric(
        self,
        metric_name: str,
        value: float,
        system_id: str,
        timestamp: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Add metric data point.

        Args:
            metric_name: Name of metric (e.g., "conversation_count", "quality_score")
            value: Metric value
            system_id: System identifier
            timestamp: Timestamp (defaults to now)
            metadata: Additional metadata
        """
        if timestamp is None:
            timestamp = datetime.now(UTC)

        add_span_attributes({
            "analytics.metric_name": metric_name,
            "analytics.system_id": system_id,
            "analytics.value": str(value),
        })

        point = DataPoint(
            timestamp=timestamp,
            value=value,
            system_id=system_id,
            metadata=metadata or {},
        )

        self._metrics_cache[metric_name].append(point)

        record_counter("analytics.metrics.added", 1, {"metric_name": metric_name})
        record_histogram("analytics.metric.value", value, {"metric_name": metric_name})

        logger.debug(f"Added metric: {metric_name}={value} for system={system_id}")

    @traced("analytics_analyze_trend")
    async def analyze_trend(
        self,
        metric_name: str,
        system_id: str | None = None,
        time_window: timedelta = timedelta(days=7),
    ) -> TrendAnalysis | None:
        """Analyze trend for a metric over time.

        Args:
            metric_name: Name of metric
            system_id: Optional system filter
            time_window: Time window for analysis

        Returns:
            Trend analysis results or None if insufficient data
        """
        add_span_attributes({
            "analytics.metric_name": metric_name,
            "analytics.system_id": system_id or "all",
            "analytics.time_window_hours": str(time_window.total_seconds() / 3600),
        })

        # Filter data points
        points = self._metrics_cache.get(metric_name, [])

        cutoff_time = datetime.now(UTC) - time_window
        filtered = [
            p for p in points
            if p.timestamp >= cutoff_time and (system_id is None or p.system_id == system_id)
        ]

        if len(filtered) < 2:
            logger.warning(f"Insufficient data for trend analysis: {metric_name}")
            record_counter("analytics.trend.failed", 1, {"reason": "insufficient_data"})
            return None

        # Sort by timestamp
        filtered.sort(key=lambda p: p.timestamp)

        # Extract values
        values = np.array([p.value for p in filtered])

        # Compute linear regression for trend
        x = np.arange(len(values))
        coeffs = np.polyfit(x, values, 1)
        slope = coeffs[0]

        # Determine trend direction
        if slope > 0.01:
            trend_direction = "increasing"
        elif slope < -0.01:
            trend_direction = "decreasing"
        else:
            trend_direction = "stable"

        # Calculate trend strength (R²)
        p = np.poly1d(coeffs)
        y_hat = p(x)
        y_bar = np.mean(values)
        ss_tot = np.sum((values - y_bar) ** 2)
        ss_res = np.sum((values - y_hat) ** 2)
        r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
        trend_strength = float(r_squared)

        # Calculate percent change
        percent_change = (
            ((values[-1] - values[0]) / values[0]) * 100 if values[0] != 0 else 0.0
        )

        # Confidence based on data point count
        confidence = min(1.0, len(filtered) / 100)  # Max confidence at 100 points

        time_range = (filtered[0].timestamp, filtered[-1].timestamp)

        record_histogram("analytics.trend.strength", trend_strength, {"direction": trend_direction})
        record_histogram("analytics.trend.confidence", confidence)
        record_counter("analytics.trend.completed", 1, {"direction": trend_direction})

        logger.info(
            f"Trend analysis for {metric_name}: {trend_direction} "
            f"(strength={trend_strength:.2f}, change={percent_change:.1f}%)"
        )

        return TrendAnalysis(
            metric_name=metric_name,
            trend_direction=trend_direction,
            trend_strength=trend_strength,
            percent_change=percent_change,
            confidence=confidence,
            time_range=time_range,
        )

    @traced("analytics_detect_anomalies")
    async def detect_anomalies(
        self,
        metric_name: str,
        system_id: str | None = None,
        time_window: timedelta = timedelta(days=7),
        threshold_std: float = 3.0,
    ) -> AnomalyDetection | None:
        """Detect statistical anomalies in metric data.

        Args:
            metric_name: Name of metric
            system_id: Optional system filter
            time_window: Time window for analysis
            threshold_std: Standard deviation threshold for anomalies

        Returns:
            Anomaly detection results or None if insufficient data
        """
        add_span_attributes({
            "analytics.metric_name": metric_name,
            "analytics.system_id": system_id or "all",
            "analytics.threshold_std": str(threshold_std),
        })

        # Filter data points
        points = self._metrics_cache.get(metric_name, [])

        cutoff_time = datetime.now(UTC) - time_window
        filtered = [
            p for p in points
            if p.timestamp >= cutoff_time and (system_id is None or p.system_id == system_id)
        ]

        if len(filtered) < 10:
            logger.warning(f"Insufficient data for anomaly detection: {metric_name}")
            record_counter("analytics.anomaly.failed", 1, {"reason": "insufficient_data"})
            return None

        # Extract values
        values = np.array([p.value for p in filtered])

        # Compute statistics
        mean = np.mean(values)
        std = np.std(values)

        # Detect anomalies (values beyond threshold_std standard deviations)
        anomalies = []
        for point in filtered:
            z_score = abs((point.value - mean) / std) if std > 0 else 0
            if z_score > threshold_std:
                anomalies.append({
                    "timestamp": point.timestamp.isoformat(),
                    "value": point.value,
                    "system_id": point.system_id,
                    "z_score": float(z_score),
                    "deviation": float(point.value - mean),
                    "metadata": point.metadata,
                })

        anomaly_rate = len(anomalies) / len(filtered)

        record_histogram("analytics.anomaly.rate", anomaly_rate, {"metric_name": metric_name})
        record_counter("analytics.anomaly.detected", len(anomalies), {"metric_name": metric_name})

        logger.info(
            f"Anomaly detection for {metric_name}: "
            f"{len(anomalies)} anomalies detected ({anomaly_rate:.1%})"
        )

        return AnomalyDetection(
            metric_name=metric_name,
            anomalies=anomalies,
            threshold=threshold_std,
            total_points=len(filtered),
            anomaly_count=len(anomalies),
            anomaly_rate=anomaly_rate,
        )

    @traced("analytics_correlate_systems")
    async def correlate_systems(
        self,
        metric_name: str,
        time_window: timedelta = timedelta(days=7),
    ) -> CorrelationResult | None:
        """Analyze correlations between systems for a metric.

        Args:
            metric_name: Name of metric
            time_window: Time window for analysis

        Returns:
            Correlation analysis results or None if insufficient data
        """
        add_span_attributes({
            "analytics.metric_name": metric_name,
            "analytics.time_window_hours": str(time_window.total_seconds() / 3600),
        })

        # Filter data points
        points = self._metrics_cache.get(metric_name, [])

        cutoff_time = datetime.now(UTC) - time_window
        filtered = [p for p in points if p.timestamp >= cutoff_time]

        if len(filtered) < 10:
            logger.warning(f"Insufficient data for correlation analysis: {metric_name}")
            record_counter("analytics.correlation.failed", 1, {"reason": "insufficient_data"})
            return None

        # Group by system
        system_data: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
        for point in filtered:
            # Round timestamp to nearest hour for alignment
            hour_key = point.timestamp.replace(minute=0, second=0, microsecond=0)
            system_data[point.system_id].append((hour_key, point.value))

        # Get systems with sufficient data
        systems = [sys for sys, data in system_data.items() if len(data) >= 5]

        if len(systems) < 2:
            logger.warning(f"Need at least 2 systems for correlation: {metric_name}")
            record_counter("analytics.correlation.failed", 1, {"reason": "insufficient_systems"})
            return None

        add_span_attributes({
            "analytics.system_count": str(len(systems)),
        })

        # Build aligned time series
        # (simplified - in production, use proper time series alignment)
        aligned_data: dict[str, list[float]] = {}
        for system_id in systems:
            data = system_data[system_id]
            # Take last N values for alignment
            values = [v for _, v in data]
            aligned_data[system_id] = values[-min(len(values), 100):]

        # Compute correlation matrix
        sys_list = list(aligned_data.keys())
        n = len(sys_list)
        correlation_matrix = np.eye(n)

        for i in range(n):
            for j in range(i + 1, n):
                # Align lengths
                vals_i = aligned_data[sys_list[i]]
                vals_j = aligned_data[sys_list[j]]
                min_len = min(len(vals_i), len(vals_j))

                if min_len < 5:
                    corr = 0.0
                else:
                    arr_i = np.array(vals_i[-min_len:])
                    arr_j = np.array(vals_j[-min_len:])

                    # Compute Pearson correlation
                    if np.std(arr_i) > 0 and np.std(arr_j) > 0:
                        corr_matrix = np.corrcoef(arr_i, arr_j)
                        corr = float(corr_matrix[0, 1])
                    else:
                        corr = 0.0

                correlation_matrix[i, j] = corr
                correlation_matrix[j, i] = corr

        # Extract significant correlations
        system_pairs = []
        for i in range(n):
            for j in range(i + 1, n):
                corr = float(correlation_matrix[i, j])
                if abs(corr) > 0.5:  # Significant correlation threshold
                    system_pairs.append({
                        "system_1": sys_list[i],
                        "system_2": sys_list[j],
                        "correlation": corr,
                        "strength": "strong" if abs(corr) > 0.7 else "moderate",
                    })

        time_range = (
            min(p.timestamp for p in filtered),
            max(p.timestamp for p in filtered),
        )

        record_histogram("analytics.correlation.count", len(system_pairs))
        record_counter("analytics.correlation.completed", 1)

        logger.info(
            f"Correlation analysis for {metric_name}: "
            f"{len(system_pairs)} significant correlations found"
        )

        return CorrelationResult(
            metric_name=metric_name,
            system_pairs=system_pairs,
            correlation_matrix=correlation_matrix,
            systems=sys_list,
            time_range=time_range,
        )

    def get_metric_names(self) -> list[str]:
        """Get list of all tracked metrics.

        Returns:
            List of metric names
        """
        return list(self._metrics_cache.keys())

    def get_system_count(self, metric_name: str) -> int:
        """Get number of systems reporting a metric.

        Args:
            metric_name: Name of metric

        Returns:
            Number of unique systems
        """
        systems = {p.system_id for p in self._metrics_cache.get(metric_name, [])}
        return len(systems)
