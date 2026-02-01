"""Pytest configuration and fixtures for Akosha tests."""

import pytest


@pytest.fixture(scope="session", autouse=True)
def setup_telemetry():
    """Setup OpenTelemetry for all tests.

    This fixture automatically sets up telemetry once per test session
    and uses pytest-asyncio's event loop.

    The session scope means this runs once for all tests, which is
    more efficient and avoids event loop conflicts.
    """
    from akosha.observability.tracing import setup_telemetry

    # Setup telemetry with test configuration
    setup_telemetry(
        service_name="akosha-test",
        enable_console_export=False,  # Disable console export for cleaner test output
        otlp_endpoint=None,  # No external OTLP collector for tests
    )

    yield

    # No explicit shutdown needed - process cleanup handles it
    # Calling shutdown_telemetry() here would conflict with pytest-asyncio's event loop
