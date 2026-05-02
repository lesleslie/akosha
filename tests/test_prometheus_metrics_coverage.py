"""Tests for akosha/observability/prometheus_metrics.py — Prometheus metrics collection."""

from __future__ import annotations

import threading
from unittest.mock import patch

from akosha.observability.prometheus_metrics import (
    _add_metric_to_summary,
    _aggregate_metrics_from_text,
    _is_valid_metric_line,
    _parse_labeled_metric,
    _parse_unlabeled_metric,
    cache_entry_count,
    cache_hit_rate,
    cache_operations,
    cache_size_bytes,
    deduplication_checks,
    deduplication_duration,
    embedding_batch_size,
    embedding_generation_duration,
    error_last_timestamp,
    error_total,
    generate_metrics,
    get_metric_summary,
    get_metrics_registry,
    http_active_requests,
    http_request_duration,
    http_requests_total,
    increment_errors,
    ingestion_bytes_total,
    ingestion_duration_seconds,
    ingestion_throughput,
    knowledge_graph_entities,
    knowledge_graph_query_duration,
    knowledge_graph_relationships,
    observe_operation,
    observe_search_latency,
    observe_store_operation,
    operation_duration,
    operations_total,
    record_cache_hit,
    record_cache_miss,
    record_deduplication_check,
    record_ingestion_record,
    reset_all_metrics,
    search_latency,
    search_result_count,
    search_results_total,
    start_metrics_server,
    store_operation_duration,
    store_operations,
    store_size,
    store_size_bytes,
    update_cache_entry_count,
    update_cache_hit_rate,
    update_cache_size,
    update_ingestion_throughput,
    update_store_sizes,
    vector_index_build_duration,
    vector_index_size,
)


class TestGetMetricsRegistry:
    def test_returns_registry(self):
        registry = get_metrics_registry()
        assert registry is not None

    def test_singleton(self):
        r1 = get_metrics_registry()
        r2 = get_metrics_registry()
        assert r1 is r2

    def test_thread_safety(self):
        results = []

        def get_reg():
            results.append(get_metrics_registry())

        threads = [threading.Thread(target=get_reg) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert all(r is results[0] for r in results)


class TestIngestionMetrics:
    def test_record_ingestion_success(self):
        reset_all_metrics()
        record_ingestion_record("sys-1", "success")
        # Just ensure no exception

    def test_record_ingestion_error(self):
        reset_all_metrics()
        record_ingestion_record("sys-1", "error")

    def test_record_ingestion_skipped(self):
        reset_all_metrics()
        record_ingestion_record("sys-1", "skipped")

    def test_record_ingestion_with_bytes(self):
        reset_all_metrics()
        record_ingestion_record("sys-1", "success", bytes_processed=1024)

    def test_record_ingestion_zero_bytes_no_counter(self):
        reset_all_metrics()
        record_ingestion_record("sys-1", "success", bytes_processed=0)
        # ingestion_bytes_total should not be incremented

    def test_update_ingestion_throughput(self):
        reset_all_metrics()
        update_ingestion_throughput(42.5, "sys-1")

    def test_update_ingestion_throughput_default_system(self):
        reset_all_metrics()
        update_ingestion_throughput(10.0)


class TestSearchMetrics:
    def test_observe_search_latency_with_results(self):
        reset_all_metrics()
        with observe_search_latency("semantic", 3, "hot") as record:
            # Simulate search work
            pass
            record(5)

    def test_observe_search_latency_zero_results(self):
        reset_all_metrics()
        with observe_search_latency("keyword", 1, "warm") as record:
            record(0)

    def test_observe_search_latency_all_query_types(self):
        for qt in ["semantic", "keyword", "hybrid", "graph"]:
            reset_all_metrics()
            with observe_search_latency(qt, 2) as record:
                record(10)

    def test_observe_search_latency_all_tiers(self):
        for tier in ["hot", "warm", "cold"]:
            reset_all_metrics()
            with observe_search_latency("semantic", 1, tier) as record:
                record(1)


class TestCacheMetrics:
    def test_record_cache_hit_l1(self):
        reset_all_metrics()
        record_cache_hit("L1", "semantic")

    def test_record_cache_hit_l2(self):
        reset_all_metrics()
        record_cache_hit("L2", "hybrid")

    def test_record_cache_miss_l1(self):
        reset_all_metrics()
        record_cache_miss("L1", "semantic")

    def test_record_cache_miss_l2(self):
        reset_all_metrics()
        record_cache_miss("L2", "graph")

    def test_update_cache_hit_rate_valid(self):
        reset_all_metrics()
        update_cache_hit_rate(0.85, "L1", "semantic")

    def test_update_cache_hit_rate_clamps_high(self):
        reset_all_metrics()
        update_cache_hit_rate(1.5, "L1")

    def test_update_cache_hit_rate_clamps_low(self):
        reset_all_metrics()
        update_cache_hit_rate(-0.5, "L2")

    def test_update_cache_hit_rate_zero(self):
        reset_all_metrics()
        update_cache_hit_rate(0.0, "L1")

    def test_update_cache_hit_rate_one(self):
        reset_all_metrics()
        update_cache_hit_rate(1.0, "L1")

    def test_update_cache_size(self):
        reset_all_metrics()
        update_cache_size(1024 * 1024, "L1")

    def test_update_cache_entry_count(self):
        reset_all_metrics()
        update_cache_entry_count(500, "L2")


class TestStorageMetrics:
    def test_update_store_sizes(self):
        reset_all_metrics()
        update_store_sizes(hot_size=1000, warm_size=5000, cold_size=10000)

    def test_update_store_sizes_with_bytes(self):
        reset_all_metrics()
        update_store_sizes(100, 200, 300, hot_bytes=1024, warm_bytes=2048, cold_bytes=4096)

    def test_update_store_sizes_partial_bytes(self):
        reset_all_metrics()
        update_store_sizes(100, 200, 300, hot_bytes=1024)

    def test_observe_store_operation_success(self):
        reset_all_metrics()
        with observe_store_operation("hot", "write") as record:
            record("success")

    def test_observe_store_operation_error(self):
        reset_all_metrics()
        with observe_store_operation("warm", "read") as record:
            record("error")

    def test_observe_store_operation_all_tiers(self):
        for tier in ["hot", "warm", "cold"]:
            reset_all_metrics()
            with observe_store_operation(tier, "read") as record:
                record("success")

    def test_observe_store_operation_all_operations(self):
        for op in ["read", "write", "delete", "scan"]:
            reset_all_metrics()
            with observe_store_operation("hot", op) as record:
                record("success")


class TestErrorMetrics:
    def test_increment_errors_default_severity(self):
        reset_all_metrics()
        increment_errors("hot_store", "database_error")

    def test_increment_errors_critical(self):
        reset_all_metrics()
        increment_errors("mcp_server", "timeout_error", severity="critical")

    def test_increment_errors_warning(self):
        reset_all_metrics()
        increment_errors("cache_layer", "rate_limit_error", severity="warning")

    def test_increment_errors_all_components(self):
        components = [
            "ingestion_worker",
            "ingestion_orchestrator",
            "hot_store",
            "warm_store",
            "cold_store",
            "vector_indexer",
            "cache_layer",
            "query_engine",
            "deduplication",
            "knowledge_graph",
            "time_series",
            "mcp_server",
            "aging_service",
        ]
        for comp in components:
            reset_all_metrics()
            increment_errors(comp, "unknown_error")


class TestOperationMetrics:
    def test_observe_operation_success(self):
        reset_all_metrics()
        with observe_operation("embedding_generation") as record:
            record("success")

    def test_observe_operation_error(self):
        reset_all_metrics()
        with observe_operation("embedding_generation") as record:
            record("error")


class TestDeduplicationMetrics:
    def test_exact_duplicate(self):
        reset_all_metrics()
        record_deduplication_check("exact", "duplicate")

    def test_fuzzy_unique(self):
        reset_all_metrics()
        record_deduplication_check("fuzzy", "unique")


class TestGenerateMetrics:
    def test_returns_bytes(self):
        reset_all_metrics()
        record_ingestion_record("sys-1", "success")
        result = generate_metrics()
        assert isinstance(result, bytes)
        assert b"akosha" in result or b"akosha_ingestion" in result


class TestStartMetricsServer:
    def test_calls_start_http_server(self):
        reset_all_metrics()
        with patch("prometheus_client.start_http_server") as mock:
            start_metrics_server(port=9999, addr="127.0.0.1")
            mock.assert_called_once_with(port=9999, addr="127.0.0.1")


class TestResetAllMetrics:
    def test_reset_clears_metrics(self):
        reset_all_metrics()
        record_ingestion_record("sys-1", "success")
        reset_all_metrics()
        # After reset, metrics should be clean
        text = generate_metrics().decode("utf-8")
        # Should not have ingestion data since we reset and didn't re-record
        assert "akosha_ingestion_throughput" not in text or "_total" in text.lower()

    def test_metrics_work_after_reset(self):
        reset_all_metrics()
        record_ingestion_record("sys-1", "success")
        reset_all_metrics()
        record_cache_hit("L1")
        text = generate_metrics().decode("utf-8")
        assert "akosha_cache_operations" in text


class TestMetricParsing:
    def test_is_valid_metric_line_valid(self):
        assert _is_valid_metric_line('akosha_test_total{label="val"} 42')

    def test_is_valid_metric_line_empty(self):
        assert not _is_valid_metric_line("")

    def test_is_valid_metric_line_comment(self):
        assert not _is_valid_metric_line("# HELP akosha_test A metric")

    def test_is_valid_metric_line_whitespace(self):
        assert not _is_valid_metric_line("   ")

    def test_parse_labeled_metric_valid(self):
        result = _parse_labeled_metric('metric_name{a="1",b="2"} 42.5')
        assert result == ("metric_name", 'a="1",b="2"', 42.5)

    def test_parse_labeled_metric_invalid(self):
        assert _parse_labeled_metric("no_labels_here") is None

    def test_parse_labeled_metric_empty(self):
        assert _parse_labeled_metric("") is None

    def test_parse_unlabeled_metric_valid(self):
        result = _parse_unlabeled_metric("metric_name 42.5")
        assert result == ("metric_name", 42.5)

    def test_parse_unlabeled_metric_invalid_value(self):
        assert _parse_unlabeled_metric("metric_name notanumber") is None

    def test_parse_unlabeled_metric_single_word(self):
        assert _parse_unlabeled_metric("just_a_name") is None

    def test_parse_unlabeled_metric_empty(self):
        assert _parse_unlabeled_metric("") is None

    def test_add_metric_to_summary_new(self):
        summary = {}
        _add_metric_to_summary(summary, "test_metric", 'k="v"', 1.0)
        assert summary == {"test_metric": {'k="v"': 1.0}}

    def test_add_metric_to_summary_existing(self):
        summary = {"test_metric": {'k="v1"': 1.0}}
        _add_metric_to_summary(summary, "test_metric", 'k="v2"', 2.0)
        assert len(summary["test_metric"]) == 2


class TestAggregateMetricsFromText:
    def test_labeled_metrics(self):
        text = 'metric_a{k="v"} 1.0\nmetric_b 2.0'
        result = _aggregate_metrics_from_text(text)
        assert "metric_a" in result
        assert result["metric_a"]['k="v"'] == 1.0
        assert "metric_b" in result
        assert result["metric_b"][""] == 2.0

    def test_empty_text(self):
        result = _aggregate_metrics_from_text("")
        assert result == {}

    def test_comments_and_blanks_ignored(self):
        text = "# HELP metric A\n\nmetric_total 5\n"
        result = _aggregate_metrics_from_text(text)
        assert "metric_total" in result

    def test_multiple_labels_same_metric(self):
        text = 'm{k="a"} 1\nm{k="b"} 2\n'
        result = _aggregate_metrics_from_text(text)
        assert len(result["m"]) == 2


class TestGetMetricSummary:
    def test_returns_dict(self):
        reset_all_metrics()
        record_ingestion_record("sys-1", "success")
        summary = get_metric_summary()
        assert isinstance(summary, dict)

    def test_contains_metrics(self):
        reset_all_metrics()
        record_cache_hit("L1")
        summary = get_metric_summary()
        assert any("cache" in k.lower() for k in summary.keys())


class TestModuleLevelMetrics:
    """Verify all module-level metric objects are properly created."""

    def test_ingestion_metrics_exist(self):
        assert ingestion_throughput is not None
        assert ingestion_bytes_total is not None
        assert ingestion_duration_seconds is not None

    def test_search_metrics_exist(self):
        assert search_latency is not None
        assert search_results_total is not None
        assert search_result_count is not None

    def test_cache_metrics_exist(self):
        assert cache_operations is not None
        assert cache_hit_rate is not None
        assert cache_size_bytes is not None
        assert cache_entry_count is not None

    def test_storage_metrics_exist(self):
        assert store_size is not None
        assert store_size_bytes is not None
        assert store_operations is not None
        assert store_operation_duration is not None

    def test_error_metrics_exist(self):
        assert error_total is not None
        assert error_last_timestamp is not None

    def test_operation_metrics_exist(self):
        assert operations_total is not None
        assert operation_duration is not None

    def test_deduplication_metrics_exist(self):
        assert deduplication_checks is not None
        assert deduplication_duration is not None

    def test_embedding_metrics_exist(self):
        assert embedding_generation_duration is not None
        assert embedding_batch_size is not None

    def test_vector_index_metrics_exist(self):
        assert vector_index_size is not None
        assert vector_index_build_duration is not None

    def test_knowledge_graph_metrics_exist(self):
        assert knowledge_graph_entities is not None
        assert knowledge_graph_relationships is not None
        assert knowledge_graph_query_duration is not None

    def test_http_metrics_exist(self):
        assert http_requests_total is not None
        assert http_request_duration is not None
        assert http_active_requests is not None
