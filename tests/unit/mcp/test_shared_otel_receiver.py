"""``set_shared_otel_receiver`` / ``get_shared_otel_receiver`` — PR-0b.

The spec's §6 PR-0b regression test, reduced to what is actually observable
without booting the full server: a stale receiver must not survive into the
next ``create_app()``.

A leaked receiver is worse than an absent one. Its ``stop()`` has already run,
so its listeners are closed, but the object still answers every counter query
with plausible numbers from its previous life — a health probe reading
``rows_inserted_total`` sees a healthy ingest that stopped happening an hour
ago.
"""

from __future__ import annotations

from typing import Any

import pytest

from akosha.mcp import server as server_mod


class _FakeReceiver:
    """Stands in for OTLPReceiver; the singleton holds it untyped."""

    def __init__(self) -> None:
        self.rows_inserted_total = 7
        self.stopped = False


@pytest.fixture(autouse=True)
def _clean_singletons() -> Any:
    server_mod.clear_shared_services()
    yield
    server_mod.clear_shared_services()


def test_the_receiver_getter_exists_and_starts_none() -> None:
    """A default of ``None`` is the whole point of the getter.

    Every caller has to be able to ask "is there a receiver?" without a
    try/except, which means the unset state has to be representable rather
    than an AttributeError.
    """
    assert server_mod.get_shared_otel_receiver() is None


def test_set_then_get_round_trips() -> None:
    receiver = _FakeReceiver()
    server_mod.set_shared_otel_receiver(receiver)
    assert server_mod.get_shared_otel_receiver() is receiver


def test_clear_shared_services_clears_the_receiver() -> None:
    """PR-0b's regression: create_app() twice must not leak the first receiver."""
    server_mod.set_shared_otel_receiver(_FakeReceiver())
    server_mod.clear_shared_services()
    assert server_mod.get_shared_otel_receiver() is None


def test_clear_shared_services_still_clears_its_two_originals() -> None:
    """Extending the function must not quietly narrow it.

    ``clear_shared_services`` grew from two globals to three in this change.
    A refactor that rebuilt its body would silently stop clearing the two it
    used to handle, and nothing else would notice until test cross-talk.
    """
    server_mod.set_shared_hot_store(object())
    server_mod.set_shared_kg_builder(object())
    server_mod.set_shared_otel_receiver(_FakeReceiver())
    server_mod.clear_shared_services()
    assert server_mod.get_shared_hot_store() is None
    assert server_mod.get_shared_kg_builder() is None
    assert server_mod.get_shared_otel_receiver() is None


def test_clearing_the_receiver_leaves_the_hot_store_alone() -> None:
    """They are cleared together, not coupled.

    The read path's ``SpanQuery`` holds the HotStore; tearing that down
    because the ingest side stopped would take out querying too, and the two
    have genuinely different lifetimes.
    """
    store = object()
    server_mod.set_shared_hot_store(store)
    server_mod.set_shared_otel_receiver(_FakeReceiver())
    server_mod.set_shared_otel_receiver(None)
    assert server_mod.get_shared_hot_store() is store


def test_the_lifespan_teardown_nulls_the_receiver_explicitly() -> None:
    """Teardown clears it at its own site, not only via ``clear_shared_services``.

    The lifespan calls ``set_shared_otel_receiver(None)`` inline so the
    receiver is dropped at the same moment its listeners close. Relying on
    ``clear_shared_services`` alone leaves a window where a shutdown server
    still reports a stopped receiver's counters.
    """
    import inspect

    source = inspect.getsource(server_mod.create_app)
    assert "set_shared_otel_receiver(None)" in source
    assert source.index("await receiver.stop()") < source.index(
        "set_shared_otel_receiver(None)"
    )


# ── port resolution ──────────────────────────────────────────────────────


def test_ports_default_to_the_canonical_otlp_ports() -> None:
    """:4317/:4318 belong to the substrate per the v7 port-ownership decision.

    Grafana Alloy is moved to :14317/:14318 at cutover. If these defaults
    ever drift, the two collide on bind and neither receives a span.
    """
    from akosha.observability.otlp_receiver import OTLPReceiver

    assert OTLPReceiver.GRPC_PORT == 4317
    assert OTLPReceiver.HTTP_PORT == 4318


def test_the_port_resolver_returns_an_int(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever the settings layer returns, the constructor wants an int.

    A YAML value that arrives as a string would otherwise reach
    ``add_insecure_port("127.0.0.1:4317")`` via f-string formatting and fail
    at bind with a message naming no setting.
    """
    assert isinstance(server_mod._receiver_port("grpc"), int)
    assert isinstance(server_mod._receiver_port("http"), int)


def test_the_resolver_reads_the_spans_substrate_config() -> None:
    """The wiring from config to bind is real, not decorative.

    ``get_config`` strips any key ``AkoshaConfig`` does not declare, and does
    it silently. A resolver pointed at an undeclared key therefore always
    returns the default, always binds the canonical ports, and never says so —
    which is why the override is asserted here rather than trusted.
    """
    from akosha.config import SpansSubstrateConfig

    original = server_mod._lifespan_spans_config
    try:
        server_mod._lifespan_spans_config = SpansSubstrateConfig(
            receiver_grpc_port=14317, receiver_http_port=14318
        )
        assert server_mod._receiver_port("grpc") == 14317
        assert server_mod._receiver_port("http") == 14318
    finally:
        server_mod._lifespan_spans_config = original


def test_the_resolver_falls_back_to_class_defaults_with_no_config() -> None:
    """``None`` is the test path, where no lifespan has run yet.

    The fallback has to be the class attribute rather than a literal, or the
    test default and the production default drift apart and nothing notices
    until two instances collide on bind.
    """
    original = server_mod._lifespan_spans_config
    try:
        server_mod._lifespan_spans_config = None
        from akosha.observability.otlp_receiver import OTLPReceiver

        assert server_mod._receiver_port("grpc") == OTLPReceiver.GRPC_PORT
        assert server_mod._receiver_port("http") == OTLPReceiver.HTTP_PORT
    finally:
        server_mod._lifespan_spans_config = original


def test_the_settings_keys_are_declared_on_the_config_model() -> None:
    """The failure this guards is invisible: an undeclared key is DROPPED.

    ``get_config`` filters merged settings down to ``AkoshaConfig`` fields.
    Add the YAML key without adding the field and the operator's value
    vanishes with no warning — the receiver binds the defaults while the
    config file claims otherwise.
    """
    from akosha.config import SpansSubstrateConfig

    fields = SpansSubstrateConfig.model_fields
    assert "receiver_grpc_port" in fields
    assert "receiver_http_port" in fields
    assert fields["receiver_grpc_port"].default == 4317
    assert fields["receiver_http_port"].default == 4318


def test_the_committed_settings_file_declares_both_ports() -> None:
    """``settings/akosha.yaml`` is what actually runs in production.

    Anchored at the config module's own location, not the CWD — a test run
    from anywhere else would otherwise read a different file (or none) and
    pass or fail on the runner's working directory.
    """
    from pathlib import Path

    import yaml

    import akosha.config as config_mod

    settings = Path(config_mod.__file__).parent.parent / "settings" / "akosha.yaml"
    data = yaml.safe_load(settings.read_text())
    assert data["spans_substrate"]["receiver_grpc_port"] == 4317
    assert data["spans_substrate"]["receiver_http_port"] == 4318