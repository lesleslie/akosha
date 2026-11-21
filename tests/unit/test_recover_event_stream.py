"""Unit tests for the ``bodai:events`` consumer recovery tool.

The Akosha event subscriber leaked one Redis consumer per
``XREADGROUP`` call for six weeks — 446,049 of them on the
``akosha-tool-invocation-indexers`` group. ``XINFO CONSUMERS`` took 158
seconds to answer, and every one of those consumers held a slot in the
group's PEL bookkeeping.

``scripts/recover_event_stream.py`` is the repeatable cure: it removes
the consumers this tool leaked and nothing else.

Why these tests exist
---------------------
The script deletes things in a live Redis stream, so its SELECTION
rules matter more than its mechanics:

* Only consumers whose name starts with ``akosha-`` are candidates.
  Another component's consumer on the same group is not ours to remove.
* Only consumers with ``pending == 0`` are candidates.
  ``XGROUP DELCONSUMER`` destroys every message still pending for the
  consumer — neither acked nor redelivered, and unrecoverable by
  ``XAUTOCLAIM``. A message that failed to index must not die on a
  cleanup run.
* The group itself is never deleted.

The script is loaded by path (it lives under ``scripts/``, which is not
a package) using a path derived from THIS test file, so it always tests
the copy in the tree it ships with.
"""

from __future__ import annotations

import importlib.util
import sys
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from redis.exceptions import ResponseError

if TYPE_CHECKING:
    import types

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "recover_event_stream.py"


def _load_script() -> types.ModuleType:
    """Load ``scripts/recover_event_stream.py`` as a module."""
    spec = importlib.util.spec_from_file_location("recover_event_stream", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["recover_event_stream"] = module
    spec.loader.exec_module(module)
    return module


_mod = _load_script()


def _consumer(name: str, pending: int = 0) -> dict[str, Any]:
    """One row of ``XINFO CONSUMERS`` output."""
    return {"name": name, "pending": pending, "idle": 0, "inactive": 0}


def _make_redis(consumers: list[dict[str, Any]]) -> tuple[MagicMock, list[str]]:
    """A redis double that tracks ``XGROUP DELCONSUMER`` calls.

    ``xinfo_consumers`` reflects the deletions, so the script's
    "pending after" measurement is meaningful rather than a replay of
    the "before" one. ``pipeline()`` returns a real pipeline double
    that queues the commands and applies them on ``execute()`` — the
    way redis-py does — so the script's batching path is exercised.
    """
    deleted: list[str] = []

    def _xinfo_consumers(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [c for c in consumers if c["name"] not in deleted]

    def _xgroup_delconsumer(name: str, groupname: str, consumername: str) -> int:
        deleted.append(consumername)
        return 1

    client = MagicMock()
    client.xinfo_consumers.side_effect = _xinfo_consumers
    client.xinfo_groups.return_value = [
        {"name": "akosha-tool-invocation-indexers", "consumers": len(consumers)}
    ]
    client.xgroup_delconsumer.side_effect = _xgroup_delconsumer
    client.pipeline.side_effect = lambda *_a, **_k: _FakePipeline(client, deleted)
    return client, deleted


class _FakePipeline:
    """A redis-py-shaped pipeline double that applies on ``execute()``."""

    def __init__(self, client: MagicMock, deleted: list[str]) -> None:
        self._client = client
        self._deleted = deleted
        self._queued: list[tuple[str, str, str]] = []

    def xgroup_delconsumer(self, name: str, groupname: str, consumername: str) -> _FakePipeline:
        self._queued.append((name, groupname, consumername))
        return self

    def execute(self) -> list[int]:
        for name, groupname, consumername in self._queued:
            self._client.xgroup_delconsumer(name, groupname, consumername)
        self._queued.clear()
        return [1] * len(self._queued)


class TestPlanRecoverEventStream:
    """The selection rules and the report the tool produces."""

    def test_dry_run_reports_without_deleting(self) -> None:
        """A dry run counts what it WOULD delete and deletes nothing."""
        client, deleted = _make_redis(
            [
                _consumer("akosha-aaa"),
                _consumer("akosha-bbb"),
                _consumer("akosha-ccc", pending=4),
            ]
        )

        result = _mod.plan_recover_event_stream(client, dry_run=True)

        assert result["leaked_consumers"] == 3
        assert result["deleted"] == 0
        assert deleted == []
        assert result["would_delete"] == 2

    def test_apply_deletes_only_pending_zero_leaked_consumers(self) -> None:
        """The two empty ``akosha-`` consumers go; the one with pending stays."""
        client, deleted = _make_redis(
            [
                _consumer("akosha-aaa"),
                _consumer("akosha-bbb"),
                _consumer("akosha-ccc", pending=4),
            ]
        )

        result = _mod.plan_recover_event_stream(client, dry_run=False)

        assert result["deleted"] == 2
        assert sorted(deleted) == ["akosha-aaa", "akosha-bbb"]
        assert result["preserved_pending"] == 1

    def test_apply_preserves_foreign_consumers(self) -> None:
        """A consumer this tool did not create is not ours to delete."""
        client, deleted = _make_redis(
            [
                _consumer("test-consumer"),
                _consumer("mahavishnu-indexer"),
                _consumer("akosha-aaa"),
            ]
        )

        _mod.plan_recover_event_stream(client, dry_run=False)

        assert "test-consumer" not in deleted
        assert "mahavishnu-indexer" not in deleted
        assert deleted == ["akosha-aaa"]

    def test_apply_preserves_a_consumer_with_pending_messages(self) -> None:
        """``XGROUP DELCONSUMER`` would destroy the pending messages."""
        client, deleted = _make_redis([_consumer("akosha-busy", pending=17)])

        result = _mod.plan_recover_event_stream(client, dry_run=False)

        assert deleted == []
        assert result["preserved_pending"] == 1
        assert result["pending_before"] == 17

    def test_never_deletes_the_group(self) -> None:
        """The group is the thing that holds the backlog; it stays."""
        client, _ = _make_redis([_consumer("akosha-aaa")])

        _mod.plan_recover_event_stream(client, dry_run=False)

        client.xgroup_destroy.assert_not_called()

    def test_reports_pending_before_and_after(self) -> None:
        """The pending count is measured on both sides of the cleanup."""
        client, _ = _make_redis(
            [
                _consumer("akosha-aaa"),
                _consumer("kosha-keep", pending=3),
            ]
        )

        result = _mod.plan_recover_event_stream(client, dry_run=False)

        assert result["pending_before"] == 3
        assert result["pending_after"] == 3

    def test_reports_the_xinfo_wall_clock(self) -> None:
        """``XINFO CONSUMERS`` took 158s before the cleanup; the report says so."""
        client, _ = _make_redis([_consumer("akosha-aaa")])

        result = _mod.plan_recover_event_stream(client, dry_run=False)

        assert result["xinfo_seconds_before"] >= 0.0
        assert result["xinfo_seconds_after"] >= 0.0

    def test_deletes_in_batches(self) -> None:
        """The delete is pipelined, so a huge group does not issue 446k calls."""
        client, deleted = _make_redis(
            [_consumer(f"akosha-{i:06d}") for i in range(_mod.DELETE_BATCH_SIZE + 7)]
        )

        result = _mod.plan_recover_event_stream(client, dry_run=False)

        assert result["deleted"] == _mod.DELETE_BATCH_SIZE + 7
        assert len(deleted) == _mod.DELETE_BATCH_SIZE + 7
        # Two batches, not one unbounded transaction.
        assert client.pipeline.call_count == 2

    def test_returns_the_leaked_consumer_count(self) -> None:
        """``leaked_consumers`` counts every ``akosha-`` consumer, pending or not."""
        client, _ = _make_redis(
            [_consumer("akosha-a"), _consumer("akosha-b", pending=2), _consumer("other")]
        )

        result = _mod.plan_recover_event_stream(client, dry_run=False)

        assert result["leaked_consumers"] == 2


class TestScriptIsImportableAndHasACli:
    """The script is a tool, not just a function."""

    def test_exposes_main(self) -> None:
        """``main()`` is the entry point the CLI calls."""
        assert callable(getattr(_mod, "main", None))

    def test_exposes_the_stream_and_group_defaults(self) -> None:
        """The stream and group are the ones the subscriber actually uses."""
        assert _mod.STREAM_NAME == "bodai:events"
        assert _mod.CONSUMER_GROUP == "akosha-tool-invocation-indexers"

    def test_exposes_the_consumer_prefix(self) -> None:
        """Only ``akosha-`` consumers are ours to delete."""
        assert _mod.CONSUMER_PREFIX == "akosha-"


class TestScriptPathIsDerivedFromTheTestFile:
    """The script under test is the one in this repository."""

    def test_the_script_lives_inside_this_repository(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        assert repo_root in _SCRIPT_PATH.resolve().parents
        assert _SCRIPT_PATH.exists()


@pytest.mark.parametrize("dry_run", [True, False])
def test_plan_accepts_the_documented_signature(dry_run: bool) -> None:
    """``plan_recover_event_stream(client, *, dry_run=...)`` is the contract."""
    client, _ = _make_redis([])
    result = _mod.plan_recover_event_stream(client, dry_run=dry_run)
    assert isinstance(result, dict)
    assert result["deleted"] == 0


class TestConsumerNamesMayBeBytes:
    """Redis returns the consumer name as BYTES, not str.

    ``XINFO CONSUMERS`` hands back ``name`` as ``b'akosha-...'``. A
    naive ``str(entry["name"]).startswith("akosha-")`` is False for
    that -- ``str(b"akosha-x")`` is ``"b'akosha-x'"``, which starts
    with ``b'`` and not with ``akosha-``. The script would then select
    zero consumers and report a clean group while the group was full of
    them.

    This is not hypothetical: against the live group the selection
    matched nothing and reported ``leaked_consumers: 0`` for a group
    holding 14,950 leaked consumers.
    """

    @staticmethod
    def _bytes_redis(count: int) -> tuple[MagicMock, list[str]]:
        rows = [{"name": f"akosha-{i:012x}".encode(), "pending": 0} for i in range(count)]
        deleted: list[str] = []

        def _xinfo_consumers(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return [r for r in rows if r["name"].decode() not in deleted]

        def _xgroup_delconsumer(name: str, groupname: str, consumername: Any) -> int:
            deleted.append(
                consumername.decode() if isinstance(consumername, bytes) else consumername
            )
            return 1

        client = MagicMock()
        client.xinfo_consumers.side_effect = _xinfo_consumers
        client.xinfo_groups.return_value = [{"name": "akosha-tool-invocation-indexers"}]
        client.xgroup_delconsumer.side_effect = _xgroup_delconsumer
        client.pipeline.side_effect = lambda *_a, **_k: _FakePipeline(client, deleted)
        return client, deleted

    def test_bytes_names_are_recognised_as_leaked(self) -> None:
        client, _ = self._bytes_redis(3)

        result = _mod.plan_recover_event_stream(client, dry_run=True)

        assert result["leaked_consumers"] == 3
        assert result["would_delete"] == 3

    def test_bytes_names_are_deleted_by_name(self) -> None:
        client, deleted = self._bytes_redis(2)

        result = _mod.plan_recover_event_stream(client, dry_run=False)

        assert result["deleted"] == 2
        assert deleted == ["akosha-000000000000", "akosha-000000000001"]

    def test_a_bytes_foreign_consumer_is_still_preserved(self) -> None:
        rows = [
            {"name": b"other-component", "pending": 0},
            {"name": b"akosha-000000000001", "pending": 0},
        ]
        deleted: list[str] = []

        def _xinfo_consumers(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return [r for r in rows if r["name"].decode() not in deleted]

        def _xgroup_delconsumer(name: str, groupname: str, consumername: Any) -> int:
            deleted.append(
                consumername.decode() if isinstance(consumername, bytes) else consumername
            )
            return 1

        client = MagicMock()
        client.xinfo_consumers.side_effect = _xinfo_consumers
        client.xinfo_groups.return_value = [{"name": "akosha-tool-invocation-indexers"}]
        client.xgroup_delconsumer.side_effect = _xgroup_delconsumer
        client.pipeline.side_effect = lambda *_a, **_k: _FakePipeline(client, deleted)

        _mod.plan_recover_event_stream(client, dry_run=False)

        assert deleted == ["akosha-000000000001"]


class TestAnUnreadableGroupIsNotAnAllClear:
    """A tool that cannot see the group must not report that it is clean.

    The first version swallowed every read failure and returned an
    empty list, so a Redis timeout produced ``leaked_consumers: 0``,
    ``deleted: 0`` and exit code 0 -- a confident all-clear from a tool
    that had seen nothing. It is not hypothetical: the live dry-run
    printed "cannot read the consumer list ... treating it as empty"
    while the group held 14,950 leaked consumers.
    """

    def test_a_read_failure_raises_rather_than_reporting_zero(self) -> None:
        client = MagicMock()
        client.xinfo_consumers.side_effect = TimeoutError("Timeout reading from socket")

        with pytest.raises(_mod.ConsumerListUnavailable):
            _mod.plan_recover_event_stream(client, dry_run=True)

    def test_a_missing_group_is_nothing_to_clean(self) -> None:
        """A group that does not exist is a legitimate empty answer."""
        client = MagicMock()
        client.xinfo_consumers.side_effect = ResponseError(
            "NOGROUP No such consumer group 'x' for key name 'bodai:events'"
        )

        result = _mod.plan_recover_event_stream(client, dry_run=True)

        assert result["leaked_consumers"] == 0
        assert result["group_missing"] is True

    def test_main_exits_non_zero_when_the_group_cannot_be_read(self) -> None:
        """The CLI must not exit 0 when it learned nothing."""
        client = MagicMock()
        client.xinfo_consumers.side_effect = TimeoutError("Timeout reading from socket")

        exit_code = _mod.main(["--dry-run"], redis_client=client)

        assert exit_code == 1

    def test_main_exits_zero_when_the_group_is_missing(self) -> None:
        client = MagicMock()
        client.xinfo_consumers.side_effect = ResponseError("NOGROUP No such consumer group")

        exit_code = _mod.main(["--dry-run"], redis_client=client)

        assert exit_code == 0


class TestUnknownPendingIsNeverDeleted:
    """An unreadable pending count must PRESERVE the consumer.

    ``XGROUP DELCONSUMER`` destroys every message still pending for the
    consumer. If the pending count cannot be read, the tool does not
    know whether there are messages to destroy -- so it must leave the
    consumer alone. Treating "unknown" as "zero" turns a read failure
    into data loss.
    """

    @staticmethod
    def _redis_with(unparseable_pending: Any) -> tuple[MagicMock, list[str]]:
        rows: list[dict[str, Any]] = [
            {"name": b"akosha-unknown", "pending": unparseable_pending},
            {"name": b"akosha-empty", "pending": 0},
        ]
        deleted: list[str] = []

        def _xinfo_consumers(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return [r for r in rows if r["name"].decode() not in deleted]

        def _xgroup_delconsumer(name: str, groupname: str, consumername: Any) -> int:
            deleted.append(
                consumername.decode() if isinstance(consumername, bytes) else consumername
            )
            return 1

        client = MagicMock()
        client.xinfo_consumers.side_effect = _xinfo_consumers
        client.xinfo_groups.return_value = [{"name": "akosha-tool-invocation-indexers"}]
        client.xgroup_delconsumer.side_effect = _xgroup_delconsumer
        client.pipeline.side_effect = lambda *_a, **_k: _FakePipeline(client, deleted)
        return client, deleted

    def test_an_unparseable_pending_count_preserves_the_consumer(self) -> None:
        client, deleted = self._redis_with("not-a-number")

        result = _mod.plan_recover_event_stream(client, dry_run=False)

        assert "akosha-unknown" not in deleted
        assert result["preserved_unknown"] == 1

    def test_a_missing_pending_count_preserves_the_consumer(self) -> None:
        client, deleted = self._redis_with(None)

        result = _mod.plan_recover_event_stream(client, dry_run=False)

        assert "akosha-unknown" not in deleted
        assert result["preserved_unknown"] == 1

    def test_the_empty_consumer_is_still_deleted(self) -> None:
        client, deleted = self._redis_with("not-a-number")

        _mod.plan_recover_event_stream(client, dry_run=False)

        assert deleted == ["akosha-empty"]


class TestDeletedCountsWhatIsActuallyGone:
    """``deleted`` must count deletions, not attempts.

    The first version returned the size of the batch it queued, so the
    post-condition in ``main()`` compared a number against itself and
    could never fire. The count is now taken by re-reading the consumer
    list and seeing which of the targeted names are gone.
    """

    def test_deleted_counts_the_consumers_that_are_gone(self) -> None:
        client, _ = _make_redis([_consumer("akosha-aaa"), _consumer("akosha-bbb")])

        result = _mod.plan_recover_event_stream(client, dry_run=False)

        assert result["deleted"] == 2
        assert result["remaining"] == 0

    def test_a_deletion_that_did_not_happen_is_reported(self) -> None:
        """If the consumer is still there afterwards, it was not deleted."""
        client, _ = _make_redis([_consumer("akosha-aaa")])
        # The delete silently does nothing.
        client.xgroup_delconsumer.side_effect = None

        result = _mod.plan_recover_event_stream(client, dry_run=False)

        assert result["remaining"] == 1
        assert result["deleted"] == 0

    def test_main_exits_non_zero_when_the_post_condition_fails(self) -> None:
        client, _ = _make_redis([_consumer("akosha-aaa")])
        client.xgroup_delconsumer.side_effect = None

        exit_code = _mod.main(["--apply"], redis_client=client)

        assert exit_code == 1


class TestTheToolDoesNotHoldTheWholeConsumerList:
    """The cleanup must not need the whole group resident in memory.

    ``XINFO CONSUMERS`` answers with every consumer on the group in one
    reply. The live group held 446,049 of them, and a plan that kept
    that list alive while it worked needed hundreds of megabytes --
    so a tool whose job is to rescue an overloaded group would itself
    be the thing that pushed the machine over.

    Two things follow:

    * the rows read BEFORE the deletions must be released before the
      group is read again, and
    * the delete batches must be produced lazily, not all at once.

    The rows are observed with weak references, so the assertions are
    about what is actually alive -- not about how the code reads.
    """

    def test_the_rows_read_before_the_deletions_are_released_first(self) -> None:
        observed: list[Any] = []
        calls = {"n": 0}

        def _xinfo_consumers(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            calls["n"] += 1
            rows = [
                _ObservedRow(name=f"akosha-{i:08d}", pending=0, idle=0, inactive=0)
                for i in range(50)
            ]
            if calls["n"] == 1:
                # Keep only weak references: the lifetime of these rows
                # is then decided entirely by whoever holds them.
                observed.extend(weakref.ref(row) for row in rows)
            return rows

        client = MagicMock()
        client.xinfo_consumers.side_effect = _xinfo_consumers
        client.xinfo_groups.return_value = [{"name": "akosha-tool-invocation-indexers"}]
        client.pipeline.side_effect = lambda *_a, **_k: _FakePipeline(client, [])

        alive_at_second_read: list[int] = []
        original = client.xinfo_consumers.side_effect

        def _counting(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            if calls["n"] >= 1:
                alive_at_second_read.append(sum(1 for ref in observed if ref() is not None))
            return original(*args, **kwargs)

        client.xinfo_consumers.side_effect = _counting

        _mod.plan_recover_event_stream(client, dry_run=False)

        assert alive_at_second_read, "the group must be read a second time"
        assert alive_at_second_read[0] == 0, (
            "the consumer rows read before the deletions must be released "
            "before the group is read again; "
            f"{alive_at_second_read[0]} of {len(observed)} were still alive"
        )

    def test_the_batches_are_produced_lazily(self) -> None:
        """A list of chunks is the same peak as no chunks at all."""
        batches = _mod._batched(list(range(10)), 3)

        assert not isinstance(batches, list), (
            "the delete batches must be produced lazily, not materialised as a list of lists"
        )

    def test_only_the_first_batch_is_drawn_from_the_source(self) -> None:
        produced: list[int] = []

        def _source() -> Any:
            for i in range(10):
                produced.append(i)
                yield i

        batches = _mod._batched(_source(), 3)
        first = next(iter(batches))

        assert first == [0, 1, 2]
        assert produced == [0, 1, 2], "drawing the first batch must not drain the whole source"


class _ObservedRow(dict):
    """An ``XINFO CONSUMERS`` row that a weak reference can watch."""
