#!/usr/bin/env python3
"""Recover the ``bodai:events`` consumer group after a subscriber leak.

What went wrong
---------------
``BodaiToolInvocationSubscriber`` generated a fresh ``uuid4`` consumer
name inside its read loop, so every ``XREADGROUP`` call registered a
NEW consumer on the ``akosha-tool-invocation-indexers`` group. Over six
weeks that accumulated 446,049 consumers. The group's PEL bookkeeping
grew with them, and ``XINFO CONSUMERS`` took 158 seconds to answer —
long enough that the health check timed out and the ingestion pipeline
looked dead.

The subscriber is fixed (one stable consumer name per process). This
script is the cleanup for the damage the leak already did.

What it deletes
---------------
A consumer is deleted only when BOTH hold:

1. its name starts with ``akosha-`` — the prefix
   ``BodaiToolInvocationSubscriber._auto_consumer_name`` generates, so
   it is ours and nobody else's;
2. its ``pending`` count is 0 — ``XGROUP DELCONSUMER`` DESTROYS every
   message still pending for the consumer. The message is neither acked
   nor redelivered, and ``XAUTOCLAIM`` can no longer recover it. A
   message that failed to index must not die because a cleanup ran.

The group itself is never deleted: it holds the backlog.

Usage
-----
    python scripts/recover_event_stream.py --dry-run    # default
    python scripts/recover_event_stream.py --apply

``--dry-run`` reports what would be deleted and touches nothing.
``--apply`` deletes and re-measures, so the report shows the effect.

The Redis URL comes from ``REDIS_URL`` (default
``redis://localhost:6379/0``). The DSN is read from the environment at
call time and never written into a committed file.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import TYPE_CHECKING, Any

#: The stream the subscriber reads. Matches
#: ``akosha.ingestion.bodai_event_subscriber.STREAM_NAME``.
STREAM_NAME: str = "bodai:events"

#: The consumer group the subscriber registers under. Matches the
#: ``consumer_group`` default in ``settings/akosha.yaml``.
CONSUMER_GROUP: str = "akosha-tool-invocation-indexers"

#: Only consumers whose name starts with this prefix are ours. It is the
#: prefix ``_auto_consumer_name`` generates.
CONSUMER_PREFIX: str = "akosha-"

#: Deletions are pipelined in batches of this size, so a group with
#: hundreds of thousands of leaked consumers does not issue one command
#: per consumer in a single unbounded transaction.
DELETE_BATCH_SIZE: int = 5000

#: Environment variable holding the Redis DSN.
REDIS_URL_ENV: str = "REDIS_URL"

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

_DEFAULT_REDIS_URL = "redis://localhost:6379/0"


def _consumer_name(entry: dict[str, Any]) -> str:
    """The consumer's name from one ``XINFO CONSUMERS`` row.

    Redis hands the name back as BYTES. ``str(b"akosha-x")`` is
    ``"b'akosha-x'"``, which does not start with ``akosha-`` -- so a
    naive ``str()`` here makes the prefix test below match NOTHING and
    the tool reports a clean group while the group is full of leaked
    consumers. Against the live group that reported
    ``leaked_consumers: 0`` for 14,950 leaked consumers.
    """
    raw = entry.get("name")
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw or "")


def _pending(entry: dict[str, Any]) -> int | None:
    """The consumer's pending-message count from one ``XINFO CONSUMERS`` row.

    Returns:
        The count, or ``None`` when it cannot be read.

    ``None`` is NOT zero. ``XGROUP DELCONSUMER`` destroys every message
    still pending for the consumer, so a count the tool could not read
    must PRESERVE the consumer, not delete it. The first version
    mapped an unreadable count to 0, which turned a read failure into
    data loss.
    """
    raw = entry.get("pending")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except TypeError, ValueError:
        return None


def _batched(items: Iterable[Any], size: int) -> Iterator[list[Any]]:
    """Yield consecutive chunks of at most ``size`` items, lazily.

    A generator, not a list of lists. ``XINFO CONSUMERS`` answers with
    every consumer on the group in one reply -- 446,049 of them on the
    live group -- so a tool that batched by building the whole list of
    chunks first held the names twice over, on top of the reply itself.

    The point of batching is to bound what is in flight; a chunk list
    built up front bounds nothing.
    """
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


class ConsumerListUnavailable(RuntimeError):
    """The consumer list could not be read, so nothing can be claimed about it.

    The tool must not report an all-clear it did not earn. A Redis
    timeout used to be swallowed into an empty list, which printed
    ``leaked_consumers: 0`` and exited 0 -- while the group held 14,950
    leaked consumers. The failure mode is silence in the face of a
    question, and the report looked certain while being blind.
    """


def _is_missing_group(exc: BaseException) -> bool:
    """True when the error says the consumer group does not exist.

    A group that was never created is a legitimate "nothing to clean"
    answer, and it is different from "Redis is unreachable". Redis
    reports it as ``NOGROUP No such consumer group ...``.
    """
    text = str(exc).upper()
    return "NOGROUP" in text or "NO SUCH CONSUMER GROUP" in text


def _list_consumers(redis_client: Any) -> tuple[list[dict[str, Any]], bool]:
    """Read the group's consumer list.

    Returns:
        The consumer rows, and whether the group was missing.

    Raises:
        ConsumerListUnavailable: the list could not be read for any
            reason other than the group not existing. The caller must
            not turn that into an empty group.
    """
    try:
        rows = redis_client.xinfo_consumers(STREAM_NAME, CONSUMER_GROUP)
    except Exception as exc:
        if _is_missing_group(exc):
            return [], True
        raise ConsumerListUnavailable(
            f"cannot read the consumer list for {STREAM_NAME}/{CONSUMER_GROUP}: {exc}"
        ) from exc
    # ``redis-py`` already hands back a list of dicts. Copying each one
    # would double the memory the largest structure in this tool needs,
    # so the copy is made only for the shapes that are not dicts.
    return [row if isinstance(row, dict) else dict(row) for row in (rows or [])], False


def _delete_batch(redis_client: Any, names: list[str]) -> None:
    """Delete one batch of consumers through a pipeline.

    The pipeline keeps the command count per round trip bounded, which
    is the whole point when there are hundreds of thousands of leaked
    consumers.

    The number of consumers actually removed is NOT read from
    ``XGROUP DELCONSUMER``'s return value: that value is the number of
    PENDING ENTRIES destroyed, which is 0 for the empty consumers this
    tool targets -- the very consumers it deleted most successfully.
    The count is taken afterwards by re-reading the group.
    """
    if not names:
        return
    pipeline = redis_client.pipeline()
    for name in names:
        pipeline.xgroup_delconsumer(STREAM_NAME, CONSUMER_GROUP, name)
    pipeline.execute()


def _classify_consumers(consumers: list[dict[str, Any]]) -> dict[str, Any]:
    """Sort the group's consumers into the ones we may delete and the rest.

    Returns:
        A dict with ``leaked_count`` (every ``akosha-`` consumer),
        ``preserved_pending`` (leaked ones that still own messages),
        ``preserved_unknown`` (leaked ones whose pending count could
        not be read), ``pending_before`` (the group's total pending
        count) and ``deletable`` (the names that may be deleted, in the
        group's own order, as a dict so membership is O(1)).

    This runs in its own frame on purpose: when it returns, the loop
    variable that held the last row goes with it. Leaving it in the
    caller would keep one row alive across the delete phase, which is
    the kind of almost-nothing that makes a memory bound a lie.
    """
    leaked_count = 0
    preserved_pending = 0
    preserved_unknown = 0
    pending_before = 0
    deletable: dict[str, None] = {}
    for row in consumers:
        pending = _pending(row)
        if pending is not None:
            pending_before += pending
        name = _consumer_name(row)
        if not name.startswith(CONSUMER_PREFIX):
            continue
        leaked_count += 1
        if pending is None:
            preserved_unknown += 1
        elif pending == 0:
            deletable[name] = None
        else:
            preserved_pending += 1
    return {
        "leaked_count": leaked_count,
        "preserved_pending": preserved_pending,
        "preserved_unknown": preserved_unknown,
        "pending_before": pending_before,
        "deletable": deletable,
    }


def plan_recover_event_stream(redis_client: Any, *, dry_run: bool) -> dict[str, Any]:
    """Report on — and optionally perform — the consumer cleanup.

    Args:
        redis_client: A Redis client exposing ``xinfo_consumers``,
            ``xinfo_groups`` and ``xgroup_delconsumer``.
        dry_run: When True, nothing is deleted; the report says what
            WOULD be deleted.

    Returns:
        A dict with ``leaked_consumers`` (every ``akosha-`` consumer,
        pending or not), ``would_delete`` (the ones that are empty),
        ``deleted`` (how many were actually gone when the group was
        re-read), ``remaining`` (how many of the targeted consumers are
        still registered), ``preserved_pending`` (the ones kept because
        they still own messages), ``preserved_unknown`` (the ones kept
        because their pending count could not be read),
        ``pending_before`` / ``pending_after`` (the group's total
        pending count on each side), ``group_missing`` (whether the
        group exists at all), and ``xinfo_seconds_before`` /
        ``xinfo_seconds_after`` (how long ``XINFO CONSUMERS`` took on
        each side — the number that went from 158 seconds to 0.1).

    Raises:
        ConsumerListUnavailable: the consumer list could not be read.

    Memory
    ------
    ``XINFO CONSUMERS`` answers with every consumer on the group in ONE
    reply -- 446,049 of them on the live group -- and redis-py hands
    that back as a list of dicts. So the reply itself is the floor on
    memory here, and everything this function adds on top of it is
    worth avoiding:

    * the rows are classified in a single pass and the row list is
      released before the deletions start, so the "before" read and
      the "after" read are never both resident;
    * only the TARGETED names are kept, not every name on the group;
    * the delete batches are produced lazily.
    """
    started = time.monotonic()
    consumers, group_missing = _list_consumers(redis_client)
    xinfo_seconds_before = time.monotonic() - started

    leaked_count = 0
    preserved_pending = 0
    preserved_unknown = 0
    pending_before = 0
    # A dict, not a list: it keeps the group's order (so the deletion
    # order is reproducible) AND gives O(1) membership for the
    # post-condition check, without a second container holding the
    # same names.
    classification = _classify_consumers(consumers)
    leaked_count = classification["leaked_count"]
    preserved_pending = classification["preserved_pending"]
    preserved_unknown = classification["preserved_unknown"]
    pending_before = classification["pending_before"]
    deletable = classification["deletable"]
    # Release the rows now: the names we need are kept, the dicts are
    # not. Holding them across the delete phase would double the peak.
    del consumers

    if not dry_run and deletable:
        for batch in _batched(deletable, DELETE_BATCH_SIZE):
            _delete_batch(redis_client, batch)

    started_after = time.monotonic()
    consumers_after, _ = _list_consumers(redis_client)
    xinfo_seconds_after = time.monotonic() - started_after
    pending_after = sum(p for p in (_pending(row) for row in consumers_after) if p is not None)

    # Only the targeted names are looked up in the re-read group, so no
    # second full set of the group's names is built.
    remaining = sum(1 for row in consumers_after if _consumer_name(row) in deletable)
    deleted = 0 if dry_run else len(deletable) - remaining

    return {
        "leaked_consumers": leaked_count,
        "would_delete": len(deletable),
        "deleted": deleted,
        "remaining": remaining,
        "preserved_pending": preserved_pending,
        "preserved_unknown": preserved_unknown,
        "group_missing": group_missing,
        "pending_before": pending_before,
        "pending_after": pending_after,
        "xinfo_seconds_before": xinfo_seconds_before,
        "xinfo_seconds_after": xinfo_seconds_after,
    }


def _report(result: dict[str, Any], *, dry_run: bool) -> str:
    """Render the report as the lines the CLI prints."""
    mode = "DRY RUN" if dry_run else "APPLIED"
    lines = [
        f"akosha.recover_event_stream [{mode}] {STREAM_NAME}/{CONSUMER_GROUP}",
        f"  leaked_consumers   : {result['leaked_consumers']}",
        f"  would_delete       : {result['would_delete']}",
        f"  deleted            : {result['deleted']}",
        f"  remaining          : {result['remaining']}",
        f"  preserved_pending  : {result['preserved_pending']}",
        f"  preserved_unknown  : {result['preserved_unknown']}",
        f"  pending_before     : {result['pending_before']}",
        f"  pending_after      : {result['pending_after']}",
        f"  XINFO seconds      : {result['xinfo_seconds_before']:.3f} -> "
        f"{result['xinfo_seconds_after']:.3f}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, redis_client: Any = None) -> int:
    """Run the recovery tool.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``.
        redis_client: An injected Redis client. ``None`` builds one from
            ``REDIS_URL``. The seam exists so the exit codes can be
            tested without a live Redis.

    Returns:
        0 on success, 1 when the consumer list could not be read or
        when the post-condition does not hold (a consumer that should
        have been deleted is still registered).
    """
    parser = argparse.ArgumentParser(
        description=(
            "Remove the consumers Akosha's event subscriber leaked on the "
            f"{STREAM_NAME}/{CONSUMER_GROUP} group. Consumers that still own "
            "pending messages are preserved: XGROUP DELCONSUMER would destroy "
            "them."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be deleted and change nothing (the default)",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="delete the empty leaked consumers",
    )
    args = parser.parse_args(argv)
    dry_run = not args.apply

    owned_client = None
    if redis_client is None:
        url = os.getenv(REDIS_URL_ENV, "") or _DEFAULT_REDIS_URL
        try:
            import redis
        except ImportError:  # pragma: no cover - redis is a hard dependency
            print(
                "akosha.recover_event_stream: the redis package is not installed",
                file=sys.stderr,
            )
            return 1
        owned_client = redis.Redis.from_url(url)
        redis_client = owned_client

    try:
        result = plan_recover_event_stream(redis_client, dry_run=dry_run)
    except ConsumerListUnavailable as exc:
        print(f"akosha.recover_event_stream: {exc}", file=sys.stderr)
        print(
            "akosha.recover_event_stream: nothing was read, so nothing can be "
            "claimed about the group",
            file=sys.stderr,
        )
        return 1
    finally:
        if owned_client is not None:
            owned_client.close()

    print(_report(result, dry_run=dry_run))

    if not dry_run and result["remaining"]:
        print(
            "akosha.recover_event_stream: the post-condition does not hold — "
            f"{result['remaining']} of {result['would_delete']} targeted "
            "consumers are still registered",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
