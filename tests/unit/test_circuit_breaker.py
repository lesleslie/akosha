"""Tests for circuit breaker resilience patterns."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from akosha.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerError,
    CircuitState,
)


class TestCircuitBreakerConfig:
    """Test suite for CircuitBreakerConfig."""

    def test_default_config(self) -> None:
        """Test default configuration values."""
        config = CircuitBreakerConfig()

        assert config.failure_threshold == 5
        assert config.success_threshold == 2
        assert config.timeout == 60.0
        assert config.call_timeout == 30.0
        assert config.half_open_max_calls == 3


class TestCircuitBreaker:
    """Test suite for CircuitBreaker."""

    @pytest.fixture
    def breaker(self) -> CircuitBreaker:
        """Create circuit breaker with test configuration."""
        config = CircuitBreakerConfig(
            failure_threshold=3,
            success_threshold=2,
            timeout=5.0,
            call_timeout=1.0,
        )
        return CircuitBreaker("test-service", config)

    @pytest.mark.asyncio
    async def test_initial_state(self, breaker: CircuitBreaker) -> None:
        """Test circuit starts in CLOSED state."""
        assert breaker.state == CircuitState.CLOSED
        assert breaker.stats.total_calls == 0

    @pytest.mark.asyncio
    async def test_successful_call(self, breaker: CircuitBreaker) -> None:
        """Test successful call through circuit breaker."""

        async def success_func():
            return "success"

        result = await breaker.call(success_func)

        assert result == "success"
        assert breaker.stats.total_calls == 1
        assert breaker.stats.successful_calls == 1
        assert breaker.stats.failed_calls == 0
        assert breaker.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_failed_call(self, breaker: CircuitBreaker) -> None:
        """Test failed call increments failure counter."""

        async def failing_func():
            raise ValueError("Service error")

        with pytest.raises(ValueError):
            await breaker.call(failing_func)

        assert breaker.stats.total_calls == 1
        assert breaker.stats.successful_calls == 0
        assert breaker.stats.failed_calls == 1
        assert breaker.stats.consecutive_failures == 1
        assert breaker.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_circuit_opens_after_threshold(self, breaker: CircuitBreaker) -> None:
        """Test circuit opens after failure threshold."""

        async def failing_func():
            raise ValueError("Service error")

        # Trigger failures up to threshold
        for _ in range(3):
            with pytest.raises(ValueError):
                await breaker.call(failing_func)

        # Circuit should be OPEN now
        assert breaker.state == CircuitState.OPEN
        assert breaker.stats.consecutive_failures == 3

    @pytest.mark.asyncio
    async def test_open_circuit_rejects_calls(self, breaker: CircuitBreaker) -> None:
        """Test open circuit rejects calls."""

        async def success_func():
            return "success"

        # Open the circuit
        for _ in range(3):
            try:

                async def failing_func():
                    raise ValueError("Fail")

                await breaker.call(failing_func)
            except ValueError:
                pass

        assert breaker.state == CircuitState.OPEN

        # Try to call - should be rejected
        with pytest.raises(CircuitBreakerError):
            await breaker.call(success_func)

        assert breaker.stats.rejected_calls == 1

    @pytest.mark.asyncio
    async def test_half_open_allows_limited_calls(self, breaker: CircuitBreaker) -> None:
        """Test half-open state allows limited calls for testing."""

        async def success_func():
            return "success"

        # Open the circuit
        for _ in range(3):
            try:

                async def failing_func():
                    raise ValueError("Fail")

                await breaker.call(failing_func)
            except ValueError:
                pass

        assert breaker.state == CircuitState.OPEN

        # Wait for timeout to expire
        await asyncio.sleep(0.1)  # Small delay for test
        breaker._stats.last_failure_time = None  # Force reset check

        # First successful call in half-open
        result = await breaker.call(success_func)
        assert result == "success"
        assert breaker.state == CircuitState.HALF_OPEN
        assert breaker.stats.consecutive_successes == 1

        # Second successful call should close circuit (success_threshold=2)
        result = await breaker.call(success_func)
        assert result == "success"
        assert breaker.state == CircuitState.CLOSED
        assert breaker.stats.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_half_open_failure_reopens_circuit(self, breaker: CircuitBreaker) -> None:
        """Test failure in half-open reopens circuit."""

        async def failing_func():
            raise ValueError("Service still failing")

        # Open the circuit
        for _ in range(3):
            with contextlib.suppress(ValueError):
                await breaker.call(failing_func)

        assert breaker.state == CircuitState.OPEN

        # Wait and force half-open
        await asyncio.sleep(0.1)
        breaker._stats.last_failure_time = None

        # First call fails in half-open
        with pytest.raises(ValueError):
            await breaker.call(failing_func)

        assert breaker.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_call_timeout(self, breaker: CircuitBreaker) -> None:
        """Test call timeout is handled correctly."""

        async def slow_func():
            await asyncio.sleep(2)  # Longer than call_timeout

        breaker.config.call_timeout = 0.1  # 100ms timeout

        with pytest.raises(asyncio.TimeoutError):
            await breaker.call(slow_func)

        # Timeout should count as failure
        assert breaker.stats.failed_calls == 1
        assert breaker.stats.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_get_stats_summary(self, breaker: CircuitBreaker) -> None:
        """Test statistics summary."""
        stats = breaker.get_stats_summary()

        assert stats["service_name"] == "test-service"
        assert stats["state"] == "closed"
        assert stats["total_calls"] == 0
        assert stats["success_rate"] == 0.0


class TestCircuitBreakerRegistry:
    """Test suite for CircuitBreakerRegistry."""

    @pytest.fixture
    def registry(self):  # -> CircuitBreakerRegistry
        """Create circuit breaker registry."""
        from akosha.resilience.circuit_breaker import CircuitBreakerRegistry

        return CircuitBreakerRegistry()

    def test_get_or_create_breaker(self) -> None:
        """Test getting or creating circuit breakers."""
        from akosha.resilience.circuit_breaker import CircuitBreakerRegistry

        registry = CircuitBreakerRegistry()

        breaker1 = registry.get_or_create_breaker("service-1")
        breaker2 = registry.get_or_create_breaker("service-1")

        # Should return same instance
        assert breaker1 is breaker2

        # Different service should get different breaker
        breaker3 = registry.get_or_create_breaker("service-2")
        assert breaker1 is not breaker3

    def test_get_all_stats(self) -> None:
        """Test getting all statistics."""
        from akosha.resilience.circuit_breaker import CircuitBreakerRegistry

        registry = CircuitBreakerRegistry()

        # Create multiple breakers
        registry.get_or_create_breaker("service-1")
        registry.get_or_create_breaker("service-2")

        stats = registry.get_all_stats()

        assert "service-1" in stats
        assert "service-2" in stats
        assert len(stats) == 2


class TestWithCircuitBreakerDecorator:
    """Test suite for @with_circuit_breaker decorator."""

    @pytest.mark.asyncio
    async def test_decorator_protects_function(self) -> None:
        """Test decorator adds circuit breaker protection."""
        from akosha.resilience import with_circuit_breaker
        from akosha.resilience.circuit_breaker import CircuitBreakerConfig

        call_count = 0

        # Use custom config with lower threshold for faster test
        config = CircuitBreakerConfig(
            failure_threshold=2,
            success_threshold=2,
        )

        @with_circuit_breaker("decorator_test_service", config=config)
        async def protected_function():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("Service unavailable")
            return "success"

        # First 2 calls should fail
        for _ in range(2):
            with pytest.raises(ValueError):
                await protected_function()

        # 3rd call should be rejected (circuit open)
        from akosha.resilience.circuit_breaker import CircuitBreakerError

        with pytest.raises(CircuitBreakerError):
            await protected_function()

        # Verify circuit opened
        from akosha.resilience.circuit_breaker import get_circuit_breaker_registry

        registry = get_circuit_breaker_registry()
        breaker = registry.get_or_create_breaker("decorator_test_service")
        assert breaker.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_decorator_with_custom_config(self) -> None:
        """Test decorator with custom configuration."""
        from akosha.resilience import with_circuit_breaker

        config = CircuitBreakerConfig(
            failure_threshold=2,
            timeout=1.0,
        )

        @with_circuit_breaker("custom_service", config=config)
        async def protected_function():
            return "success"

        result = await protected_function()
        assert result == "success"

    def test_decorator_sync_function(self) -> None:
        """Test decorator works with synchronous functions (non-async context)."""
        from akosha.resilience import with_circuit_breaker

        @with_circuit_breaker("sync_service_test")
        def sync_function():
            return "sync_result"

        # Call sync function from non-async context
        result = sync_function()
        assert result == "sync_result"
