"""Integration tests for the Akosha eval metric sink (Phase 2).

REQ-MS-004: round trip across Akosha restarts — write a metric in one
subprocess, restart, read it back in a fresh subprocess pointed at the
same SQLite DB.

REQ-MS-005: backwards compatibility — every existing TimeSeriesAnalytics
test must continue to pass after the persistence backing lands.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from akosha.processing.analytics import TimeSeriesAnalytics


@pytest.mark.asyncio
async def test_round_trip_across_subprocess_bounce(tmp_path: Path) -> None:
    """REQ-MS-004: post a metric in a child subprocess, bounce the
    process, restart, and assert the metric is still queryable.
    """
    db_path = tmp_path / "eval_sink.db"
    db_path_str = str(db_path)
    # ``AKOSHA_METRICS_DB_PATH`` is read by ``TimeSeriesAnalytics.__init__``
    # when ``db_path`` is not provided. We pass it via env so each child
    # process picks up the same path without touching the parent shell.
    env = os.environ.copy()
    env["AKOSHA_METRICS_DB_PATH"] = db_path_str

    # Subprocess 1: write a metric, close cleanly.
    write_script = textwrap.dedent(
        """
        import asyncio
        from datetime import UTC, datetime, timedelta
        from akosha.observability.tracing import setup_telemetry
        from akosha.processing.analytics import TimeSeriesAnalytics

        async def main() -> None:
            # Telemetry is normally initialised by the Akosha lifespan;
            # the subprocess has no lifespan, so we initialise it here
            # so record_counter / record_histogram don't raise.
            setup_telemetry(
                service_name="akosha-eval-sink-test",
                enable_console_export=False,
                otlp_endpoint=None,
            )
            analytics = TimeSeriesAnalytics()
            await analytics.initialize()
            now = datetime.now(UTC)
            await analytics.add_metric(
                metric_name="eval_pass_rate:prefect:code_review_py_typo",
                value=0.80,
                system_id="mahavishnu-ci",
                timestamp=now,
                metadata={"fixture": "code_review_py_typo"},
            )
            await analytics.add_metric(
                metric_name="eval_pass_rate:prefect:code_review_py_typo",
                value=0.85,
                system_id="mahavishnu-ci",
                timestamp=now + timedelta(hours=1),
                metadata={"fixture": "code_review_py_typo"},
            )
            await analytics.aclose()
            print("WRITE_OK")

        asyncio.run(main())
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", write_script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"Write subprocess failed: rc={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "WRITE_OK" in result.stdout, f"Unexpected stdout: {result.stdout!r}"
    # Sanity: the DB file is on disk.
    assert db_path.exists(), f"DB file {db_path} not created"

    # Subprocess 2: cold cache, initialize, read back the metric.
    read_script = textwrap.dedent(
        """
        import asyncio
        from akosha.observability.tracing import setup_telemetry
        from akosha.processing.analytics import TimeSeriesAnalytics

        async def main() -> None:
            setup_telemetry(
                service_name="akosha-eval-sink-test",
                enable_console_export=False,
                otlp_endpoint=None,
            )
            analytics = TimeSeriesAnalytics()
            # Cold cache before initialize.
            assert analytics.get_metric_names() == [], (
                f"Expected cold cache, got {analytics.get_metric_names()!r}"
            )
            await analytics.initialize()
            names = analytics.get_metric_names()
            assert "eval_pass_rate:prefect:code_review_py_typo" in names, (
                f"Metric not loaded from SQLite; got {names!r}"
            )
            count = analytics.get_system_count(
                "eval_pass_rate:prefect:code_review_py_typo"
            )
            assert count == 1, f"Expected 1 system, got {count}"
            trend = await analytics.analyze_trend(
                "eval_pass_rate:prefect:code_review_py_typo",
                system_id="mahavishnu-ci",
            )
            assert trend is not None, "Trend returned None after restart"
            assert trend.percent_change > 0, (
                f"Expected positive percent change, got {trend.percent_change}"
            )
            print("READ_OK")
            await analytics.aclose()

        asyncio.run(main())
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", read_script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"Read subprocess failed: rc={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "READ_OK" in result.stdout, (
        f"Metric did not survive subprocess bounce: {result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )


@pytest.mark.asyncio
async def test_existing_metrics_unaffected() -> None:
    """REQ-MS-005: every existing TimeSeriesAnalytics unit test must
    continue to pass with the persistence backing in place. This is the
    smoke test pin — if a refactor regresses backwards compat, this
    suite flips red.

    We shell out to a fresh pytest invocation rather than reusing the
    parent runner. In-process ``pytest.main()`` from inside an async
    test conflicts with the parent event loop (``Runner.run() cannot be
    called from a running event loop``); subprocess is also the
    cleaner isolation boundary.
    """
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(repo_root / "tests" / "unit" / "test_analytics.py"),
            str(repo_root / "tests" / "unit" / "test_aging.py"),
            "-q",
            "--no-header",
            "--no-cov",
            "-p",
            "no:cacheprovider",
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"Existing TimeSeriesAnalytics tests regressed "
        f"(pytest exit={result.returncode}).\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


@pytest.mark.asyncio
async def test_lifespan_analytics_initialize_then_aclose(tmp_path: Path) -> None:
    """Phase 2 wiring smoke test: the lifespan at
    ``akosha/mcp/server.py`` constructs ``TimeSeriesAnalytics()`` then
    immediately calls ``initialize()`` and ``aclose()`` on shutdown.
    This test pins the same shape: construct, init, add, aclose, reopen,
    init, read.
    """
    db_path = tmp_path / "lifespan_smoke.db"

    # First "lifespan": write through, aclose.
    a = TimeSeriesAnalytics(db_path=str(db_path))
    await a.initialize()
    await a.add_metric(
        metric_name="eval_pass_rate:llamaindex:code_review_py_typo",
        value=0.92,
        system_id="mahavishnu-ci",
    )
    await a.aclose()

    # Second "lifespan": cold cache, init, read.
    b = TimeSeriesAnalytics(db_path=str(db_path))
    assert b.get_metric_names() == []  # confirm cold
    await b.initialize()
    assert "eval_pass_rate:llamaindex:code_review_py_typo" in b.get_metric_names()
    await b.aclose()
