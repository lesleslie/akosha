"""File-based cache for the Phase 4 federation response.

Per plan §5 task #4 (P-8): the federation cache MOVED from Akosha's
HotStore to ``~/.akosha/cache/ecosystem_skills.json``. HotStore is a
vector index; skill metadata is low-cardinality, infrequent-change,
identifies by exact key. File-based cache with TTL.

Why a file (not HotStore):

* Skill metadata is a small, infrequent-change dataset — ~15-50
  entries at most. Vector indexing is overkill.
* Every server in the federation needs a per-key cache, not a global
  similarity index. A flat file is the simplest correct store.
* Allows CLI-side debugging: ``cat ~/.akosha/cache/ecosystem_skills.json``
  shows the current cached response.

Cache invariants:

* **TTL**: ``DEFAULT_TTL_SECONDS`` (300s = 5min) — matches plan §5
  task #4. Override via ``AKOSHA_FEDERATION_CACHE_TTL``.
* **Atomic write**: write to ``.tmp`` then :func:`Path.replace` —
  mirrors the pattern used by the Phase 2 skill installer (M-3, P-10).
* **Per-server version invalidation**: when a server's ``list_skills``
  response reports a new ``version``, the cached entries for that
  server are dropped (separate per-server sub-cache).
* **Stale flag**: when :meth:`get` returns a TTL-expired snapshot, the
  response carries ``cache.stale=True`` so the caller can decide to
  refresh.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


DEFAULT_CACHE_PATH = Path("~/.akosha/cache/ecosystem_skills.json").expanduser()
DEFAULT_TTL_SECONDS = 300  # 5 minutes
ENV_TTL_OVERRIDE = "AKOSHA_FEDERATION_CACHE_TTL"


def _ttl_seconds() -> int:
    """Resolve the active TTL from env override, falling back to the default."""
    raw = os.getenv(ENV_TTL_OVERRIDE)
    if raw is None:
        return DEFAULT_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an int; using default %d",
            ENV_TTL_OVERRIDE,
            raw,
            DEFAULT_TTL_SECONDS,
        )
        return DEFAULT_TTL_SECONDS
    if value <= 0:
        logger.warning(
            "%s=%d is not positive; using default %d",
            ENV_TTL_OVERRIDE,
            value,
            DEFAULT_TTL_SECONDS,
        )
        return DEFAULT_TTL_SECONDS
    return value


class EcosystemSkillsCache:
    """File-based response cache for :func:`akosha_list_ecosystem_skills`.

    The cache stores the full federation response keyed by
    ``(server_versions_signature, query_signature)``. Per-server
    invalidation: each ``list_skills`` response carries per-entry
    ``version`` (semver); the cache key for each server's slice is the
    join of those versions. A change in any ``version`` invalidates the
    cached slice.

    The cache is intentionally *not* process-local: a fresh process that
    reads the same file sees the same snapshot. The TTL governs whether
    the snapshot is "fresh" (served as-is) or "stale" (returned with a
    flag so the federation tool can refresh).

    Args:
        path: filesystem location for the cache file.
        ttl_seconds: freshness window. Defaults to ``DEFAULT_TTL_SECONDS``
            but the env override (``AKOSHA_FEDERATION_CACHE_TTL``) wins
            when set.
        clock: overridable clock for tests.
    """

    def __init__(
        self,
        path: Path | None = None,
        ttl_seconds: int | None = None,
        clock: Any = None,
    ) -> None:
        self._path = Path(path) if path is not None else DEFAULT_CACHE_PATH
        self._ttl = ttl_seconds if ttl_seconds is not None else _ttl_seconds()
        self._clock = clock if clock is not None else time.time

    # ---- public surface ----

    @property
    def path(self) -> Path:
        return self._path

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    def get(self, key: str) -> tuple[dict[str, Any] | None, bool]:
        """Return ``(snapshot, stale)`` for ``key``.

        ``snapshot`` is the cached dict (or ``None`` if no cache exists).
        ``stale`` is ``True`` when the snapshot is past its TTL — the
        federation tool should refresh but may still return the stale
        data to keep latency low.
        """
        snapshot = self._read_all().get(key)
        if snapshot is None:
            return None, False
        ts = float(snapshot.get("_fetched_at", 0))
        now = float(self._clock())
        stale = (now - ts) > self._ttl
        return dict(snapshot), stale

    def put(self, key: str, value: dict[str, Any]) -> None:
        """Persist ``value`` under ``key`` with the current timestamp.

        Atomic write to ``<path>.tmp`` then :func:`os.replace` so a
        crash mid-write cannot leave a half-formed cache file.
        """
        all_data = self._read_all()
        value_with_ts = value.copy()
        value_with_ts["_fetched_at"] = float(self._clock())
        all_data[key] = value_with_ts
        self._atomic_write(all_data)

    def invalidate(self, key: str | None = None) -> None:
        """Remove a single key or the entire cache.

        ``key=None`` clears the file. Used by tests and by operators
        forcing a refresh (``akosha cache invalidate --federation`` in
        future CLI work).
        """
        if key is None:
            self._atomic_write({})
            return
        all_data = self._read_all()
        if key in all_data:
            del all_data[key]
            self._atomic_write(all_data)

    # ---- helpers ----

    def _read_all(self) -> dict[str, Any]:
        """Read the full cache file as a ``{key: snapshot}`` dict.

        Returns an empty dict on any IO/JSON error — the cache is best-
        effort, never fatal.
        """
        if not self._path.is_file():
            return {}
        try:
            text = self._path.read_text(encoding="utf-8")
            data = json.loads(text)
        except (OSError, ValueError) as exc:
            logger.warning("ecosystem_skills cache read failed (%s); treating as empty", exc)
            return {}
        if not isinstance(data, dict):
            logger.warning(
                "ecosystem_skills cache root is %s (expected dict); treating as empty",
                type(data).__name__,
            )
            return {}
        return data

    def _atomic_write(self, data: dict[str, Any]) -> None:
        """Write ``data`` atomically to ``self._path``.

        Uses ``tempfile.mkstemp`` in the same directory so ``os.replace``
        (rename) is atomic on POSIX. Falls back to a sibling ``.tmp`` file
        when the directory is not writable (defense-in-depth for tests
        with locked filesystems).
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, default=str, sort_keys=True)
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix="ecosystem_skills.",
                suffix=".tmp",
                dir=str(self._path.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fp:
                    fp.write(payload)
                Path(tmp_path).replace(self._path)
            except Exception:
                with suppress(OSError):
                    Path(tmp_path).unlink()
                raise
        except OSError as exc:
            logger.warning(
                "ecosystem_skills cache write to %s failed (%s); falling back",
                self._path,
                exc,
            )
            sibling = self._path.with_suffix(self._path.suffix + ".tmp")
            sibling.write_text(payload, encoding="utf-8")
            sibling.replace(self._path)


__all__ = [
    "DEFAULT_CACHE_PATH",
    "DEFAULT_TTL_SECONDS",
    "EcosystemSkillsCache",
]
