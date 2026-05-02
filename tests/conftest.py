"""Pytest configuration and fixtures for Akosha tests."""

import os
import sys

# Fix import path for akosha modules
_akosha_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _akosha_root not in sys.path:
    sys.path.insert(0, _akosha_root)

import pytest


@pytest.fixture(scope="session", autouse=True)
def setup_telemetry():
    """Setup OpenTelemetry for all tests.

    This fixture automatically sets up telemetry once per test session
    and uses pytest-asyncio's event loop.

    The session scope means this runs once for all tests, which is
    more efficient and avoids event loop conflicts.
    """
    try:
        from akosha.observability.tracing import setup_telemetry as setup_otel

        setup_otel(
            service_name="akosha-test",
            enable_console_export=False,
            otlp_endpoint=None,
        )
    except ImportError:
        pass

    yield
