"""Warm->Cold exporter: ages warm-tier records into cold-tier Parquet batches.

Akosha's cold tier is Parquet batches in object storage (S3 / R2 / GCS /
Azure). ``ColdStore.export_batch`` is the only producer of such a batch and
this module is the only caller of it -- without this exporter the cold tier
is reachable but not writable, which is what
``AKOSHA_ARCHITECTURE_AUDIT_2026-09-09.md:44`` recorded as "DOC LIES".

The shape of the move follows the hot->warm migration that already exists
in :mod:`akosha.storage.aging`: export first, evict second. A failed upload
never destroys the source row.

Design: ``docs/specs/2026-11-05-akosha-cold-write-path-design.md``.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from akosha.json_utils import coerce_json_object
from akosha.models import ColdRecord

if TYPE_CHECKING:
    from akosha.storage.cold_store import ColdStore
    from akosha.storage.warm_store import WarmStore

logger = logging.getLogger(__name__)

#: The documented cold-tier age: "Cold Tier (90+ days)".
DEFAULT_AGE_THRESHOLD_DAYS = 90

#: Maximum number of warm rows aged out in one cycle. Bounds the memory a
#: single cycle can hold and the time it can take.
DEFAULT_BATCH_SIZE = 1000

#: Number of MinHash permutations. Matches
#: :class:`akosha.processing.deduplication.DeduplicationService` so the two
#: fingerprint paths describe the same signature space.
FINGERPRINT_PERMUTATIONS = 128

#: Mask that keeps a 64-bit MinHash value inside the cold Parquet schema's
#: ``list_(int32)`` column while preserving the per-permutation equality
#: semantics Jaccard similarity depends on.
_INT32_MASK = 0x7FFFFFFF


@dataclass
class ColdExportStats:
    """Outcome of one warm->cold export cycle."""

    records_exported: int = 0
    batches_exported: int = 0
    bytes_exported: int = 0
    records_evicted: int = 0
    errors: int = 0
    partitions: list[str] = field(default_factory=list)
    start_time: datetime | None = None
    end_time: datetime | None = None


def _naive_utc(value: datetime) -> datetime:
    """Normalise a datetime to naive UTC.

    DuckDB ``TIMESTAMP`` columns store naive wall-clock values, and the
    substrate's insert path normalises to naive UTC before writing. A
    tz-aware value bound as a query PARAMETER is shifted to the session's
    local time instead, so an aware cutoff would compare against the
    stored value with the local UTC offset applied.

    Args:
        value: The datetime to normalise.

    Returns:
        The same instant as a naive UTC datetime.
    """
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _sanitise_component(value: str | None) -> str:
    """Make a value safe to use as a single object-key path component.

    Args:
        value: The raw component (a ``system_id``, in practice). May be
            NULL or a non-string -- DuckDB columns carry no type
            constraint at the storage layer, so a malformed row must not
            raise out of the export loop.

    Returns:
        A component with no path separators in it. ``.`` and ``..`` are
        replaced outright so no value can address a parent directory. A
        missing or non-string value becomes ``_unknown`` -- a stable
        placeholder that keeps the row inside a well-formed partition
        instead of aborting the whole cycle.
    """
    if not isinstance(value, str):
        return "_unknown"
    cleaned = value.strip().replace("\\", "_").replace("/", "_")
    if cleaned in ("", ".", ".."):
        return "_"
    return cleaned


def _decode_metadata(raw: Any) -> tuple[dict[str, Any], bool]:
    """Decode a warm row's metadata blob.

    Args:
        raw: The column value as DuckDB returned it.

    Returns:
        The decoded mapping and whether it was preserved faithfully.

    Notes:
        ``coerce_json_object`` returns ``{}`` for every undecodable blob,
        and ``{}`` is also the legitimate value for a row that simply
        carries no metadata -- so its return value alone cannot tell
        "absent" from "lost". The raw blob is re-checked here: a blob
        that is present, is not the empty object, and does not decode to
        a non-empty mapping was NOT preserved, and the row must not be
        evicted -- the cold record would carry an empty mapping and the
        original would be lost.
    """
    decoded = coerce_json_object(raw)
    if not raw:
        return decoded, True
    if decoded:
        return decoded, True
    if isinstance(raw, (bytes, bytearray)):
        text = raw.decode("utf-8", errors="replace")
    elif isinstance(raw, str):
        text = raw
    else:
        return decoded, True
    # "{}" is a legitimately empty metadata mapping; "[]" and anything
    # unparseable are shapes the cold record cannot represent.
    return decoded, text.strip() in ("", "{}")


def build_partition_path(system_id: str | None, timestamp: datetime) -> str:
    """Build the partition path for a cold object.

    The convention is ``system_id/YYYY/MM/DD``, taken from the record's own
    timestamp rather than the export date: a partition is therefore stable
    (the same record always lands in the same partition, whatever day it is
    exported) and one partition maps to one Parquet object.

    Args:
        system_id: The system the record belongs to.
        timestamp: The record's timestamp -- the day the record belongs to.

    Returns:
        The partition path, e.g. ``"sys-001/2026/01/31"``.
    """
    return (
        f"{_sanitise_component(system_id)}"
        f"/{timestamp.year:04d}/{timestamp.month:02d}/{timestamp.day:02d}"
    )


def first_sentence(text: str) -> str:
    """Reduce a summary to its first sentence.

    The cold tier keeps "summaries only" -- the warm tier's three-sentence
    extractive summary is collapsed to one sentence on the way down.

    Args:
        text: The warm-tier summary.

    Returns:
        The first sentence, or the whole (stripped) text when it holds no
        sentence break.
    """
    stripped = text.strip()
    if not stripped:
        return ""
    head, separator, _ = stripped.partition(". ")
    if separator:
        return head.rstrip(".").strip()
    return stripped


def _sha256_fingerprint(text: str, num_perm: int) -> list[int]:
    """Derive a deterministic fingerprint without ``datasketch``.

    Fallback for a deployment where ``datasketch`` is not installed: a
    SHA-256 expansion into ``num_perm`` non-negative 31-bit values. It is
    not a MinHash signature -- it carries no Jaccard guarantee -- but it
    keeps the cold write path alive instead of failing the whole batch.
    """
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    values: list[int] = []
    counter = 0
    while len(values) < num_perm:
        block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        for offset in range(0, len(block), 4):
            if len(values) >= num_perm:
                break
            values.append(int.from_bytes(block[offset : offset + 4], "big") & _INT32_MASK)
        counter += 1
    return values


def minhash_fingerprint(text: str, num_perm: int = FINGERPRINT_PERMUTATIONS) -> list[int]:
    """Derive the cold tier's MinHash fingerprint for ``text``.

    ``ColdRecord.fingerprint`` is a ``list[int]`` and the cold Parquet
    schema stores it as ``list_(int32)``. datasketch's ``MinHash.hashvalues``
    are ``uint64``, so the top bit is masked off: every value stays a
    non-negative ``int32`` while the per-permutation equality semantics
    Jaccard similarity depends on are preserved.

    Args:
        text: The text to fingerprint (the warm tier's summary).
        num_perm: Number of MinHash permutations.

    Returns:
        The signature as a list of non-negative ints.
    """
    try:
        from datasketch import MinHash
    except ImportError:
        logger.warning("datasketch not installed; using a SHA-256 derived fingerprint")
        return _sha256_fingerprint(text, num_perm)
    minhash = MinHash(num_perm=num_perm)
    for word in text.split():
        minhash.update(word.encode("utf-8"))
    return [int(value) & _INT32_MASK for value in minhash.hashvalues]


class ColdExporter:
    """Moves aged warm-tier records into the cold tier as Parquet batches."""

    def __init__(
        self,
        warm_store: WarmStore,
        cold_store: ColdStore,
        *,
        age_threshold_days: int = DEFAULT_AGE_THRESHOLD_DAYS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        evict_after_export: bool = True,
    ) -> None:
        """Initialize the exporter.

        Args:
            warm_store: The warm tier -- the source of the aged records.
            cold_store: The cold tier -- the destination. It owns the
                object-key prefix, so the configured prefix reaches the key
                through it.
            age_threshold_days: Records older than this leave the warm
                tier. Defaults to the documented 90 days.
            batch_size: Maximum number of warm rows aged out per cycle.
            evict_after_export: Delete the warm rows once they have been
                exported. The export happens first, so a failed upload
                never destroys the source row.
        """
        self._warm_store = warm_store
        self._cold_store = cold_store
        self._age_threshold_days = age_threshold_days
        self._batch_size = batch_size
        self._evict_after_export = evict_after_export
        logger.info(
            "Cold exporter initialized (threshold=%dd, batch_size=%d, evict=%s)",
            age_threshold_days,
            batch_size,
            evict_after_export,
        )

    async def export_expired(self, *, now: datetime | None = None) -> ColdExportStats:
        """Export every warm record older than the age threshold.

        Args:
            now: The reference time. Defaults to the current UTC time.

        Returns:
            The cycle's statistics. A failing partition is counted in
            ``errors`` and its warm rows are kept; the cycle continues with
            the next partition.
        """
        started = now or datetime.now(UTC)
        stats = ColdExportStats(start_time=started)
        bytes_before = self._cold_store.bytes_exported
        cutoff = _naive_utc(started) - timedelta(days=self._age_threshold_days)

        rows = self._fetch_expired_rows(cutoff)
        if not rows:
            stats.end_time = datetime.now(UTC)
            logger.info(
                "cold export: no warm records older than %d days",
                self._age_threshold_days,
            )
            return stats

        for partition_path, partition_rows in self._group_by_partition(rows).items():
            records: list[ColdRecord] = []
            evictable: list[str] = []
            for row in partition_rows:
                try:
                    projected = self._to_cold_record(row)
                except Exception:
                    # One malformed row must not wedge the whole retention
                    # path. It would be re-fetched every cycle and abort
                    # the cycle before any partition is exported, so the
                    # warm tier would grow without bound while the cold
                    # tier stayed empty. Skip it, count it, and leave it
                    # in the warm tier for diagnosis.
                    stats.errors += 1
                    logger.exception(
                        "cold export: skipping malformed warm row "
                        "(conversation_id=%r) - it stays in the warm tier",
                        row.get("conversation_id"),
                    )
                    continue
                if projected is None:
                    stats.errors += 1
                    logger.error(
                        "cold export: skipping row whose metadata did not decode "
                        "(conversation_id=%r) - it stays in the warm tier",
                        row.get("conversation_id"),
                    )
                    continue
                records.append(projected)
                evictable.append(row["conversation_id"])
            if not records:
                continue
            try:
                await self._cold_store.export_batch(
                    records=records,
                    partition_path=partition_path,
                )
            except Exception:
                stats.errors += len(records)
                logger.exception("cold export failed for partition %s", partition_path)
                continue
            stats.records_exported += len(records)
            stats.batches_exported += 1
            stats.partitions.append(partition_path)
            if self._evict_after_export:
                # Only the rows that projected cleanly are evicted. A row
                # that was skipped keeps its warm-tier copy.
                evicted = self._evict(evictable)
                stats.records_evicted += evicted

        stats.bytes_exported = self._cold_store.bytes_exported - bytes_before
        stats.end_time = datetime.now(UTC)
        self._emit_metrics(stats)
        logger.info(
            "cold export: records=%d batches=%d bytes=%d evicted=%d errors=%d",
            stats.records_exported,
            stats.batches_exported,
            stats.bytes_exported,
            stats.records_evicted,
            stats.errors,
        )
        return stats

    def _fetch_expired_rows(self, cutoff: datetime) -> list[dict[str, Any]]:
        """Read the warm rows older than ``cutoff``.

        Args:
            cutoff: The age boundary.

        Returns:
            One dict per warm row, oldest first.

        Raises:
            RuntimeError: When the warm store has not been initialised.
        """
        conn = self._warm_store.conn
        if conn is None:
            raise RuntimeError("Warm store not initialized")
        rows = conn.execute(
            """
            SELECT system_id, conversation_id, summary, timestamp, metadata
            FROM conversations
            WHERE timestamp < ?
            ORDER BY timestamp ASC
            LIMIT ?
            """,
            [cutoff, self._batch_size],
        ).fetchall()
        decoded: list[dict[str, Any]] = []
        for row in rows:
            # DuckDB returns a JSON column as text, not as a mapping.
            metadata, metadata_ok = _decode_metadata(row[4])
            decoded.append(
                {
                    "system_id": row[0],
                    "conversation_id": row[1],
                    "summary": row[2] or "",
                    "timestamp": row[3],
                    "metadata": metadata,
                    # False when the blob was present but did not decode:
                    # such a row is exported nowhere and evicted nowhere.
                    "metadata_ok": metadata_ok,
                }
            )
        return decoded

    @staticmethod
    def _group_by_partition(
        rows: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        """Group the rows by the partition they belong to."""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            partition = build_partition_path(row["system_id"], row["timestamp"])
            grouped.setdefault(partition, []).append(row)
        return grouped

    @staticmethod
    def _to_cold_record(row: dict[str, Any]) -> ColdRecord | None:
        """Project a warm row onto the cold record shape.

        Returns:
            The cold record, or ``None`` when the row's metadata did not
            survive the projection. Such a row is exported nowhere and
            evicted nowhere: the cold record would carry an empty
            mapping, and evicting the warm row would destroy the
            original metadata.
        """
        if not row.get("metadata_ok", True):
            return None
        summary = row["summary"]
        return ColdRecord(
            system_id=_sanitise_component(row["system_id"]),
            conversation_id=row["conversation_id"],
            fingerprint=minhash_fingerprint(summary),
            ultra_summary=first_sentence(summary),
            timestamp=row["timestamp"],
            metadata=row["metadata"],
        )

    def _evict(self, conversation_ids: list[str]) -> int:
        """Delete the exported rows from the warm tier.

        Args:
            conversation_ids: The rows that were exported successfully.

        Returns:
            The number of rows deleted.
        """
        if not conversation_ids:
            return 0
        conn = self._warm_store.conn
        if conn is None:
            raise RuntimeError("Warm store not initialized")
        placeholders = ", ".join("?" for _ in conversation_ids)
        conn.execute(
            f"DELETE FROM conversations WHERE conversation_id IN ({placeholders})",
            conversation_ids,
        )
        logger.debug("Evicted %d rows from the warm tier", len(conversation_ids))
        return len(conversation_ids)

    def _emit_metrics(self, stats: ColdExportStats) -> None:
        """Publish the cycle's size and migration counters.

        The gauge carries the cold store's CUMULATIVE exported byte count,
        not this cycle's delta: a gauge has to describe a total, and the
        store is the only component that knows how many bytes have gone
        through it.
        """
        from akosha.monitoring.metrics import (
            cold_store_size_bytes,
            migration_records_total,
        )

        cold_store_size_bytes.set(self._cold_store.bytes_exported)
        if stats.records_exported:
            migration_records_total.labels(
                source_tier="warm",
                target_tier="cold",
                status="success",
            ).inc(stats.records_exported)
        if stats.errors:
            migration_records_total.labels(
                source_tier="warm",
                target_tier="cold",
                status="error",
            ).inc(stats.errors)
