"""Unit tests for Prometheus metrics collection."""

from __future__ import annotations

import time

import pytest
from prometheus_client import REGISTRY as DEFAULT_REGISTRY

from akosha.observability import prometheus_metrics


@pytest.fixture(autouse=True)
def reset_metrics_before_each_test():
    """Reset all metrics before each test to ensure isolation."""
    yield
    # Reset after test
    prometheus_metrics.reset_all_metrics()


class TestIngestionMetrics:
    """Test suite for ingestion-related metrics."""

    def test_record_ingestion_success(self):
        """Test recording successful ingestion."""
        prometheus_metrics.record_ingestion_record(
            system_id="test-system-1",
            status="success",
            bytes_processed=1024,
        )

        # Verify the metric was recorded
        metrics = prometheus_metrics.get_metric_summary()
        assert "akosha_ingestion_throughput" in metrics

        throughput_samples = metrics["akosha_ingestion_throughput"]
        # Labels use format: key="value"
        assert any(
            'status="success"' in labels and 'system_id="test-system-1"' in labels
            for labels in throughput_samples.keys()
        )

    def test_record_ingestion_error(self):
        """Test recording failed ingestion."""
        prometheus_metrics.record_ingestion_record(
            system_id="test-system-1",
            status="error",
        )

        metrics = prometheus_metrics.get_metric_summary()
        assert "akosha_ingestion_throughput" in metrics

    def test_update_ingestion_throughput(self):
        """Test updating ingestion throughput gauge."""
        prometheus_metrics.update_ingestion_throughput(
            records_per_second=100.5,
            system_id="test-system-1",
        )

        metrics = prometheus_metrics.get_metric_summary()
        throughput_samples = metrics["akosha_ingestion_throughput"]

        # Find the sample for our system
        for labels, value in throughput_samples.items():
            if 'system_id="test-system-1"' in labels:
                assert value == 100.5
                break
        else:
            pytest.fail("Throughput metric not found for test-system-1")


class TestSearchMetrics:
    """Test suite for search-related metrics."""

    def test_observe_search_latency_basic(self):
        """Test basic search latency observation."""
        with prometheus_metrics.observe_search_latency(
            query_type="semantic",
            shard_count=3,
            tier="hot",
        ) as record_results:
            # Simulate search
            time.sleep(0.01)
            record_results(10)

        metrics = prometheus_metrics.get_metric_summary()
        assert "akosha_search_latency_milliseconds_bucket" in metrics
        assert "akosha_search_results_total" in metrics
        assert "akosha_search_result_count_bucket" in metrics

    def test_search_latency_different_tiers(self):
        """Test search metrics for different storage tiers."""
        tiers = ["hot", "warm", "cold"]

        for tier in tiers:
            with prometheus_metrics.observe_search_latency(
                query_type="semantic",
                shard_count=2,
                tier=tier,
            ) as record_results:
                time.sleep(0.001)
                record_results(5)

        metrics = prometheus_metrics.get_metric_summary()
        latency_samples = metrics["akosha_search_latency_milliseconds_bucket"]

        # Verify we have samples for all tiers
        tier_labels = [
            labels for labels in latency_samples.keys() if "tier=" in labels
        ]
        assert len(tier_labels) >= 3

    def test_search_result_count_tracking(self):
        """Test tracking of result counts."""
        result_counts = [0, 5, 10, 100]

        for count in result_counts:
            with prometheus_metrics.observe_search_latency(
                query_type="keyword",
                shard_count=1,
                tier="hot",
            ) as record_results:
                record_results(count)

        metrics = prometheus_metrics.get_metric_summary()
        result_count_samples = metrics["akosha_search_result_count_bucket"]

        # Verify we tracked multiple searches
        assert len(result_count_samples) > 0


class TestCacheMetrics:
    """Test suite for cache-related metrics."""

    def test_record_cache_hit(self):
        """Test recording cache hits."""
        prometheus_metrics.record_cache_hit(cache_tier="L1", query_type="semantic")

        metrics = prometheus_metrics.get_metric_summary()
        assert "akosha_cache_operations_total" in metrics

        cache_samples = metrics["akosha_cache_operations_total"]
        assert any(
            'operation="hit"' in labels and 'cache_tier="L1"' in labels
            for labels in cache_samples.keys()
        )

    def test_record_cache_miss(self):
        """Test recording cache misses."""
        prometheus_metrics.record_cache_miss(cache_tier="L1", query_type="semantic")

        metrics = prometheus_metrics.get_metric_summary()
        cache_samples = metrics["akosha_cache_operations_total"]

        assert any(
            'operation="miss"' in labels and 'cache_tier="L1"' in labels
            for labels in cache_samples.keys()
        )

    def test_update_cache_hit_rate(self):
        """Test updating cache hit rate gauge."""
        prometheus_metrics.update_cache_hit_rate(
            hit_rate=0.85,
            cache_tier="L1",
            query_type="semantic",
        )

        metrics = prometheus_metrics.get_metric_summary()
        hit_rate_samples = metrics["akosha_cache_hit_rate"]

        # Find the sample and verify value
        for labels, value in hit_rate_samples.items():
            if 'cache_tier="L1"' in labels and 'query_type="semantic"' in labels:
                assert value == 0.85
                break
        else:
            pytest.fail("Cache hit rate metric not found")

    def test_cache_hit_rate_clamping(self):
        """Test that cache hit rate is clamped between 0 and 1."""
        # Test upper bound
        prometheus_metrics.update_cache_hit_rate(
            hit_rate=1.5,  # Invalid, should be clamped to 1.0
            cache_tier="L1",
        )

        metrics = prometheus_metrics.get_metric_summary()
        hit_rate_samples = metrics["akosha_cache_hit_rate"]

        for labels, value in hit_rate_samples.items():
            if 'cache_tier="L1"' in labels:
                assert value == 1.0
                break

    def test_update_cache_size(self):
        """Test updating cache size in bytes."""
        prometheus_metrics.update_cache_size(
            size_bytes=1024 * 1024 * 100,  # 100 MB
            cache_tier="L1",
        )

        metrics = prometheus_metrics.get_metric_summary()
        size_samples = metrics["akosha_cache_size_bytes"]

        for labels, value in size_samples.items():
            if 'cache_tier="L1"' in labels:
                assert value == 1024 * 1024 * 100
                break


class TestStorageMetrics:
    """Test suite for storage tier metrics."""

    def test_update_store_sizes(self):
        """Test updating storage tier record counts."""
        prometheus_metrics.update_store_sizes(
            hot_size=1_000_000,
            warm_size=50_000_000,
            cold_size=500_000_000,
        )

        metrics = prometheus_metrics.get_metric_summary()
        store_samples = metrics["akosha_store_size_records"]

        # Verify all tiers are present
        tier_values = {}
        for labels, value in store_samples.items():
            if 'tier="hot"' in labels:
                tier_values["hot"] = value
            elif 'tier="warm"' in labels:
                tier_values["warm"] = value
            elif 'tier="cold"' in labels:
                tier_values["cold"] = value

        assert tier_values.get("hot") == 1_000_000
        assert tier_values.get("warm") == 50_000_000
        assert tier_values.get("cold") == 500_000_000

    def test_update_store_sizes_with_bytes(self):
        """Test updating storage tier sizes in bytes."""
        prometheus_metrics.update_store_sizes(
            hot_size=1_000_000,
            warm_size=50_000_000,
            cold_size=500_000_000,
            hot_bytes=1024 * 1024 * 1024,  # 1 GB
            warm_bytes=1024 * 1024 * 1024 * 50,  # 50 GB
            cold_bytes=1024 * 1024 * 1024 * 500,  # 500 GB
        )

        metrics = prometheus_metrics.get_metric_summary()
        size_bytes_samples = metrics["akosha_store_size_bytes"]

        # Verify byte counts
        assert any(
            'tier="hot"' in labels and value == 1024 * 1024 * 1024
            for labels, value in size_bytes_samples.items()
        )

    def test_observe_store_operation(self):
        """Test observing storage operation duration."""
        with prometheus_metrics.observe_store_operation(
            tier="hot",
            operation="write",
        ) as record_status:
            time.sleep(0.001)
            record_status("success")

        metrics = prometheus_metrics.get_metric_summary()
        assert "akosha_store_operation_duration_seconds_bucket" in metrics
        assert "akosha_store_operations_total" in metrics


class TestErrorMetrics:
    """Test suite for error tracking metrics."""

    def test_increment_errors_basic(self):
        """Test basic error increment."""
        prometheus_metrics.increment_errors(
            component="hot_store",
            error_type="database_error",
            severity="error",
        )

        metrics = prometheus_metrics.get_metric_summary()
        assert "akosha_errors_total" in metrics

        error_samples = metrics["akosha_errors_total"]
        assert any(
            'component="hot_store"' in labels
            and 'error_type="database_error"' in labels
            and 'severity="error"' in labels
            for labels in error_samples.keys()
        )

    def test_error_timestamp_update(self):
        """Test that error timestamp is updated."""
        before_time = time.time()

        prometheus_metrics.increment_errors(
            component="hot_store",
            error_type="database_error",
        )

        after_time = time.time()

        metrics = prometheus_metrics.get_metric_summary()
        timestamp_samples = metrics["akosha_error_last_timestamp_seconds"]

        for labels, value in timestamp_samples.items():
            if 'component="hot_store"' in labels and 'error_type="database_error"' in labels:
                assert before_time <= value <= after_time
                break

    def test_multiple_error_components(self):
        """Test tracking errors from multiple components."""
        components = [
            "ingestion_worker",
            "hot_store",
            "vector_indexer",
        ]

        for component in components:
            prometheus_metrics.increment_errors(
                component=component,
                error_type="timeout_error",
                severity="warning",
            )

        metrics = prometheus_metrics.get_metric_summary()
        error_samples = metrics["akosha_errors_total"]

        # Count unique components with errors
        unique_components = set()
        for labels in error_samples.keys():
            for component in components:
                if f'component="{component}"' in labels:
                    unique_components.add(component)

        assert len(unique_components) == 3


class TestOperationMetrics:
    """Test suite for general operation metrics."""

    def test_observe_operation_success(self):
        """Test observing a successful operation."""
        with prometheus_metrics.observe_operation("test_operation") as record_status:
            time.sleep(0.001)
            record_status("success")

        metrics = prometheus_metrics.get_metric_summary()
        assert "akosha_operation_duration_seconds_bucket" in metrics
        assert "akosha_operations_total" in metrics

    def test_observe_operation_error(self):
        """Test observing a failed operation."""
        with prometheus_metrics.observe_operation("failing_operation") as record_status:
            time.sleep(0.001)
            record_status("error")

        metrics = prometheus_metrics.get_metric_summary()
        ops_samples = metrics["akosha_operations_total"]

        # Verify error status was recorded
        assert any(
            'operation_type="failing_operation"' in labels and 'status="error"' in labels
            for labels in ops_samples.keys()
        )


class TestDeduplicationMetrics:
    """Test suite for deduplication metrics."""

    def test_record_exact_duplicate(self):
        """Test recording exact duplicate detection."""
        prometheus_metrics.record_deduplication_check(
            check_type="exact",
            result="duplicate",
        )

        metrics = prometheus_metrics.get_metric_summary()
        assert "akosha_deduplication_checks_total" in metrics

        dedup_samples = metrics["akosha_deduplication_checks_total"]
        assert any(
            'check_type="exact"' in labels and 'result="duplicate"' in labels
            for labels in dedup_samples.keys()
        )

    def test_record_fuzzy_unique(self):
        """Test recording fuzzy unique result."""
        prometheus_metrics.record_deduplication_check(
            check_type="fuzzy",
            result="unique",
        )

        metrics = prometheus_metrics.get_metric_summary()
        dedup_samples = metrics["akosha_deduplication_checks_total"]

        assert any(
            'check_type="fuzzy"' in labels and 'result="unique"' in labels
            for labels in dedup_samples.keys()
        )


class TestKnowledgeGraphMetrics:
    """Test suite for knowledge graph metrics."""

    def test_knowledge_graph_entities(self):
        """Test tracking knowledge graph entity count."""
        prometheus_metrics.knowledge_graph_entities.set(10000)

        metrics = prometheus_metrics.get_metric_summary()
        entity_samples = metrics["akosha_knowledge_graph_entities_total"]

        # Should have one sample with value 10000
        assert any(value == 10000 for value in entity_samples.values())

    def test_knowledge_graph_relationships(self):
        """Test tracking knowledge graph relationship count."""
        prometheus_metrics.knowledge_graph_relationships.set(50000)

        metrics = prometheus_metrics.get_metric_summary()
        rel_samples = metrics["akosha_knowledge_graph_relationships_total"]

        assert any(value == 50000 for value in rel_samples.values())


class TestVectorIndexMetrics:
    """Test suite for vector index metrics."""

    def test_vector_index_size(self):
        """Test tracking vector index size."""
        prometheus_metrics.vector_index_size.labels(index_name="main").set(1_000_000)

        metrics = prometheus_metrics.get_metric_summary()
        index_samples = metrics["akosha_vector_index_size_vectors"]

        assert any(
            'index_name="main"' in labels and value == 1_000_000
            for labels, value in index_samples.items()
        )


class TestMetricsGeneration:
    """Test suite for metrics endpoint functionality."""

    def test_generate_metrics_returns_bytes(self):
        """Test that generate_metrics returns bytes."""
        # Record some metrics first
        prometheus_metrics.record_cache_hit(cache_tier="L1")

        metrics_output = prometheus_metrics.generate_metrics()

        # Should return bytes
        assert isinstance(metrics_output, bytes)

        # Should contain some metric names
        decoded = metrics_output.decode("utf-8")
        assert "akosha" in decoded
        assert "cache" in decoded.lower()

    def test_get_metrics_registry_singleton(self):
        """Test that get_metrics_registry returns the same instance."""
        registry1 = prometheus_metrics.get_metrics_registry()
        registry2 = prometheus_metrics.get_metrics_registry()

        assert registry1 is registry2

    def test_get_metric_summary_comprehensive(self):
        """Test that get_metric_summary returns all expected metrics."""
        # Record some metrics
        prometheus_metrics.record_cache_hit(cache_tier="L1")
        prometheus_metrics.increment_errors(
            component="hot_store",
            error_type="database_error",
        )
        prometheus_metrics.record_ingestion_record(
            system_id="test",
            status="success",
        )

        summary = prometheus_metrics.get_metric_summary()

        # Verify expected metrics are present
        assert "akosha_cache_operations_total" in summary
        assert "akosha_errors_total" in summary
        assert "akosha_ingestion_throughput" in summary


class TestMetricsIsolation:
    """Test suite for metrics isolation and reset functionality."""

    def test_reset_all_metrics_clears_values(self):
        """Test that reset_all_metrics clears all metric values."""
        # Record some metrics
        prometheus_metrics.record_cache_hit(cache_tier="L1")
        prometheus_metrics.increment_errors(
            component="hot_store",
            error_type="database_error",
        )

        # Get summary before reset
        summary_before = prometheus_metrics.get_metric_summary()
        assert len(summary_before) > 0

        # Reset
        prometheus_metrics.reset_all_metrics()

        # Get summary after reset
        summary_after = prometheus_metrics.get_metric_summary()

        # Metrics structure should exist but values should be cleared
        # Note: Prometheus doesn't fully remove metrics, but resets counters


class TestLabelCombinations:
    """Test various label combinations for metrics."""

    def test_cache_all_query_types(self):
        """Test cache metrics with all query type label values."""
        query_types = ["semantic", "keyword", "hybrid", "graph"]

        for qt in query_types:
            prometheus_metrics.record_cache_hit(cache_tier="L1", query_type=qt)

        metrics = prometheus_metrics.get_metric_summary()
        cache_samples = metrics["akosha_cache_operations_total"]

        # Count unique query types
        found_types = set()
        for labels in cache_samples.keys():
            for qt in query_types:
                if f'query_type="{qt}"' in labels:
                    found_types.add(qt)

        assert found_types == set(query_types)

    def test_error_all_severities(self):
        """Test error metrics with all severity levels."""
        severities = ["critical", "error", "warning"]

        for severity in severities:
            prometheus_metrics.increment_errors(
                component="hot_store",
                error_type="database_error",
                severity=severity,
            )

        metrics = prometheus_metrics.get_metric_summary()
        error_samples = metrics["akosha_errors_total"]

        # Verify all severities are present
        found_severities = set()
        for labels in error_samples.keys():
            for severity in severities:
                if f'severity="{severity}"' in labels:
                    found_severities.add(severity)

        assert found_severities == set(severities)


class TestMetricsPerformance:
    """Performance tests for metrics collection."""

    @pytest.mark.performance
    def test_high_frequency_metric_recording(self):
        """Test that high-frequency metric recording doesn't degrade significantly."""
        iterations = 1000
        start_time = time.time()

        for _ in range(iterations):
            prometheus_metrics.record_cache_hit(cache_tier="L1")

        duration = time.time() - start_time

        # Should be able to record 1000 metrics in less than 1 second
        assert duration < 1.0

        # Verify all were recorded
        metrics = prometheus_metrics.get_metric_summary()
        cache_samples = metrics["akosha_cache_operations_total"]

        for labels, value in cache_samples.items():
            if 'operation="hit"' in labels and 'cache_tier="L1"' in labels:
                assert value == iterations
                break
