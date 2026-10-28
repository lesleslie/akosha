"""Unit tests for ``akosha.mcp.tools.ecosystem_skills_cache`` (Phase 4).

Covers the file-based response cache (``~/.akosha/cache/ecosystem_skills.json``):

* TTL freshness (default 300s, env override).
* Stale flag when TTL expires.
* Atomic write via tmp + rename.
* Version-change invalidation surfaced via ``(key, _)`` matrix.
* Missing-file / corrupted JSON fall-throughs (best-effort cache, never fatal).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from akosha.mcp.tools.ecosystem_skills_cache import (
    DEFAULT_TTL_SECONDS,
    EcosystemSkillsCache,
)


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def cache_path(tmp_path: Path) -> Iterator[Path]:
    """Yield a per-test cache path; clean ``AKOSHA_FEDERATION_CACHE_TTL`` env."""
    saved = os.environ.pop("AKOSHA_FEDERATION_CACHE_TTL", None)
    target = tmp_path / "ecosystem_skills.json"
    try:
        yield target
    finally:
        if saved is not None:
            os.environ["AKOSHA_FEDERATION_CACHE_TTL"] = saved


class TestEcosystemSkillsCache:
    def test_get_returns_none_for_missing_key(self, cache_path: Path) -> None:
        clock = _Clock()
        cache = EcosystemSkillsCache(path=cache_path, clock=clock)
        snapshot, stale = cache.get("k")
        assert snapshot is None
        assert stale is False

    def test_put_then_get_returns_snapshot(self, cache_path: Path) -> None:
        clock = _Clock()
        cache = EcosystemSkillsCache(path=cache_path, ttl_seconds=300, clock=clock)
        cache.put("k", {"data": [1, 2, 3], "errors": {}})
        snapshot, stale = cache.get("k")
        assert snapshot is not None
        assert snapshot["data"] == [1, 2, 3]
        assert snapshot["errors"] == {}
        assert stale is False

    def test_get_marks_stale_after_ttl(self, cache_path: Path) -> None:
        clock = _Clock()
        cache = EcosystemSkillsCache(path=cache_path, ttl_seconds=60, clock=clock)
        cache.put("k", {"data": [], "errors": {}})
        # Just under TTL — fresh.
        clock.advance(59)
        _snapshot, stale = cache.get("k")
        assert stale is False
        # Exactly past TTL — stale.
        clock.advance(2)
        _snapshot, stale = cache.get("k")
        assert stale is True

    def test_invalidate_single_key(self, cache_path: Path) -> None:
        clock = _Clock()
        cache = EcosystemSkillsCache(path=cache_path, ttl_seconds=300, clock=clock)
        cache.put("k1", {"data": [1]})
        cache.put("k2", {"data": [2]})
        cache.invalidate("k1")
        s1, _ = cache.get("k1")
        s2, _ = cache.get("k2")
        assert s1 is None
        assert s2 is not None

    def test_invalidate_all_clears_file(self, cache_path: Path) -> None:
        clock = _Clock()
        cache = EcosystemSkillsCache(path=cache_path, ttl_seconds=300, clock=clock)
        cache.put("k1", {"data": [1]})
        cache.put("k2", {"data": [2]})
        cache.invalidate()
        s1, _ = cache.get("k1")
        s2, _ = cache.get("k2")
        assert s1 is None
        assert s2 is None

    def test_atomic_write_creates_tmp_then_rename(self, cache_path: Path) -> None:
        clock = _Clock()
        cache = EcosystemSkillsCache(path=cache_path, ttl_seconds=300, clock=clock)
        cache.put("k", {"data": [42]})
        # File exists and is valid JSON, not a half-finished tmp.
        assert cache_path.is_file()
        assert not any(cache_path.parent.glob("*.tmp"))
        parsed = json.loads(cache_path.read_text(encoding="utf-8"))
        assert "k" in parsed
        assert parsed["k"]["_fetched_at"] == 1000.0

    def test_corrupted_cache_treated_as_empty(self, cache_path: Path) -> None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text("not-json", encoding="utf-8")
        cache = EcosystemSkillsCache(path=cache_path, ttl_seconds=300, clock=_Clock())
        snapshot, stale = cache.get("k")
        assert snapshot is None
        assert stale is False

    def test_get_returns_independent_copy(self, cache_path: Path) -> None:
        clock = _Clock()
        cache = EcosystemSkillsCache(path=cache_path, ttl_seconds=300, clock=clock)
        cache.put("k", {"data": [{"id": "x"}]})
        snap_a, _ = cache.get("k")
        assert snap_a is not None
        snap_a["data"].append({"id": "y"})
        # Mutating the returned snapshot MUST NOT affect future reads.
        snap_b, _ = cache.get("k")
        assert snap_b is not None
        assert len(snap_b["data"]) == 1

    def test_default_ttl_is_300_seconds(self) -> None:
        from akosha.mcp.tools import ecosystem_skills_cache

        assert DEFAULT_TTL_SECONDS == 300
        assert ecosystem_skills_cache._ttl_seconds.__name__ == "_ttl_seconds"

    def test_env_override_negative_falls_back_to_default(
        self, cache_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AKOSHA_FEDERATION_CACHE_TTL", "-1")
        cache = EcosystemSkillsCache(path=cache_path, clock=_Clock())
        assert cache.ttl_seconds == DEFAULT_TTL_SECONDS

    def test_env_override_garbage_falls_back(self, cache_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AKOSHA_FEDERATION_CACHE_TTL", "two-minutes")
        cache = EcosystemSkillsCache(path=cache_path, clock=_Clock())
        assert cache.ttl_seconds == DEFAULT_TTL_SECONDS

    def test_env_override_positive_value_applies(
        self, cache_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AKOSHA_FEDERATION_CACHE_TTL", "60")
        cache = EcosystemSkillsCache(path=cache_path, clock=_Clock())
        assert cache.ttl_seconds == 60


def test_module_all_exports() -> None:
    from akosha.mcp.tools import ecosystem_skills_cache

    assert sorted(ecosystem_skills_cache.__all__) == [
        "DEFAULT_CACHE_PATH",
        "DEFAULT_TTL_SECONDS",
        "EcosystemSkillsCache",
    ]


class TestCacheWriteDegradedPath:
    """The degraded write path must not use a fixed-name temp file.

    The primary write path already used ``tempfile.mkstemp``. The
    fallback, taken when that failed, wrote through a *fixed-name*
    sibling (``<path>.json.tmp``) instead -- which every writer of the
    same cache shares, and which is a predictable path to plant a
    symlink on.
    """

    def test_a_failed_temp_creation_does_not_write_through_a_symlink(
        self, cache_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A planted symlink at the old temp path must not be written to.

        The old fallback wrote through ``<path>.json.tmp`` with
        ``Path.write_text``, which follows a symlink -- so the planted
        link's target was truncated and overwritten.
        """
        victim = cache_path.parent / "victim.txt"
        victim.write_text("precious", encoding="utf-8")
        (cache_path.parent / f"{cache_path.name}.tmp").symlink_to(victim)

        def boom(*args: object, **kwargs: object) -> tuple[int, str]:
            raise OSError("no temp space")

        monkeypatch.setattr(tempfile, "mkstemp", boom)

        cache = EcosystemSkillsCache(path=cache_path, ttl_seconds=300, clock=_Clock())
        cache.put("k", {"data": [42]})

        assert victim.read_text(encoding="utf-8") == "precious"

    def test_a_failed_temp_creation_publishes_a_regular_file(
        self, cache_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The degraded write must not publish a symlink as the cache.

        The old fallback renamed the planted symlink over the
        destination, so the cache file became a link to whatever the
        link pointed at.
        """
        victim = cache_path.parent / "victim.txt"
        victim.write_text("precious", encoding="utf-8")
        planted = cache_path.parent / f"{cache_path.name}.tmp"
        planted.symlink_to(victim)

        def boom(*args: object, **kwargs: object) -> tuple[int, str]:
            raise OSError("no temp space")

        monkeypatch.setattr(tempfile, "mkstemp", boom)

        cache = EcosystemSkillsCache(path=cache_path, ttl_seconds=300, clock=_Clock())
        cache.put("k", {"data": [42]})

        assert not cache_path.is_symlink()
        assert json.loads(cache_path.read_text(encoding="utf-8"))["k"]["data"] == [42]
        # The planted link is left exactly as it was.
        assert planted.is_symlink()
        assert victim.read_text(encoding="utf-8") == "precious"
