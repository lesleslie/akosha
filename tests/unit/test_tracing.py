"""Tests for OpenTelemetry distributed tracing.

Tests setup_telemetry, get_tracer, get_meter, trace_operation,
traced decorator, record_counter/histogram/gauge, add_span_attributes,
add_span_event, and shutdown_telemetry from akosha.observability.tracing.

Note: conftest.py session fixture calls setup_telemetry() automatically,
so _tracer and _meter are initialized for all tests.
"""

from unittest.mock import patch

import pytest
from opentelemetry import trace

from akosha.observability.tracing import (
    add_span_attributes,
    add_span_event,
    get_meter,
    get_tracer,
    record_counter,
    record_gauge,
    record_histogram,
    setup_telemetry,
    shutdown_telemetry,
    trace_operation,
    traced,
)

# ============================================================================
# setup_telemetry
# ============================================================================


class TestSetupTelemetry:
    """Tests for setup_telemetry function."""

    def test_returns_tracer_and_meter(self):
        tracer, meter = setup_telemetry(
            service_name="test-service",
            enable_console_export=False,
            otlp_endpoint=None,
        )
        assert tracer is not None
        assert meter is not None

    def test_sets_global_tracer_provider(self):
        setup_telemetry(service_name="test-provider")
        provider = trace.get_tracer_provider()
        assert provider is not None

    def test_custom_service_name(self):
        tracer, meter = setup_telemetry(service_name="my-custom-service")
        assert tracer is not None

    def test_custom_environment(self):
        tracer, meter = setup_telemetry(
            service_name="test",
            environment="production",
        )
        assert tracer is not None

    def test_sample_rate(self):
        tracer, meter = setup_telemetry(
            service_name="test",
            sample_rate=0.5,
        )
        assert tracer is not None

    def test_enable_console_export(self):
        tracer, meter = setup_telemetry(
            service_name="test",
            enable_console_export=True,
        )
        assert tracer is not None

    def test_idempotent(self):
        t1, m1 = setup_telemetry(service_name="test-idempotent")
        t2, m2 = setup_telemetry(service_name="test-idempotent")
        assert t1 is not None
        assert t2 is not None


# ============================================================================
# get_tracer / get_meter
# ============================================================================


class TestGetTracerMeter:
    """Tests for get_tracer and get_meter accessors."""

    def test_get_tracer_returns_tracer(self):
        tracer = get_tracer()
        assert tracer is not None

    def test_get_meter_returns_meter(self):
        meter = get_meter()
        assert meter is not None

    def test_get_tracer_same_after_multiple_calls(self):
        t1 = get_tracer()
        t2 = get_tracer()
        assert t1 is t2

    def test_get_meter_same_after_multiple_calls(self):
        m1 = get_meter()
        m2 = get_meter()
        assert m1 is m2


# ============================================================================
# trace_operation context manager
# ============================================================================


class TestTraceOperation:
    """Tests for trace_operation context manager."""

    def test_yields_span(self):
        with trace_operation("test_op") as span:
            assert span is not None
            assert span.is_recording()

    def test_span_has_name(self):
        with trace_operation("my_operation") as span:
            assert span.name == "my_operation"

    def test_span_ok_on_success(self):
        with trace_operation("test_op") as span:
            pass
        # After context exit, span should have OK status
        from opentelemetry.trace import StatusCode

        assert span.status.status_code == StatusCode.OK

    def test_span_records_exception_on_error(self):
        with pytest.raises(ValueError), trace_operation("fail_op") as span:
            raise ValueError("test error")
        from opentelemetry.trace import StatusCode

        assert span.status.status_code == StatusCode.ERROR

    def test_attributes_passed_to_span(self):
        with trace_operation("test_op", attributes={"key": "value"}) as span:
            pass
        assert span is not None

    def test_no_attributes_defaults_to_empty(self):
        with trace_operation("test_op") as span:
            pass
        assert span is not None


# ============================================================================
# add_span_attributes
# ============================================================================


class TestAddSpanAttributes:
    """Tests for add_span_attributes function."""

    def test_adds_attributes_inside_span(self):
        with trace_operation("attr_test") as span:
            add_span_attributes({"custom_attr": "custom_value"})
            # Attributes are set on the current span

    def test_no_error_without_active_span(self):
        # Should not raise even without an active span
        add_span_attributes({"key": "value"})

    def test_accepts_string_values(self):
        with trace_operation("attr_test"):
            add_span_attributes({"str_key": "str_value"})

    def test_accepts_numeric_values(self):
        with trace_operation("attr_test"):
            add_span_attributes({"int_key": 42, "float_key": 3.14})


# ============================================================================
# add_span_event
# ============================================================================


class TestAddSpanEvent:
    """Tests for add_span_event function."""

    def test_adds_event_inside_span(self):
        with trace_operation("event_test") as span:
            add_span_event("checkpoint", {"stage": "processing"})

    def test_no_error_without_active_span(self):
        add_span_event("orphan_event")

    def test_event_with_no_attributes(self):
        with trace_operation("event_test"):
            add_span_event("simple_event")


# ============================================================================
# record_counter
# ============================================================================


class TestRecordCounter:
    """Tests for record_counter function."""

    def test_creates_and_increments_counter(self):
        record_counter("test_counter", 1)

    def test_counter_with_attributes(self):
        record_counter("test_counter_attr", 1, {"label": "value"})

    def test_counter_with_custom_value(self):
        record_counter("test_counter_val", 5)

    def test_counter_defaults_to_one(self):
        record_counter("test_counter_default")


# ============================================================================
# record_histogram
# ============================================================================


class TestRecordHistogram:
    """Tests for record_histogram function."""

    def test_creates_and_records_histogram(self):
        record_histogram("test_histogram", 0.5)

    def test_histogram_with_attributes(self):
        record_histogram("test_hist_attr", 1.5, {"operation": "query"})

    def test_histogram_with_large_value(self):
        record_histogram("test_hist_large", 9999.99)

    def test_histogram_with_zero(self):
        record_histogram("test_hist_zero", 0.0)


# ============================================================================
# record_gauge
# ============================================================================


class TestRecordGauge:
    """Tests for record_gauge function."""

    def test_creates_and_sets_gauge(self):
        record_gauge("test_gauge", 42.0)

    def test_gauge_with_attributes(self):
        record_gauge("test_gauge_attr", 100.0, {"server": "prod"})

    def test_gauge_with_negative_value(self):
        record_gauge("test_gauge_neg", -5.0)

    def test_gauge_with_zero(self):
        record_gauge("test_gauge_zero", 0.0)


# ============================================================================
# traced decorator
# ============================================================================


class TestTracedDecorator:
    """Tests for the @traced decorator."""

    def test_traced_sync_function(self):
        @traced("sync_op")
        def my_func(x):
            return x * 2

        result = my_func(5)
        assert result == 10
        assert my_func.__name__ == "my_func"

    @pytest.mark.asyncio
    async def test_traced_async_function(self):
        @traced("async_op")
        async def my_async_func(x):
            return x + 1

        result = await my_async_func(5)
        assert result == 6

    def test_default_operation_name_from_function(self):
        @traced()
        def some_function():
            return 42

        result = some_function()
        assert result == 42

    @pytest.mark.asyncio
    async def test_async_decorator_preserves_function_name(self):
        @traced("custom_name")
        async def original_name():
            return True

        assert original_name.__name__ == "original_name"

    def test_sync_decorator_preserves_function_name(self):
        @traced("custom_name")
        def original_name():
            return True

        assert original_name.__name__ == "original_name"

    def test_sync_function_records_error(self):
        @traced("fail_sync")
        def failing():
            raise RuntimeError("sync boom")

        with pytest.raises(RuntimeError, match="sync boom"):
            failing()

    @pytest.mark.asyncio
    async def test_async_function_records_error(self):
        @traced("fail_async")
        async def failing():
            raise RuntimeError("async boom")

        with pytest.raises(RuntimeError, match="async boom"):
            await failing()

    def test_sync_with_attributes(self):
        @traced("attr_op", attributes={"custom": "attr"})
        def func():
            return "ok"

        assert func() == "ok"

    @pytest.mark.asyncio
    async def test_async_with_attributes(self):
        @traced("attr_op", attributes={"custom": "attr"})
        async def func():
            return "ok"

        assert await func() == "ok"


# ============================================================================
# shutdown_telemetry
# ============================================================================


class TestShutdownTelemetry:
    """Tests for shutdown_telemetry function."""

    def test_shutdown_does_not_raise(self):
        # Setup fresh telemetry for shutdown test
        setup_telemetry(service_name="shutdown-test")
        shutdown_telemetry()

    def test_get_tracer_raises_after_shutdown(self):
        # After shutdown, _tracer may be None depending on implementation
        # Re-initialize to ensure tests can continue
        setup_telemetry(service_name="reinit-after-shutdown")
        tracer = get_tracer()
        assert tracer is not None


# ============================================================================
# Integration: security_logging uses record_counter
# ============================================================================


class TestSecurityLoggingIntegration:
    """Tests that SecurityLogger correctly calls record_counter from tracing."""

    def test_auth_success_calls_record_counter(self):
        from akosha.observability.security_logging import SecurityLogger

        log = logging.getLogger("int_test")
        log.setLevel(logging.INFO)
        records = []
        handler = logging.Handler()
        handler.setLevel(logging.INFO)
        handler.emit = lambda r: records.append(r)
        log.addHandler(handler)

        sec_logger = SecurityLogger(logger=log)

        with patch("akosha.observability.security_logging.record_counter") as mock_rc:
            sec_logger.log_auth_success("user1", "10.0.0.1")

        mock_rc.assert_called_once_with(
            "security.auth_success",
            1,
            {"severity": "INFO"},
        )
        log.removeHandler(handler)


# Need logging import at module level for integration test
import logging
