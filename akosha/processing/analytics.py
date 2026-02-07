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

from akosha.observability import add_span_attributes, record_counter, record_histogram, traced

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

        add_span_attributes(
            {
                "analytics.metric_name": metric_name,
                "analytics.system_id": system_id,
                "analytics.value": str(value),
            }
        )

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
        add_span_attributes(
            {
                "analytics.metric_name": metric_name,
                "analytics.system_id": system_id or "all",
                "analytics.time_window_hours": str(time_window.total_seconds() / 3600),
            }
        )

        # Filter data points
        points = self._metrics_cache.get(metric_name, [])

        cutoff_time = datetime.now(UTC) - time_window
        filtered = [
            p
            for p in points
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
        percent_change = ((values[-1] - values[0]) / values[0]) * 100 if values[0] != 0 else 0.0

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
        add_span_attributes(
            {
                "analytics.metric_name": metric_name,
                "analytics.system_id": system_id or "all",
                "analytics.threshold_std": str(threshold_std),
            }
        )

        # Filter data points
        points = self._metrics_cache.get(metric_name, [])

        cutoff_time = datetime.now(UTC) - time_window
        filtered = [
            p
            for p in points
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
                anomalies.append(
                    {
                        "timestamp": point.timestamp.isoformat(),
                        "value": point.value,
                        "system_id": point.system_id,
                        "z_score": float(z_score),
                        "deviation": float(point.value - mean),
                        "metadata": point.metadata,
                    }
                )

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
        add_span_attributes(
            {
                "analytics.metric_name": metric_name,
                "analytics.time_window_hours": str(time_window.total_seconds() / 3600),
            }
        )

        # Step 1: Filter data by time window
        filtered = self._filter_data_by_time_window(metric_name, time_window)
        if filtered is None:
            return None

        # Step 2: Group and validate systems
        system_data = self._group_data_by_system(filtered)
        systems = self._get_systems_with_sufficient_data(system_data)
        if systems is None:
            return None

        add_span_attributes(
            {
                "analytics.system_count": str(len(systems)),
            }
        )

        # Step 3: Align time series data
        aligned_data = self._align_time_series(systems, system_data)

        # Step 4: Compute correlation matrix
        sys_list = list(aligned_data.keys())
        correlation_matrix = self._compute_correlation_matrix(aligned_data, sys_list)

        # Step 5: Extract significant correlations
        system_pairs = self._extract_significant_correlations(sys_list, correlation_matrix)

        # Step 6: Build result
        time_range = self._compute_time_range(filtered)

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

    def _filter_data_by_time_window(
        self, metric_name: str, time_window: timedelta
    ) -> list[DataPoint] | None:
        """Filter metric data points by time window.

        Args:
            metric_name: Name of metric to filter
            time_window: Time window to filter by

        Returns:
            Filtered data points or None if insufficient data
        """
        points = self._metrics_cache.get(metric_name, [])
        cutoff_time = datetime.now(UTC) - time_window
        filtered = [p for p in points if p.timestamp >= cutoff_time]

        if len(filtered) < 10:
            logger.warning(f"Insufficient data for correlation analysis: {metric_name}")
            record_counter("analytics.correlation.failed", 1, {"reason": "insufficient_data"})
            return None

        return filtered

    def _group_data_by_system(
        self, filtered: list[DataPoint]
    ) -> dict[str, list[tuple[datetime, float]]]:
        """Group filtered data points by system ID.

        Args:
            filtered: List of filtered data points

        Returns:
            Dictionary mapping system_id to list of (timestamp, value) tuples
        """
        system_data: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
        for point in filtered:
            hour_key = point.timestamp.replace(minute=0, second=0, microsecond=0)
            system_data[point.system_id].append((hour_key, point.value))
        return system_data

    def _get_systems_with_sufficient_data(
        self, system_data: dict[str, list[tuple[datetime, float]]]
    ) -> list[str] | None:
        """Get list of systems with sufficient data points.

        Args:
            system_data: Dictionary mapping system_id to data points

        Returns:
            List of system IDs with sufficient data, or None if < 2 systems
        """
        systems = [sys_id for sys_id, data in system_data.items() if len(data) >= 5]

        if len(systems) < 2:
            logger.warning("Need at least 2 systems for correlation")
            record_counter("analytics.correlation.failed", 1, {"reason": "insufficient_systems"})
            return None

        return systems

    def _align_time_series(
        self,
        systems: list[str],
        system_data: dict[str, list[tuple[datetime, float]]],
    ) -> dict[str, list[float]]:
        """Align time series data for correlation analysis.

        Args:
            systems: List of system IDs to align
            system_data: Dictionary mapping system_id to data points

        Returns:
            Dictionary mapping system_id to aligned value lists
        """
        aligned_data: dict[str, list[float]] = {}
        for system_id in systems:
            data = system_data[system_id]
            values = [v for _, v in data]
            max_values = 100
            aligned_data[system_id] = values[-min(len(values), max_values) :]
        return aligned_data

    def _compute_correlation_matrix(
        self, aligned_data: dict[str, list[float]], sys_list: list[str]
    ) -> npt.NDArray[np.float64]:
        """Compute correlation matrix for all system pairs.

        Args:
            aligned_data: Dictionary mapping system_id to aligned values
            sys_list: List of system IDs

        Returns:
            NxN correlation matrix
        """
        n = len(sys_list)
        correlation_matrix = np.eye(n)

        for i in range(n):
            for j in range(i + 1, n):
                corr = self._calculate_pairwise_correlation(
                    aligned_data[sys_list[i]], aligned_data[sys_list[j]]
                )
                correlation_matrix[i, j] = corr
                correlation_matrix[j, i] = corr

        return correlation_matrix

    def _calculate_pairwise_correlation(self, vals_i: list[float], vals_j: list[float]) -> float:
        """Calculate Pearson correlation between two value lists.

        Args:
            vals_i: First value list
            vals_j: Second value list

        Returns:
            Correlation coefficient (0.0 if insufficient data or zero variance)
        """
        min_len = min(len(vals_i), len(vals_j))

        if min_len < 5:
            return 0.0

        arr_i = np.array(vals_i[-min_len:])
        arr_j = np.array(vals_j[-min_len:])

        if np.std(arr_i) > 0 and np.std(arr_j) > 0:
            corr_matrix = np.corrcoef(arr_i, arr_j)
            return float(corr_matrix[0, 1])

        return 0.0

    def _extract_significant_correlations(
        self,
        sys_list: list[str],
        correlation_matrix: npt.NDArray[np.float64],
        threshold: float = 0.5,
    ) -> list[dict[str, Any]]:
        """Extract system pairs with significant correlations.

        Args:
            sys_list: List of system IDs
            correlation_matrix: NxN correlation matrix
            threshold: Minimum absolute correlation for significance

        Returns:
            List of correlation info dictionaries
        """
        system_pairs = []
        n = len(sys_list)

        for i in range(n):
            for j in range(i + 1, n):
                corr = float(correlation_matrix[i, j])
                if abs(corr) > threshold:
                    system_pairs.append(
                        {
                            "system_1": sys_list[i],
                            "system_2": sys_list[j],
                            "correlation": corr,
                            "strength": "strong" if abs(corr) > 0.7 else "moderate",
                        }
                    )

        return system_pairs

    def _compute_time_range(self, filtered: list[DataPoint]) -> tuple[datetime, datetime]:
        """Compute time range from filtered data points.

        Args:
            filtered: List of filtered data points

        Returns:
            Tuple of (earliest_timestamp, latest_timestamp)
        """
        return (
            min(p.timestamp for p in filtered),
            max(p.timestamp for p in filtered),
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
