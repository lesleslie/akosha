from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import akosha.mcp.rate_limit as rate_limit


@pytest.fixture(autouse=True)
def _reset_limiter(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rate_limit, "_global_limiter", None)


def test_get_rate_limiter_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_RPS", "2.5")
    monkeypatch.setenv("RATE_LIMIT_BURST", "7")

    limiter = rate_limit.get_rate_limiter()
    second = rate_limit.get_rate_limiter()

    assert limiter is second
    assert limiter.rate == 2.5
    assert limiter.burst == 7


@pytest.mark.asyncio
async def test_rate_limiter_allow_and_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    limiter = rate_limit.RateLimiter(requests_per_second=0.0, burst_limit=1)
    monkeypatch.setattr(rate_limit.time, "time", lambda: 0.0)

    assert await limiter.is_allowed("user-1") is True
    assert await limiter.is_allowed("user-1") is False
    assert limiter.get_stats() == {"allow_count": 1, "deny_count": 1}


@pytest.mark.asyncio
async def test_require_rate_limit_allowed_denied_and_missing_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class StubLimiter:
        def __init__(self):
            self.is_allowed = AsyncMock(side_effect=[True, False])

    limiter = StubLimiter()
    monkeypatch.setattr(rate_limit, "get_rate_limiter", lambda: limiter)
    monkeypatch.setattr(rate_limit, "record_counter", lambda name, value, labels: calls.append((name, labels)))

    @rate_limit.require_rate_limit(tokens=2)
    async def tool(*, user_id: str, query: str = "x") -> str:
        return f"{user_id}:{query}"

    assert await tool(user_id="user-1", query="ok") == "user-1:ok"
    assert calls == [("mcp.rate_limit.allowed", {"user_id": "user-1"})]

    with pytest.raises(HTTPException, match="Rate limit exceeded"):
        await tool(user_id="user-1", query="again")

    @rate_limit.require_rate_limit()
    async def tool_without_user(*, query: str) -> str:
        return query

    assert await tool_without_user(query="free") == "free"
