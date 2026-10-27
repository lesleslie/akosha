"""The operator's "sweep now" request, and what it is NOT allowed to do.

§11.4a's operator-interface table had one row marked "Not available": force a
retention sweep now. The sweep runs on a 6-hour interval, so an operator
under substrate pressure had up to six hours to wait.

Three properties are pinned here, and the second is the one that matters:

1. **A request wakes the loop.** The sweep runs within a moment, not at the
   next interval.

2. **A request SIGNALS; it never deletes.** The sweep loop owns the single
   DuckDB connection to the substrate. A caller that ran the sweep itself
   would put a second ``DELETE`` against that connection, and two coroutines
   deleting on one DuckDB handle is a race, not a feature. So the request
   sets an event, and the LOOP -- the sole writer -- performs the sweep.

3. **One pending request is one sweep.** Asking twice does not queue two
   sweeps; the second call says so.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from akosha.observability.otlp_receiver import OTLPReceiver
from akosha.storage.hot_store import HotStore


@pytest.fixture
async def store() -> HotStore:
    hs = HotStore(database_path=":memory:", embedding_dim=384)
    await hs.initialize()
    return hs


@pytest.fixture
def receiver(store: HotStore) -> OTLPReceiver:
    return OTLPReceiver(store, grpc_port=0, http_port=0)


def _fake_config(interval_hours: int = 24 * 365, retention_days: int = 30) -> SimpleNamespace:
    """A config whose sweep interval is long enough that the loop never self-fires."""
    return SimpleNamespace(
        spans_substrate=SimpleNamespace(
            retention_sweep_interval_hours=interval_hours,
            retention_days=retention_days,
        )
    )


def _patch_config(monkeypatch: pytest.MonkeyPatch, **kwargs) -> None:
    """Patch the config MODULE's ``get_config`` for the duration of a test.

    ``akosha/__init__.py`` resolves the ``akosha.config`` ATTRIBUTE to the
    config INSTANCE through a lazy ``__getattr__``. So ``akosha.config`` is
    not a reliable way to reach the module, and
    ``monkeypatch.setattr("akosha.config.get_config", ...)`` resolves that
    attribute -- the instance -- and raises ``AttributeError``. Reach the
    module explicitly.
    """
    import importlib

    module = importlib.import_module("akosha.config")

    def _fake_get_config(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return _fake_config(**kwargs)

    monkeypatch.setattr(module, "get_config", _fake_get_config)


def _insert_old_row(receiver: OTLPReceiver, age_hours: int = 24 * 60) -> None:
    """Insert one row older than the retention horizon."""
    old_ns = time.time_ns() - age_hours * 3600 * 1_000_000_000
    receiver._hot_store.conn.execute(  # type: ignore[union-attr]
        "INSERT INTO spans_substrate "
        "(span_id, trace_id, service_name, service_namespace, span_name, "
        " start_time_unix_nano, received_at_unix_nano, attributes, resource) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, '{}', '{}')",
        ["e" * 16, "f" * 32, "svc", "bodai", "old_span", old_ns, old_ns],
    )


def _row_count(receiver: OTLPReceiver) -> int:
    row = receiver._hot_store.conn.execute(  # type: ignore[union-attr]
        "SELECT COUNT(*) FROM spans_substrate"
    ).fetchone()
    return int(row[0])


async def _wait_until(predicate, timeout: float = 10.0) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` elapses.

    Condition-based waiting, not a fixed sleep: the sweep runs on the loop's
    schedule, and a fixed sleep is either flaky or slow.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


class TestRequestSemantics:
    """``request_retention_sweep`` is a signal, and it is idempotent."""

    def test_the_first_request_is_queued(self, receiver: OTLPReceiver) -> None:
        assert receiver.request_retention_sweep() is True

    def test_a_second_request_while_one_is_pending_is_not_a_second_sweep(
        self, receiver: OTLPReceiver
    ) -> None:
        """One pending request is one sweep.

        Queueing a second would make the loop sweep twice for one ask, and
        a second DELETE over the same rows is pure cost.
        """
        assert receiver.request_retention_sweep() is True
        assert receiver.request_retention_sweep() is False

    def test_requesting_does_not_delete_anything(self, receiver: OTLPReceiver) -> None:
        """The request itself must not touch the substrate.

        This is the property that keeps the tool safe to expose: the tool
        signals, and the loop -- the sole writer -- does the deleting.
        """
        _insert_old_row(receiver)
        assert _row_count(receiver) == 1

        receiver.request_retention_sweep()

        assert _row_count(receiver) == 1, "the request must not delete; the loop does"


class TestTheLoopWakesOnRequest:
    """The requested sweep actually runs, and runs soon."""

    async def test_a_requested_sweep_runs_without_waiting_for_the_interval(
        self, receiver: OTLPReceiver, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The row goes away in moments, not at the next 6-hour interval."""
        _patch_config(monkeypatch, interval_hours=24 * 365)
        _insert_old_row(receiver)

        stop_event = asyncio.Event()
        task = asyncio.create_task(receiver.run_retention_sweep(stop_event))
        try:
            # The interval is a year, so the loop must NOT sweep on its own.
            await asyncio.sleep(0.2)
            assert _row_count(receiver) == 1, "the loop must not self-fire"

            assert receiver.request_retention_sweep() is True

            assert await _wait_until(lambda: _row_count(receiver) == 0), (
                "the requested sweep must delete the expired row"
            )
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=10)

    async def test_the_request_is_cleared_so_the_next_one_queues(
        self, receiver: OTLPReceiver, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After a sweep, a fresh request queues again."""
        _patch_config(monkeypatch, interval_hours=24 * 365)
        _insert_old_row(receiver)

        stop_event = asyncio.Event()
        task = asyncio.create_task(receiver.run_retention_sweep(stop_event))
        try:
            receiver.request_retention_sweep()
            assert await _wait_until(lambda: _row_count(receiver) == 0)

            # The pending request was consumed by the sweep that just ran.
            assert receiver.request_retention_sweep() is True
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=10)

    async def test_the_loop_publishes_the_interval_it_is_actually_running_on(
        self, receiver: OTLPReceiver, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``sweep_interval_s`` is the RUNNING interval, not a re-read of the settings.

        The operator tool reports this so an operator knows how long they
        would have waited. If the settings changed since boot, the running
        receiver's interval is the one that matters.
        """
        _patch_config(monkeypatch, interval_hours=6)
        stop_event = asyncio.Event()
        task = asyncio.create_task(receiver.run_retention_sweep(stop_event))
        try:
            assert await _wait_until(lambda: receiver.sweep_interval_s is not None)
            assert receiver.sweep_interval_s == 6 * 3600
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=10)


class TestStopWins:
    """A receiver that is shutting down must not start a sweep."""

    async def test_a_stop_event_ends_the_loop(
        self, receiver: OTLPReceiver, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stop event still terminates the loop after the change."""
        _patch_config(monkeypatch, interval_hours=24 * 365)
        stop_event = asyncio.Event()
        task = asyncio.create_task(receiver.run_retention_sweep(stop_event))

        await asyncio.sleep(0.1)
        stop_event.set()
        await asyncio.wait_for(task, timeout=10)
        assert task.done(), "the loop must return when the stop event fires"

    async def test_a_request_and_a_stop_together_end_the_loop(
        self, receiver: OTLPReceiver, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stop wins over a pending request.

        A receiver on its way down should not begin a DELETE.
        """
        _patch_config(monkeypatch, interval_hours=24 * 365)
        _insert_old_row(receiver)

        stop_event = asyncio.Event()
        task = asyncio.create_task(receiver.run_retention_sweep(stop_event))
        await asyncio.sleep(0.1)

        receiver.request_retention_sweep()
        stop_event.set()

        await asyncio.wait_for(task, timeout=10)
        assert task.done()
        # Whether the row went depends on which the loop observed first;
        # what must hold is that the loop RETURNED rather than hanging.
