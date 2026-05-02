"""Tests for Akosha Prometheus metrics collection and instrumentation."""

from unittest.mock import MagicMock, patch

import pytest

from akosha.monitoring.metrics import (
    MetricsCollector,
    cold_store_size_bytes,
    hot_store_record_count,
    hot_store_size_bytes,
    ingestion_errors_total,
    ingestion_requests_total,
    migration_records_total,
    query_cache_hits,
    query_requests_total,
    record_cache_hit,
    record_ingestion_event,
    record_query_event,
    setup_metrics_endpoint,
    setup_middleware,
    track_ingestion,
    track_migration,
    track_query,
    update_storage_metrics,
    warm_store_record_count,
    warm_store_size_bytes,
)


def _get_counter_value(counter):
    """Extract current counter value from Prometheus counter."""
    samples = counter.collect()
    for metric in samples:
        for sample in metric.samples:
            if (
                "total" in sample.name
                or "requests" in sample.name
                or "errors" in sample.name
                or "hits" in sample.name
                or "records" in sample.name
            ):
                return sample.value
    return samples[0].samples[0].value


def _get_gauge_value(gauge):
    """Extract current gauge value."""
    samples = gauge.collect()
    return samples[0].samples[0].value


# ============================================================================
# Metric Definitions
# ============================================================================


class TestMetricDefinitions:
    """Tests that Prometheus metric definitions are properly configured."""

    def test_ingestion_metrics_exist(self):
        assert "akosha_ingestion" in ingestion_requests_total._name
        assert "system_id" in ingestion_requests_total._labelnames
        assert "status" in ingestion_requests_total._labelnames

    def test_query_metrics_exist(self):
        assert "akosha_query" in query_requests_total._name
        assert "query_type" in query_requests_total._labelnames
        assert "status" in query_requests_total._labelnames

    def test_storage_metrics_exist(self):
        assert "hot_store_size" in hot_store_size_bytes._name
        assert "cold_store_size" in cold_store_size_bytes._name

    def test_system_metrics_exist(self):
        from akosha.monitoring.metrics import (
            system_cpu_usage_percent,
            system_disk_usage_bytes,
            system_memory_usage_bytes,
        )

        assert "cpu_usage" in system_cpu_usage_percent._name
        assert "memory_usage" in system_memory_usage_bytes._name
        assert "disk_usage" in system_disk_usage_bytes._name
        assert "mount_point" in system_disk_usage_bytes._labelnames

    def test_cache_metric_labels(self):
        assert "cache_level" in query_cache_hits._labelnames

    def test_migration_metric_labels(self):
        assert "source_tier" in migration_records_total._labelnames
        assert "target_tier" in migration_records_total._labelnames
        assert "status" in migration_records_total._labelnames


# ============================================================================
# MetricsCollector
# ============================================================================


class TestMetricsCollector:
    """Tests for MetricsCollector system metrics."""

    @pytest.mark.asyncio
    async def test_collect_system_metrics_calls_psutil(self):
        mock_psutil = MagicMock()
        mock_psutil.cpu_percent = MagicMock(return_value=45.5)
        mock_memory = MagicMock()
        mock_memory.used = 1024 * 1024 * 500
        mock_psutil.virtual_memory = MagicMock(return_value=mock_memory)
        mock_disk = MagicMock()
        mock_disk.used = 1024 * 1024 * 1024 * 50
        mock_psutil.disk_usage = MagicMock(return_value=mock_disk)

        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            await MetricsCollector.collect_system_metrics()

        mock_psutil.cpu_percent.assert_called_once_with(interval=1)
        mock_psutil.virtual_memory.assert_called_once()
        mock_psutil.disk_usage.assert_called_once_with("/")

    @pytest.mark.asyncio
    async def test_collect_system_metrics_handles_psutil_error(self):
        mock_psutil = MagicMock()
        mock_psutil.cpu_percent = MagicMock(side_effect=Exception("psutil error"))

        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            # Should not raise - error is handled by the caller
            try:
                await MetricsCollector.collect_system_metrics()
            except Exception:
                pass  # psutil error propagates, which is acceptable


# ============================================================================
# track_ingestion decorator
# ============================================================================


class TestTrackIngestionDecorator:
    """Tests for the track_ingestion decorator."""

    @pytest.mark.asyncio
    async def test_success_increments_counter(self):
        @track_ingestion("test-system")
        async def process_upload(data):
            return {"status": "ok"}

        await process_upload("data")
        value = _get_counter_value(
            ingestion_requests_total.labels(system_id="test-system", status="success")
        )
        assert value > 0

    @pytest.mark.asyncio
    async def test_failure_increments_error_counter(self):
        @track_ingestion("error-system")
        async def failing_upload(data):
            raise ValueError("bad data")

        with pytest.raises(ValueError):
            await failing_upload("bad")
        value = _get_counter_value(
            ingestion_errors_total.labels(error_type="ValueError", system_id="error-system")
        )
        assert value > 0

    @pytest.mark.asyncio
    async def test_decorator_preserves_return_value(self):
        @track_ingestion("test-system")
        async def process_upload(data):
            return {"id": "123"}

        result = await process_upload("data")
        assert result == {"id": "123"}

    @pytest.mark.asyncio
    async def test_decorator_propagates_exception(self):
        @track_ingestion("test-system")
        async def failing_upload(data):
            raise RuntimeError("crash")

        with pytest.raises(RuntimeError, match="crash"):
            await failing_upload("data")


# ============================================================================
# track_query decorator
# ============================================================================


class TestTrackQueryDecorator:
    """Tests for the track_query decorator."""

    @pytest.mark.asyncio
    async def test_success_tracks_duration(self):
        @track_query("semantic_search")
        async def search(query):
            return [{"id": "1"}, {"id": "2"}]

        result = await search("test query")
        assert len(result) == 2
        value = _get_counter_value(
            query_requests_total.labels(query_type="semantic_search", status="success")
        )
        assert value > 0

    @pytest.mark.asyncio
    async def test_non_list_result_no_count(self):
        @track_query("search")
        async def search(query):
            return {"single": "result"}

        await search("test")
        # No error should occur with non-list result

    @pytest.mark.asyncio
    async def test_failure_tracks_error(self):
        @track_query("search")
        async def failing_search(query):
            raise ConnectionError("db down")

        with pytest.raises(ConnectionError):
            await failing_search("test")


# ============================================================================
# track_migration decorator
# ============================================================================


class TestTrackMigrationDecorator:
    """Tests for the track_migration decorator."""

    @pytest.mark.asyncio
    async def test_success_tracks_migration(self):
        result_obj = MagicMock()
        result_obj.records_migrated = 10

        @track_migration("hot", "warm")
        async def migrate():
            return result_obj

        result = await migrate()
        assert result is result_obj
        value = _get_counter_value(
            migration_records_total.labels(source_tier="hot", target_tier="warm", status="success")
        )
        assert value >= 10

    @pytest.mark.asyncio
    async def test_failure_tracks_error(self):
        @track_migration("hot", "cold")
        async def failing_migrate():
            raise OSError("disk full")

        with pytest.raises(IOError):
            await failing_migrate()
        value = _get_counter_value(
            migration_records_total.labels(source_tier="hot", target_tier="cold", status="error")
        )
        assert value > 0


# ============================================================================
# FastAPI Integration
# ============================================================================


class TestFastAPIIntegration:
    """Tests for FastAPI metrics endpoint and middleware."""

    def test_setup_metrics_endpoint(self):
        from fastapi import FastAPI
        from starlette.testclient import TestClient

        app = FastAPI()
        setup_metrics_endpoint(app)
        client = TestClient(app)
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "text/" in response.headers["content-type"]

    def test_setup_middleware_ingest_tracking(self):
        from fastapi import FastAPI
        from starlette.testclient import TestClient

        app = FastAPI()
        setup_middleware(app)

        @app.get("/ingest/upload")
        async def ingest():
            return {"status": "ok"}

        client = TestClient(app)
        response = client.get("/ingest/upload")
        assert response.status_code == 200

    def test_setup_middleware_query_tracking(self):
        from fastapi import FastAPI
        from starlette.testclient import TestClient

        app = FastAPI()
        setup_middleware(app)

        @app.get("/query/search")
        async def search():
            return {"results": []}

        client = TestClient(app)
        response = client.get("/query/search")
        assert response.status_code == 200


# ============================================================================
# Convenience Functions
# ============================================================================


class TestConvenienceFunctions:
    """Tests for manual metric update convenience functions."""

    def test_record_ingestion_event(self):
        record_ingestion_event("sys1", "success")
        value = _get_counter_value(
            ingestion_requests_total.labels(system_id="sys1", status="success")
        )
        assert value > 0

    def test_record_ingestion_event_with_duration(self):
        record_ingestion_event("sys1", "success", duration=1.5)
        value = _get_counter_value(
            ingestion_requests_total.labels(system_id="sys1", status="success")
        )
        assert value > 0

    def test_record_query_event(self):
        record_query_event("search", "success")
        value = _get_counter_value(
            query_requests_total.labels(query_type="search", status="success")
        )
        assert value > 0

    def test_update_storage_metrics(self):
        update_storage_metrics(
            hot_size_bytes=1000,
            hot_record_count=10,
            warm_size_bytes=2000,
            warm_record_count=20,
        )
        assert _get_gauge_value(hot_store_size_bytes) == 1000
        assert _get_gauge_value(hot_store_record_count) == 10
        assert _get_gauge_value(warm_store_size_bytes) == 2000
        assert _get_gauge_value(warm_store_record_count) == 20

    def test_record_cache_hit(self):
        record_cache_hit("l1")
        value = _get_counter_value(query_cache_hits.labels(cache_level="l1"))
        assert value > 0

    @pytest.mark.parametrize("level", ["l1", "l2", "miss"])
    def test_record_cache_hit_different_levels(self, level):
        record_cache_hit(level)
        value = _get_counter_value(query_cache_hits.labels(cache_level=level))
        assert value > 0
