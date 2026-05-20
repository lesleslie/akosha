from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import akosha.mcp as mcp_pkg


def test_getattr_http_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test package-level http_app lazy initialization."""
    mock_app = MagicMock()
    mock_app.http_app.return_value = "http-app"
    monkeypatch.setattr("akosha.mcp.server.create_app", lambda: mock_app)

    result = mcp_pkg.__getattr__("http_app")

    assert result == "http-app"
    mock_app.http_app.assert_called_once()


def test_getattr_unknown_attribute() -> None:
    """Test package-level unknown attribute handling."""
    with pytest.raises(AttributeError, match="unknown"):
        mcp_pkg.__getattr__("unknown")
