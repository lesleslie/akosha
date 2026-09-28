"""Cache manager for Akosha.

Owns the runtime cache instance. Reads CacheConfig (Pydantic) and
delegates to oneiric.adapters.cache.memory.MemoryCacheAdapter by default.

Pre-1.0 replace-not-extend (feedback-no-backwards-compat-pre-1.0.md):
this is the FIRST runtime cache layer in Akosha; CacheConfig (Pydantic)
was config-only, never a runtime cache. The previous description in spec
§4.1 ('unbounded dict cache') was a misreading — see brief revision note.
"""

from __future__ import annotations

from typing import Any

from oneiric.adapters.cache.memory import (
    MemoryCacheAdapter,
    MemoryCacheSettings,
)

from akosha.config import CacheConfig


class CacheManager:
    """Runtime cache wrapper for Akosha.

    Reads CacheConfig (Pydantic) and owns a MemoryCacheAdapter instance
    by default. Future backends (Redis, etc.) can be plugged in here
    while CacheConfig remains the config surface.
    """

    def __init__(
        self,
        settings: CacheConfig | None = None,
        *,
        max_entries: int = 10000,
    ) -> None:
        cfg = settings or CacheConfig()
        # req: REQ-OSUB-A-003 — default backend is MemoryCacheAdapter
        if cfg.backend == "memory":
            adapter_settings = MemoryCacheSettings(
                max_entries=max_entries,
                default_ttl=float(cfg.local_ttl_seconds),
            )
            self._backend: MemoryCacheAdapter = MemoryCacheAdapter(adapter_settings)
        else:
            raise NotImplementedError(
                f"CacheManager backend '{cfg.backend}' is not yet wired in Phase A; "
                f"only 'memory' is supported. Redis backend is future work."
            )
        self._settings = cfg

    @property
    def backend(self) -> MemoryCacheAdapter:
        # req: REQ-OSUB-A-003
        return self._backend

    async def get(self, key: str) -> Any:
        # req: REQ-OSUB-A-003 — delegates to MemoryCacheAdapter.get()
        # OTel span 'cache.adapter.memory.get' emitted by adapter's internal logger
        return await self._backend.get(key)

    async def set(self, key: str, value: Any, *, ttl: float | None = None) -> None:
        # req: REQ-OSUB-A-003
        await self._backend.set(key, value, ttl=ttl)

    async def delete(self, key: str) -> None:
        # req: REQ-OSUB-A-003
        await self._backend.delete(key)

    async def delete_prefix(self, prefix: str) -> int:
        # req: REQ-OSUB-A-003
        return await self._backend.delete_prefix(prefix)

    async def clear(self) -> None:
        # req: REQ-OSUB-A-003
        await self._backend.clear()
