"""Tests for time-series analytics service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from akasha.processing.analytics import (
    AnomalyDetection,
    CorrelationResult,
    DataPoint,
    TimeSeriesAnalytics,
    TrendAnalysis,
)


class TestTimeSeriesAnalytics:
    """Test suite for TimeSeriesAnalytics."""

    @pytest.fixture
    def analytics(self) -> TimeSeriesAnalytics:
        """Create fresh analytics service for each test."""
        return TimeSeriesAnalytics()

    @pytest.mark.asyncio
    async def test_add_metric(self, analytics: TimeSeriesAnalytics) -> None:
        """Test adding metric data points."""
        await analytics.add_metric(
            metric_name="conversation_count",
            value=42.0,
            system_id="system-1",
        )

        names = analytics.get_metric_names()
        assert "conversation_count" in names

        count = analytics.get_system_count("conversation_count")
        assert count == 1

    @pytest.mark.asyncio
    async def test_add_multiple_metrics(self, analytics: TimeSeriesAnalytics) -> None:
        """Test adding multiple data points."""
        now = datetime.now(UTC)

        for i in range(10):
            await analytics.add_metric(
                metric_name="quality_score",
                value=75.0 + i,
                system_id="system-1",
                timestamp=now + timedelta(hours=i),
            )

        count = analytics.get_system_count("quality_score")
        assert count == 1

    @pytest.mark.asyncio
    async def test_analyze_trend_increasing(self, analytics: TimeSeriesAnalytics) -> None:
        """Test trend analysis with increasing values."""
        now = datetime.now(UTC)

        # Add increasing trend
        for i in range(20):
            await analytics.add_metric(
                metric_name="metric1",
                value=10.0 + i * 0.5,  # Linearly increasing
                system_id="system-1",
                timestamp=now + timedelta(hours=i),
            )

        trend = await analytics.analyze_trend("metric1", system_id="system-1")

        assert trend is not None
        assert trend.metric_name == "metric1"
        assert trend.trend_direction == "increasing"
        assert trend.trend_strength > 0.5  # Should be strong trend
        assert trend.percent_change > 0

    @pytest.mark.asyncio
    async def test_analyze_trend_decreasing(self, analytics: TimeSeriesAnalytics) -> None:
        """Test trend analysis with decreasing values."""
        now = datetime.now(UTC)

        # Add decreasing trend
        for i in range(20):
            await analytics.add_metric(
                metric_name="metric2",
                value=100.0 - i * 2.0,  # Linearly decreasing
                system_id="system-1",
                timestamp=now + timedelta(hours=i),
            )

        trend = await analytics.analyze_trend("metric2", system_id="system-1")

        assert trend is not None
        assert trend.trend_direction == "decreasing"
        assert trend.percent_change < 0

    @pytest.mark.asyncio
    async def test_analyze_trend_stable(self, analytics: TimeSeriesAnalytics) -> None:
        """Test trend analysis with stable values."""
        now = datetime.now(UTC)

        # Add stable values with small fluctuations
        for i in range(20):
            await analytics.add_metric(
                metric_name="metric3",
                value=50.0 + (i % 3 - 1),  # Oscillates around 50
                system_id="system-1",
                timestamp=now + timedelta(hours=i),
            )

        trend = await analytics.analyze_trend("metric3", system_id="system-1")

        assert trend is not None
        assert trend.trend_direction == "stable"
        assert abs(trend.percent_change) < 10  # Small change

    @pytest.mark.asyncio
    async def test_analyze_trend_insufficient_data(self, analytics: TimeSeriesAnalytics) -> None:
        """Test trend analysis with insufficient data."""
        await analytics.add_metric("metric4", 42.0, "system-1")

        trend = await analytics.analyze_trend("metric4")

        assert trend is None

    @pytest.mark.asyncio
    async def test_detect_anomalies(self, analytics: TimeSeriesAnalytics) -> None:
        """Test anomaly detection."""
        now = datetime.now(UTC)

        # Add normal data
        for i in range(20):
            await analytics.add_metric(
                metric_name="metric5",
                value=50.0 + np.random.randn() * 5,  # Normal around 50
                system_id="system-1",
                timestamp=now + timedelta(hours=i),
            )

        # Add anomalies
        await analytics.add_metric(
            metric_name="metric5",
            value=150.0,  # Extreme outlier
            system_id="system-1",
            timestamp=now + timedelta(hours=21),
        )

        await analytics.add_metric(
            metric_name="metric5",
            value=-30.0,  # Extreme outlier
            system_id="system-1",
            timestamp=now + timedelta(hours=22),
        )

        anomalies = await analytics.detect_anomalies(
            "metric5",
            system_id="system-1",
            threshold_std=2.5,
        )

        assert anomalies is not None
        assert anomalies.metric_name == "metric5"
        assert anomalies.anomaly_count >= 2  # Should detect at least the extreme values
        assert anomalies.anomaly_rate > 0

    @pytest.mark.asyncio
    async def test_detect_anomalies_insufficient_data(self, analytics: TimeSeriesAnalytics) -> None:
        """Test anomaly detection with insufficient data."""
        for i in range(5):
            await analytics.add_metric("metric6", float(i), "system-1")

        anomalies = await analytics.detect_anomalies("metric6")

        assert anomalies is None

    @pytest.mark.asyncio
    async def test_correlate_systems(self, analytics: TimeSeriesAnalytics) -> None:
        """Test cross-system correlation analysis."""
        now = datetime.now(UTC)

        # Add correlated data for two systems
        for i in range(20):
            base_value = 50.0 + i

            await analytics.add_metric(
                metric_name="metric7",
                value=base_value,
                system_id="system-1",
                timestamp=now + timedelta(hours=i),
            )

            await analytics.add_metric(
                metric_name="metric7",
                value=base_value + 5,  # Offset but correlated
                system_id="system-2",
                timestamp=now + timedelta(hours=i),
            )

        correlation = await analytics.correlate_systems("metric7")

        assert correlation is not None
        assert correlation.metric_name == "metric7"
        assert len(correlation.systems) >= 2
        assert len(correlation.system_pairs) > 0

        # Check for positive correlation between system-1 and system-2
        pair_found = False
        for pair in correlation.system_pairs:
            if (
                pair["system_1"] == "system-1" and pair["system_2"] == "system-2"
            ) or (
                pair["system_1"] == "system-2" and pair["system_2"] == "system-1"
            ):
                pair_found = True
                assert pair["correlation"] > 0.5  # Should be positively correlated
                break

        assert pair_found, "system-1 and system-2 pair not found"

    @pytest.mark.asyncio
    async def test_correlate_systems_insufficient_data(self, analytics: TimeSeriesAnalytics) -> None:
        """Test correlation with insufficient data."""
        await analytics.add_metric("metric8", 42.0, "system-1")

        correlation = await analytics.correlate_systems("metric8")

        assert correlation is None

    @pytest.mark.asyncio
    async def test_correlate_systems_single_system(self, analytics: TimeSeriesAnalytics) -> None:
        """Test correlation with only one system."""
        now = datetime.now(UTC)

        for i in range(15):
            await analytics.add_metric(
                metric_name="metric9",
                value=float(i),
                system_id="system-1",
                timestamp=now + timedelta(hours=i),
            )

        correlation = await analytics.correlate_systems("metric9")

        assert correlation is None  # Need at least 2 systems

    @pytest.mark.asyncio
    async def test_time_window_filtering(self, analytics: TimeSeriesAnalytics) -> None:
        """Test that time windows correctly filter data."""
        now = datetime.now(UTC)

        # Add old data
        await analytics.add_metric(
            "metric10",
            10.0,
            "system-1",
            timestamp=now - timedelta(days=10),
        )

        # Add recent data in chronological order
        for i in range(10):
            await analytics.add_metric(
                "metric10",
                20.0 + i,  # Increasing values
                system_id="system-1",
                timestamp=now - timedelta(days=9-i),  # From 9 days ago to now
            )

        # Analyze with 7-day window
        trend = await analytics.analyze_trend(
            "metric10",
            system_id="system-1",
            time_window=timedelta(days=7),
        )

        assert trend is not None
        # Trend should be based on recent data (increasing)
        assert trend.trend_direction == "increasing"

    @pytest.mark.asyncio
    async def test_system_filtering(self, analytics: TimeSeriesAnalytics) -> None:
        """Test filtering by system_id."""
        now = datetime.now(UTC)

        # Add data for multiple systems
        for i in range(10):
            await analytics.add_metric(
                "metric11",
                10.0 + i,  # Increasing
                system_id="system-1",
                timestamp=now + timedelta(hours=i),
            )

            await analytics.add_metric(
                "metric11",
                100.0 - i,  # Decreasing
                system_id="system-2",
                timestamp=now + timedelta(hours=i),
            )

        # Analyze only system-1
        trend = await analytics.analyze_trend("metric11", system_id="system-1")

        assert trend is not None
        assert trend.trend_direction == "increasing"

        # Analyze only system-2
        trend = await analytics.analyze_trend("metric11", system_id="system-2")

        assert trend is not None
        assert trend.trend_direction == "decreasing"

    @pytest.mark.asyncio
    async def test_multiple_metrics(self, analytics: TimeSeriesAnalytics) -> None:
        """Test tracking multiple different metrics."""
        await analytics.add_metric("metric_a", 1.0, "system-1")
        await analytics.add_metric("metric_b", 2.0, "system-1")
        await analytics.add_metric("metric_c", 3.0, "system-1")

        names = analytics.get_metric_names()

        assert len(names) == 3
        assert "metric_a" in names
        assert "metric_b" in names
        assert "metric_c" in names
