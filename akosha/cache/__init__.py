"""Akosha cache layer (Phase A).

Re-exports CacheManager and MemoryCacheAdapter for callers.
"""

from __future__ import annotations

from oneiric.adapters.cache.memory import MemoryCacheAdapter

from akosha.cache.manager import CacheManager

__all__ = ["CacheManager", "MemoryCacheAdapter"]
