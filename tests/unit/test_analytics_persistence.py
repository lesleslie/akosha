"""Tests for TimeSeriesAnalytics SQLite persistence (Phase 2 REQ-MS-002/004/005).

The in-memory ``_metrics_cache`` stays as an L1 cache; SQLite is the durable
backing. Write-through on ``add_metric``, populate cache from SQLite on
``initialize()``, close connection on ``aclose()``. SQLite write failures
are soft-logged + recorded as a counter but MUST NOT raise — the in-memory
write already accepted the point.
"""

from __future__ import annotations

import sqlite3
import unittest.mock as mock
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from akosha.processing.analytics import TimeSeriesAnalytics

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Yield a unique DB path; ``tmp_path`` auto-cleans."""
    return tmp_path / "metrics.db"


@pytest.mark.asyncio
async def test_metrics_survive_restart(db_path: Path) -> None:
    """Restart-survival: write through analytics A, aclose, then a fresh
    analytics B with the same db_path must observe the metric after
    ``initialize()``.
    """
    analytics_a = TimeSeriesAnalytics(db_path=str(db_path))
    await analytics_a.initialize()
    point_time = datetime.now(UTC)
    # Two points so the trend analysis has enough data (analyze_trend
    # returns None when len(filtered) < 2).
    await analytics_a.add_metric(
        metric_name="eval_pass_rate:prefect:code_review_py_typo",
        value=0.80,
        system_id="mahavishnu-ci",
        timestamp=point_time,
        metadata={"fixture": "code_review_py_typo"},
    )
    await analytics_a.add_metric(
        metric_name="eval_pass_rate:prefect:code_review_py_typo",
        value=0.85,
        system_id="mahavishnu-ci",
        timestamp=point_time + timedelta(hours=1),
        metadata={"fixture": "code_review_py_typo"},
    )

    # Sanity: the in-memory cache accepted the points before aclose.
    assert "eval_pass_rate:prefect:code_review_py_typo" in analytics_a.get_metric_names()
    assert analytics_a.get_system_count(
        "eval_pass_rate:prefect:code_review_py_typo"
    ) == 1

    await analytics_a.aclose()

    # Fresh process: same db_path, cold cache.
    analytics_b = TimeSeriesAnalytics(db_path=str(db_path))
    assert analytics_b.get_metric_names() == []
    await analytics_b.initialize()

    names = analytics_b.get_metric_names()
    assert "eval_pass_rate:prefect:code_review_py_typo" in names

    # And the trend analysis can read it.
    trend = await analytics_b.analyze_trend(
        "eval_pass_rate:prefect:code_review_py_typo",
        system_id="mahavishnu-ci",
    )
    assert trend is not None
    assert trend.metric_name == "eval_pass_rate:prefect:code_review_py_typo"
    assert trend.percent_change > 0  # 0.80 -> 0.85 = +6.25%

    await analytics_b.aclose()


@pytest.mark.asyncio
async def test_metrics_db_path_isolated(tmp_path: Path) -> None:
    """Two analytics instances with different db_paths must not see each
    other's data.
    """
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"

    analytics_a = TimeSeriesAnalytics(db_path=str(db_a))
    analytics_b = TimeSeriesAnalytics(db_path=str(db_b))
    await analytics_a.initialize()
    await analytics_b.initialize()

    await analytics_a.add_metric(
        metric_name="cpu_usage",
        value=42.0,
        system_id="system-a",
    )
    await analytics_b.add_metric(
        metric_name="memory_usage",
        value=99.0,
        system_id="system-b",
    )

    # Each instance only sees its own metric in-memory.
    assert "cpu_usage" in analytics_a.get_metric_names()
    assert "memory_usage" not in analytics_a.get_metric_names()
    assert "memory_usage" in analytics_b.get_metric_names()
    assert "cpu_usage" not in analytics_b.get_metric_names()

    await analytics_a.aclose()
    await analytics_b.aclose()

    # And after restart, the isolation holds across the SQLite backing.
    fresh_a = TimeSeriesAnalytics(db_path=str(db_a))
    fresh_b = TimeSeriesAnalytics(db_path=str(db_b))
    await fresh_a.initialize()
    await fresh_b.initialize()
    assert "cpu_usage" in fresh_a.get_metric_names()
    assert "memory_usage" not in fresh_a.get_metric_names()
    assert "memory_usage" in fresh_b.get_metric_names()
    assert "cpu_usage" not in fresh_b.get_metric_names()
    await fresh_a.aclose()
    await fresh_b.aclose()


@pytest.mark.asyncio
async def test_sqlite_write_failure_does_not_raise(tmp_path: Path) -> None:
    """If the SQLite connection itself fails to open (disk full, readonly
    fs, permissions), ``add_metric`` must still record to the in-memory
    cache and MUST NOT raise.
    """
    db_path = tmp_path / "metrics.db"
    boom = sqlite3.OperationalError("disk I/O error")
    with mock.patch(
        "akosha.processing.analytics.sqlite3.connect",
        side_effect=boom,
    ):
        # Connection fails to open inside __init__.
        analytics = TimeSeriesAnalytics(db_path=str(db_path))
        # Should not raise despite the broken sqlite path.
        await analytics.add_metric(
            metric_name="eval_pass_rate:prefect:code_review_py_typo",
            value=0.5,
            system_id="mahavishnu-ci",
        )

    # The in-memory cache still accepted the point.
    assert "eval_pass_rate:prefect:code_review_py_typo" in analytics.get_metric_names()
    assert analytics.get_system_count(
        "eval_pass_rate:prefect:code_review_py_typo"
    ) == 1

    # initialize() and aclose() are also no-ops when SQLite is unavailable.
    await analytics.initialize()
    await analytics.aclose()


@pytest.mark.asyncio
async def test_db_path_env_var_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When no ``db_path`` is passed, the path comes from ``AKOSHA_METRICS_DB_PATH``
    or the default ``~/.akosha/state/metrics.db`` (parent dir is created).
    """
    monkeypatch.setenv("AKOSHA_METRICS_DB_PATH", str(tmp_path / "envvar.db"))
    analytics = TimeSeriesAnalytics()
    await analytics.initialize()
    await analytics.add_metric(
        metric_name="cpu_usage",
        value=12.5,
        system_id="system-env",
    )
    # The file should exist after the first write-through.
    assert (tmp_path / "envvar.db").exists()
    await analytics.aclose()


@pytest.mark.asyncio
async def test_initialize_with_existing_data_loads_cache(db_path: Path) -> None:
    """After a write-through + aclose, a new analytics with the same
    db_path has an empty cache until ``initialize()`` runs.
    """
    analytics_a = TimeSeriesAnalytics(db_path=str(db_path))
    await analytics_a.initialize()
    for i in range(5):
        await analytics_a.add_metric(
            metric_name="requests_per_second",
            value=10.0 + i,
            system_id="system-1",
        )
    await analytics_a.aclose()

    analytics_b = TimeSeriesAnalytics(db_path=str(db_path))
    # Cache is cold.
    assert analytics_b.get_metric_names() == []
    await analytics_b.initialize()
    assert "requests_per_second" in analytics_b.get_metric_names()
    assert analytics_b.get_system_count("requests_per_second") == 1
    await analytics_b.aclose()


@pytest.mark.asyncio
async def test_initialize_idempotent(db_path: Path) -> None:
    """Calling ``initialize()`` twice does not duplicate rows."""
    analytics = TimeSeriesAnalytics(db_path=str(db_path))
    await analytics.initialize()
    await analytics.add_metric("dup_metric", 1.0, "system-1")
    await analytics.add_metric("dup_metric", 2.0, "system-1")
    await analytics.aclose()

    analytics2 = TimeSeriesAnalytics(db_path=str(db_path))
    await analytics2.initialize()
    await analytics2.initialize()  # second call must not duplicate rows
    count = analytics2.get_system_count("dup_metric")
    assert count == 1
    trend = await analytics2.analyze_trend("dup_metric", system_id="system-1")
    assert trend is not None
    await analytics2.aclose()


@pytest.mark.asyncio
async def test_aclose_safe_without_initialize(db_path: Path) -> None:
    """Calling ``aclose()`` before ``initialize()`` is a no-op (no connection yet)."""
    analytics = TimeSeriesAnalytics(db_path=str(db_path))
    await analytics.aclose()  # should not raise
