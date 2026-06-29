"""Tests for ChangePointAnalytics — pytrendy-backed structural break detection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from akosha.processing.analytics import ChangePointAnalytics, ChangePointResult, TrendSegment


def _make_dhara(records: list[dict]) -> AsyncMock:
    """Build a mock Dhara client that returns given records from query_time_series_async."""
    dhara = AsyncMock()
    dhara.query_time_series_async = AsyncMock(return_value=records)
    return dhara


def _make_records(values: list[float], base: datetime | None = None) -> list[dict]:
    """Build Dhara time-series records with ts + value fields."""
    base = base or datetime(2026, 1, 1, tzinfo=UTC)
    return [
        {"ts": (base + timedelta(days=i)).isoformat(), "value": v}
        for i, v in enumerate(values)
    ]


@pytest.mark.unit
class TestChangePointAnalyticsDataSource:
    """ChangePointAnalytics must read from Dhara, never from _metrics_cache."""

    async def test_changepoint_queries_dhara_not_metrics_cache(self) -> None:
        """analyze_changepoints calls dhara.query_time_series_async, not _metrics_cache."""
        records = _make_records([88.0] * 10)
        dhara = _make_dhara(records)

        analytics = ChangePointAnalytics(dhara=dhara)
        # Calling analyze_changepoints must trigger a dhara query
        with patch("pytrendy.detect_trends") as mock_detect:
            mock_result = MagicMock()
            mock_result.segments = [
                {
                    "direction": "flat",
                    "start": records[0]["ts"],
                    "end": records[-1]["ts"],
                    "days": 10,
                    "total_change": 0.0,
                    "change_rank": 1,
                    "trend_class": "flat",
                }
            ]
            mock_detect.return_value = mock_result

            await analytics.analyze_changepoints(
                metric_name="fix-failures",
                entity_id="fingerprint-abc",
            )

        dhara.query_time_series_async.assert_called_once()

    async def test_changepoint_has_no_metrics_cache_dependency(self) -> None:
        """ChangePointAnalytics does not inherit from TimeSeriesAnalytics; no _metrics_cache."""
        records = _make_records([70.0] * 6)
        dhara = _make_dhara(records)
        analytics = ChangePointAnalytics(dhara=dhara)
        assert not hasattr(analytics, "_metrics_cache")


@pytest.mark.unit
class TestChangePointAnalyticsMinimumThreshold:
    """analyze_changepoints returns None before calling pytrendy when data < 5 points."""

    async def test_changepoint_returns_none_below_minimum_data_threshold(self) -> None:
        """Fewer than 5 Dhara records → None (pytrendy never called)."""
        records = _make_records([80.0, 75.0, 70.0, 65.0])  # 4 points
        dhara = _make_dhara(records)
        analytics = ChangePointAnalytics(dhara=dhara)

        with patch("pytrendy.detect_trends") as mock_detect:
            result = await analytics.analyze_changepoints(
                metric_name="fix-failures",
                entity_id="fp-xyz",
            )

        assert result is None
        mock_detect.assert_not_called()

    async def test_changepoint_proceeds_with_exactly_5_points(self) -> None:
        """Exactly 5 points satisfies the minimum threshold."""
        records = _make_records([80.0, 75.0, 70.0, 65.0, 60.0])
        dhara = _make_dhara(records)
        analytics = ChangePointAnalytics(dhara=dhara)

        with patch("pytrendy.detect_trends") as mock_detect:
            mock_result = MagicMock()
            mock_result.segments = [
                {
                    "direction": "down",
                    "start": records[0]["ts"],
                    "end": records[-1]["ts"],
                    "days": 5,
                    "total_change": -20.0,
                    "change_rank": 1,
                }
            ]
            mock_detect.return_value = mock_result

            result = await analytics.analyze_changepoints(
                metric_name="fix-failures",
                entity_id="fp-xyz",
            )

        mock_detect.assert_called_once()
        assert result is not None


@pytest.mark.unit
class TestChangePointAnalyticsSegmentAccess:
    """analyze_changepoints must access results.segments, not iterate the result object."""

    async def test_changepoint_access_results_segments_not_dataframe_rows(self) -> None:
        """Implementation must read result.segments list, not iterate PyTrendyResults directly."""
        records = _make_records([88.0] * 5 + [40.0] * 5)
        dhara = _make_dhara(records)
        analytics = ChangePointAnalytics(dhara=dhara)

        with patch("pytrendy.detect_trends") as mock_detect:
            mock_result = MagicMock()
            # .segments is a list of dicts — this is the correct API surface
            mock_result.segments = [
                {
                    "direction": "flat",
                    "start": records[0]["ts"],
                    "end": records[4]["ts"],
                    "days": 5,
                    "total_change": 0.0,
                    "change_rank": 2,
                    "trend_class": "flat",
                },
                {
                    "direction": "down",
                    "start": records[5]["ts"],
                    "end": records[-1]["ts"],
                    "days": 5,
                    "total_change": -48.0,
                    "change_rank": 1,
                    "trend_class": "abrupt",
                },
            ]
            mock_detect.return_value = mock_result

            result = await analytics.analyze_changepoints(
                metric_name="fix-failures",
                entity_id="fp-abc",
            )

        assert result is not None
        assert len(result.segments) == 2
        assert result.segments[1].direction == "down"
        assert result.segments[1].trend_class == "abrupt"

    async def test_trend_class_defaults_to_flat_when_absent(self) -> None:
        """Segments without trend_class key get default 'flat' (flat/noise segments omit it)."""
        records = _make_records([70.0] * 6)
        dhara = _make_dhara(records)
        analytics = ChangePointAnalytics(dhara=dhara)

        with patch("pytrendy.detect_trends") as mock_detect:
            mock_result = MagicMock()
            # No trend_class key — should default to "flat"
            mock_result.segments = [
                {
                    "direction": "flat",
                    "start": records[0]["ts"],
                    "end": records[-1]["ts"],
                    "days": 6,
                    "total_change": 0.0,
                    "change_rank": 1,
                    # trend_class intentionally absent
                }
            ]
            mock_detect.return_value = mock_result

            result = await analytics.analyze_changepoints(
                metric_name="fix-failures",
                entity_id="fp-abc",
            )

        assert result is not None
        assert result.segments[0].trend_class == "flat"


@pytest.mark.unit
class TestChangePointAnalyticsDetection:
    """Core changepoint detection behavior."""

    async def test_changepoint_detects_stable_then_abrupt_drop(self) -> None:
        """21 stable points then 5-day cliff → has_abrupt_trend=True, last segment abrupt."""
        stable = [88.0] * 21
        drop = [40.0, 38.0, 35.0, 37.0, 36.0]
        records = _make_records(stable + drop)
        dhara = _make_dhara(records)
        analytics = ChangePointAnalytics(dhara=dhara)

        with patch("pytrendy.detect_trends") as mock_detect:
            mock_result = MagicMock()
            mock_result.segments = [
                {
                    "direction": "flat",
                    "start": records[0]["ts"],
                    "end": records[20]["ts"],
                    "days": 21,
                    "total_change": 0.0,
                    "change_rank": 2,
                    "trend_class": "flat",
                },
                {
                    "direction": "down",
                    "start": records[21]["ts"],
                    "end": records[-1]["ts"],
                    "days": 5,
                    "total_change": -52.0,
                    "change_rank": 1,
                    "trend_class": "abrupt",
                },
            ]
            mock_detect.return_value = mock_result

            result = await analytics.analyze_changepoints(
                metric_name="fix-failures",
                entity_id="fp-abc",
            )

        assert result is not None
        assert result.has_abrupt_trend is True
        assert result.segments[-1].trend_class == "abrupt"
        assert result.abrupt_segment_count == 1

    async def test_changepoint_classifies_gradual_decline_not_abrupt(self) -> None:
        """Slow linear decrease over 30 days → has_abrupt_trend=False."""
        values = [90.0 - i * 1.5 for i in range(30)]
        records = _make_records(values)
        dhara = _make_dhara(records)
        analytics = ChangePointAnalytics(dhara=dhara)

        with patch("pytrendy.detect_trends") as mock_detect:
            mock_result = MagicMock()
            mock_result.segments = [
                {
                    "direction": "down",
                    "start": records[0]["ts"],
                    "end": records[-1]["ts"],
                    "days": 30,
                    "total_change": -43.5,
                    "change_rank": 1,
                    "trend_class": "gradual",
                },
            ]
            mock_detect.return_value = mock_result

            result = await analytics.analyze_changepoints(
                metric_name="fix-failures",
                entity_id="fp-abc",
            )

        assert result is not None
        assert result.has_abrupt_trend is False

    async def test_changepoint_returns_single_flat_for_stable_metric(self) -> None:
        """Stable metric → one segment, direction='flat', has_abrupt_trend=False."""
        records = _make_records([88.0] * 14)
        dhara = _make_dhara(records)
        analytics = ChangePointAnalytics(dhara=dhara)

        with patch("pytrendy.detect_trends") as mock_detect:
            mock_result = MagicMock()
            mock_result.segments = [
                {
                    "direction": "flat",
                    "start": records[0]["ts"],
                    "end": records[-1]["ts"],
                    "days": 14,
                    "total_change": 0.0,
                    "change_rank": 1,
                    "trend_class": "flat",
                },
            ]
            mock_detect.return_value = mock_result

            result = await analytics.analyze_changepoints(
                metric_name="fix-failures",
                entity_id="fp-abc",
            )

        assert result is not None
        assert len(result.segments) == 1
        assert result.segments[0].direction == "flat"
        assert result.has_abrupt_trend is False

    async def test_changepoint_change_rank_1_is_largest_shift(self) -> None:
        """latest_segment from segment list, and rank=1 segment has biggest change."""
        records = _make_records([50.0] * 10 + [20.0] * 5)
        dhara = _make_dhara(records)
        analytics = ChangePointAnalytics(dhara=dhara)

        with patch("pytrendy.detect_trends") as mock_detect:
            mock_result = MagicMock()
            mock_result.segments = [
                {
                    "direction": "flat",
                    "start": records[0]["ts"],
                    "end": records[9]["ts"],
                    "days": 10,
                    "total_change": 0.0,
                    "change_rank": 2,
                    "trend_class": "flat",
                },
                {
                    "direction": "down",
                    "start": records[10]["ts"],
                    "end": records[-1]["ts"],
                    "days": 5,
                    "total_change": -30.0,
                    "change_rank": 1,
                    "trend_class": "abrupt",
                },
            ]
            mock_detect.return_value = mock_result

            result = await analytics.analyze_changepoints(
                metric_name="fix-failures",
                entity_id="fp-abc",
            )

        assert result is not None
        rank1_segments = [s for s in result.segments if s.change_rank == 1]
        assert len(rank1_segments) == 1
        assert rank1_segments[0].total_change == -30.0
        # latest_segment is the last in the ordered list
        assert result.latest_segment.change_rank == 1


@pytest.mark.unit
class TestChangePointAnalyticsResult:
    """ChangePointResult and TrendSegment dataclass correctness."""

    async def test_result_contains_metric_name_and_time_range(self) -> None:
        """ChangePointResult carries metric_name and time_range."""
        records = _make_records([80.0] * 6)
        dhara = _make_dhara(records)
        analytics = ChangePointAnalytics(dhara=dhara)

        with patch("pytrendy.detect_trends") as mock_detect:
            mock_result = MagicMock()
            mock_result.segments = [
                {
                    "direction": "flat",
                    "start": records[0]["ts"],
                    "end": records[-1]["ts"],
                    "days": 6,
                    "total_change": 0.0,
                    "change_rank": 1,
                    "trend_class": "flat",
                },
            ]
            mock_detect.return_value = mock_result

            result = await analytics.analyze_changepoints(
                metric_name="my-metric",
                entity_id="fp-abc",
            )

        assert result is not None
        assert result.metric_name == "my-metric"
        assert result.time_range is not None
        assert len(result.time_range) == 2


@pytest.mark.unit
class TestAnalyzeChangepointsMCPAuth:
    """analyze_changepoints MCP tool must be protected by @require_auth."""

    async def test_analyze_changepoints_requires_auth(self, monkeypatch) -> None:
        """When auth is enabled, analyze_changepoints raises an error without a token."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from akosha.mcp.tools.akosha_tools import register_analytics_tools
        from akosha.mcp.tools.tool_registry import FastMCPToolRegistry
        from akosha.processing.analytics import ChangePointAnalytics

        # Enable auth via env so @require_auth becomes active
        monkeypatch.setenv("AKOSHA_API_TOKEN", "test-secret-token")

        # Build a real registry against a minimal mock FastMCP so decorator chain executes
        mock_app = MagicMock()
        mock_app.tool.return_value = lambda fn: fn  # identity — preserve the function
        registry = FastMCPToolRegistry(mock_app)

        analytics_service = MagicMock()
        dhara = AsyncMock()
        dhara.query_time_series_async = AsyncMock(return_value=[])
        changepoint_analytics = ChangePointAnalytics(dhara=dhara)

        with patch("pytrendy.detect_trends"):
            register_analytics_tools(registry, analytics_service, changepoint_analytics)

        # The registered coroutine is the require_auth-wrapped function
        assert "analyze_changepoints" in registry.tools, (
            "analyze_changepoints tool was not registered — check changepoint_analytics wiring"
        )

        fn = registry.tools["analyze_changepoints"].coroutine

        # Calling without an auth token must raise — proves @require_auth is applied
        with pytest.raises(Exception, match=r"(?i)(token|auth|permission|unauthorized)"):
            await fn(
                metric_name="fix-failures",
                entity_id="fp-abc",
                time_window_days=30,
            )
