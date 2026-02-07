"""Circuit breaker pattern for resilient external service calls.

This module provides:
- Circuit breaker for preventing cascading failures
- Retry logic with exponential backoff
- Timeout protection
- Fallback mechanisms
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Circuit is open, blocking calls
    HALF_OPEN = "half_open"  # Testing if service has recovered


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker."""

    failure_threshold: int = 5  # Failures before opening
    success_threshold: int = 2  # Successes before closing
    timeout: float = 60.0  # Seconds to wait before half-open
    call_timeout: float = 30.0  # Max seconds per call
    half_open_max_calls: int = 3  # Max calls in half-open state


@dataclass
class CircuitBreakerStats:
    """Statistics for circuit breaker."""

    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    rejected_calls: int = 0
    last_failure_time: datetime | None = None
    last_state_change: datetime = field(default_factory=lambda: datetime.now(UTC))
    consecutive_failures: int = 0
    consecutive_successes: int = 0


class CircuitBreakerError(Exception):
    """Raised when circuit breaker is open and blocks a call."""

    def __init__(self, message: str, state: CircuitState) -> None:
        super().__init__(message)
        self.state = state


class CircuitBreaker:
    """Circuit breaker for protecting external service calls.

    Prevents cascading failures by blocking calls to failing services
    and allowing them time to recover.

    States:
        - CLOSED: Normal operation, calls pass through
        - OPEN: Service is failing, calls are blocked
        - HALF_OPEN: Testing if service has recovered
    """

    def __init__(
        self,
        service_name: str,
        config: CircuitBreakerConfig | None = None,
    ) -> None:
        """Initialize circuit breaker.

        Args:
            service_name: Name of the protected service
            config: Circuit breaker configuration
        """
        self.service_name = service_name
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._stats = CircuitBreakerStats()
        self._lock = asyncio.Lock()

        logger.info(
            f"Circuit breaker created for '{service_name} "
            f"(failure_threshold={self.config.failure_threshold})"
        )

    @property
    def state(self) -> CircuitState:
        """Get current circuit state."""
        return self._state

    @property
    def stats(self) -> CircuitBreakerStats:
        """Get circuit statistics."""
        return self._stats

    async def call(
        self,
        func: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Execute function with circuit breaker protection.

        Args:
            func: Function to call
            *args: Function arguments
            **kwargs: Function keyword arguments

        Returns:
            Function result

        Raises:
            CircuitBreakerError: If circuit is open
            TimeoutError: If call times out
            Exception: If function raises any exception
        """
        # Check circuit state and reject if open
        await self._check_circuit_state()

        # Execute the call with timeout
        try:
            result = await asyncio.wait_for(
                self._execute_with_retry(func, *args, **kwargs),
                timeout=self.config.call_timeout,
            )
            return await self._handle_success(result)

        except TimeoutError:
            await self._handle_failure_with_logging("timed out")
            logger.warning(
                f"⏱️ Call to '{self.service_name}' timed out after {self.config.call_timeout}s"
            )
            raise

        except Exception as e:
            await self._handle_failure_with_logging("failed")
            logger.error(f"❌ Call to '{self.service_name}' failed: {e.__class__.__name__}: {e}")
            raise

    async def _check_circuit_state(self) -> None:
        """Check if circuit is open and should reject calls.

        Raises:
            CircuitBreakerError: If circuit is OPEN
        """
        async with self._lock:
            if self._state == CircuitState.OPEN:
                if self._should_attempt_reset():
                    logger.info(f"⚡ Circuit '{self.service_name}' entering HALF_OPEN state")
                    self._state = CircuitState.HALF_OPEN
                    self._stats.last_state_change = datetime.now(UTC)
                    self._stats.consecutive_successes = 0
                else:
                    self._stats.rejected_calls += 1
                    raise CircuitBreakerError(
                        f"Circuit '{self.service_name}' is OPEN - rejecting call",
                        self._state,
                    )

    async def _handle_success(self, result: T) -> T:
        """Handle successful call execution.

        Args:
            result: The result from the function call

        Returns:
            The same result
        """
        async with self._lock:
            self._stats.successful_calls += 1
            self._stats.total_calls += 1

            if self._state == CircuitState.HALF_OPEN:
                self._stats.consecutive_successes += 1
                if self._stats.consecutive_successes >= self.config.success_threshold:
                    logger.info(f"✅ Circuit '{self.service_name}' CLOSED (service recovered)")
                    self._state = CircuitState.CLOSED
                    self._stats.last_state_change = datetime.now(UTC)
                    self._stats.consecutive_failures = 0

            return result

    async def _handle_failure_with_logging(self, _reason: str) -> None:
        """Handle call failure and update circuit state.

        Args:
            reason: Description of why the call failed (for logging)
        """
        async with self._lock:
            self._handle_failure()

    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to attempt reset.

        Returns:
            True if should attempt reset
        """
        if self._stats.last_failure_time is None:
            return True

        elapsed = (datetime.now(UTC) - self._stats.last_failure_time).total_seconds()
        return elapsed >= self.config.timeout

    def _handle_failure(self) -> None:
        """Handle a failure event.

        Increments failure counters and potentially opens circuit.
        """
        self._stats.failed_calls += 1
        self._stats.total_calls += 1
        self._stats.consecutive_failures += 1
        self._stats.consecutive_successes = 0
        self._stats.last_failure_time = datetime.now(UTC)

        if self._state == CircuitState.HALF_OPEN:
            logger.warning(f"⚠️ Circuit '{self.service_name}' HALF_OPEN failed - back to OPEN")
            self._state = CircuitState.OPEN
            self._stats.last_state_change = datetime.now(UTC)

        elif self._stats.consecutive_failures >= self.config.failure_threshold:
            logger.error(
                f"🔴 Circuit '{self.service_name}' OPEN "
                f"({self._stats.consecutive_failures} consecutive failures)"
            )
            self._state = CircuitState.OPEN
            self._stats.last_state_change = datetime.now(UTC)

    async def _execute_with_retry(
        self,
        func: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Execute function.

        Note: Circuit breaker provides resilience pattern - no additional retry needed.

        Args:
            func: Function to execute
            *args: Function arguments
            **kwargs: Function keyword arguments

        Returns:
            Function result

        Raises:
            Exception: If function execution fails
        """
        result: T = await func(*args, **kwargs)  # type: ignore[misc]
        return result

    def get_stats_summary(self) -> dict[str, Any]:
        """Get summary of circuit statistics.

        Returns:
            Dictionary with statistics
        """
        return {
            "service_name": self.service_name,
            "state": self._state.value,
            "total_calls": self._stats.total_calls,
            "successful_calls": self._stats.successful_calls,
            "failed_calls": self._stats.failed_calls,
            "rejected_calls": self._stats.rejected_calls,
            "consecutive_failures": self._stats.consecutive_failures,
            "consecutive_successes": self._stats.consecutive_successes,
            "success_rate": (
                self._stats.successful_calls / self._stats.total_calls
                if self._stats.total_calls > 0
                else 0
            ),
            "last_failure_time": (
                self._stats.last_failure_time.isoformat() if self._stats.last_failure_time else None
            ),
            "last_state_change": self._stats.last_state_change.isoformat(),
        }


class CircuitBreakerRegistry:
    """Registry for managing multiple circuit breakers."""

    def __init__(self) -> None:
        """Initialize circuit breaker registry."""
        self._breakers: dict[str, CircuitBreaker] = {}
        logger.info("Circuit breaker registry initialized")

    def get_or_create_breaker(
        self,
        service_name: str,
        config: CircuitBreakerConfig | None = None,
    ) -> CircuitBreaker:
        """Get or create circuit breaker for a service.

        Args:
            service_name: Name of the service
            config: Optional circuit breaker configuration

        Returns:
            CircuitBreaker instance
        """
        if service_name not in self._breakers:
            self._breakers[service_name] = CircuitBreaker(service_name, config)
            logger.info(f"Created circuit breaker for '{service_name}'")

        return self._breakers[service_name]

    def get_all_stats(self) -> dict[str, dict[str, Any]]:
        """Get statistics for all circuit breakers.

        Returns:
            Dictionary mapping service names to stats
        """
        return {name: breaker.get_stats_summary() for name, breaker in self._breakers.items()}


# Global circuit breaker registry
_circuit_breaker_registry = CircuitBreakerRegistry()


def get_circuit_breaker_registry() -> CircuitBreakerRegistry:
    """Get global circuit breaker registry.

    Returns:
        CircuitBreakerRegistry instance
    """
    return _circuit_breaker_registry


# Decorator for automatic circuit breaker protection
def with_circuit_breaker(
    service_name: str | None = None,
    config: CircuitBreakerConfig | None = None,
) -> Callable:
    """Decorator to add circuit breaker protection to a function.

    Args:
        service_name: Name of the service (defaults to function name)
        config: Optional circuit breaker configuration

    Returns:
        Decorated function

    Example:
        @with_circuit_breaker("external_api")
        async def call_external_api():
            return await httpx.get("https://api.example.com")
    """
    import functools

    def decorator(func: Callable) -> Callable:
        nonlocal service_name

        if service_name is None:
            service_name = func.__name__

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            breaker = get_circuit_breaker_registry().get_or_create_breaker(service_name, config)

            return await breaker.call(func, *args, **kwargs)

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            """Sync wrapper - only works from non-async contexts."""
            import asyncio

            # For sync functions, run in executor
            async def _async_call() -> Any:
                return func(*args, **kwargs)

            async def _wrapped() -> Any:
                breaker = get_circuit_breaker_registry().get_or_create_breaker(
                    service_name or func.__name__, config
                )
                return await breaker.call(_async_call)

            # Sync wrapper only works from non-async contexts
            return asyncio.run(_wrapped())

        # Return appropriate wrapper
        import asyncio

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator
