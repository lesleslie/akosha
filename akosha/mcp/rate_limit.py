"""Rate limiting for MCP tools to prevent abuse.

Implements token bucket algorithm for per-user rate limiting.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta
from functools import wraps
from typing import Callable, TypeVar

from akosha.observability import record_counter, record_histogram

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RateLimiter:
    """Token bucket rate limiter for MCP tools.

    Prevents abuse by limiting request rate per user.
    Uses token bucket algorithm for smooth rate limiting.

    Example:
        limiter = RateLimiter(requests_per_second=10, burst_limit=100)

        # Check if request is allowed
        if limiter.is_allowed("user-123"):
            # Process request
            pass
    """

    def __init__(
        self,
        requests_per_second: float = 10.0,
        burst_limit: int = 100,
    ) -> None:
        """Initialize rate limiter.

        Args:
            requests_per_second: Sustained request rate (tokens/sec)
            burst_limit: Maximum tokens (bucket capacity)
        """
        self.rate = requests_per_second
        self.burst = burst_limit
        self.tokens: dict[str, float] = defaultdict(lambda: burst_limit)
        self.last_update: dict[str, float] = defaultdict(time.time)
        self._lock = asyncio.Lock()

        # Metrics
        self._allow_count = 0
        self._deny_count = 0

    async def is_allowed(self, user_id: str, tokens: int = 1) -> bool:
        """Check if request is allowed for user.

        Args:
            user_id: User identifier
            tokens: Number of tokens required (default: 1)

        Returns:
            True if request is allowed, False if rate limited
        """
        async with self._lock:
            now = time.time()
            last_time = self.last_update[user_id]

            # Refill tokens based on time elapsed
            elapsed = now - last_time
            self.tokens[user_id] = min(
                self.burst,
                self.tokens[user_id] + elapsed * self.rate
            )
            self.last_update[user_id] = now

            # Check if enough tokens available
            if self.tokens[user_id] >= tokens:
                self.tokens[user_id] -= tokens
                self._allow_count += 1
                return True
            else:
                self._deny_count += 1
                logger.warning(
                    f"Rate limit exceeded for user {user_id}: "
                    f"{self.tokens[user_id]:.2f} tokens available, {tokens} required"
                )
                return False

    def get_stats(self) -> dict[str, int]:
        """Get rate limiter statistics.

        Returns:
            Dictionary with allow/deny counts
        """
        return {
            "allow_count": self._allow_count,
            "deny_count": self._deny_count,
        }


# Global rate limiter instance
_global_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    """Get global rate limiter instance.

    Returns:
        RateLimiter singleton
    """
    global _global_limiter

    if _global_limiter is None:
        # Load from environment or use defaults
        requests_per_sec = float(os.getenv("RATE_LIMIT_RPS", "10.0"))
        burst_limit = int(os.getenv("RATE_LIMIT_BURST", "100"))

        _global_limiter = RateLimiter(
            requests_per_second=requests_per_sec,
            burst_limit=burst_limit,
        )

    return _global_limiter


def require_rate_limit(
    tokens: int = 1,
    user_id_param: str = "user_id",
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator to enforce rate limiting on MCP tools.

    Args:
        tokens: Number of tokens required per request (default: 1)
        user_id_param: Parameter name containing user_id (default: "user_id")

    Returns:
        Decorated function with rate limiting

    Example:
        @require_rate_limit(tokens=1)
        async def search_all_systems(user_id: str, query: str) -> dict:
            # Function is rate limited
            pass
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args: **kwargs):  # type: ignore
            # Extract user_id from kwargs or args
            user_id = kwargs.get(user_id_param)
            if user_id is None:
                logger.warning(f"Missing {user_id_param} for rate limiting")
                # Allow request but log warning
                return await func(*args, **kwargs)

            # Check rate limit
            limiter = get_rate_limiter()
            if not await limiter.is_allowed(user_id, tokens):
                # Rate limited - return error response
                from fastapi import HTTPException

                raise HTTPException(
                    status_code=429,
                    detail="Rate limit exceeded. Please try again later.",
                )

            # Record metrics
            record_counter("mcp.rate_limit.allowed", 1, {"user_id": user_id})

            # Process request
            return await func(*args, **kwargs)

        return wrapper

    return decorator


# Import for decorator
import os


__all__ = [
    "RateLimiter",
    "get_rate_limiter",
    "require_rate_limit",
]
