"""Tests for WebSocket TLS configuration module.

Tests TLS configuration loading and SSL context creation for secure WebSocket connections.
"""

import pytest
import os
from unittest.mock import patch, MagicMock, mock_open
from akosha.websocket.tls_config import get_websocket_tls_config, load_ssl_context


class TestWebsocketTlsConfig:
    """Test WebSocket TLS configuration functionality."""

    def test_get_websocket_tls_config_defaults(self):
        """Test TLS config with default environment values."""
        # Clear environment variables first
        env_vars = [
            'AKOSHA_WS_TLS_ENABLED',
            'AKOSHA_WS_CERT_FILE',
            'AKOSHA_WS_KEY_FILE',
            'AKOSHA_WS_CA_FILE',
            'AKOSHA_WS_VERIFY_CLIENT'
        ]

        with patch.dict(os.environ, {}, clear=True):
            config = get_websocket_tls_config()

            # Should return default values
            assert config["tls_enabled"] is False
            assert config["cert_file"] is None
            assert config["key_file"] is None
            assert config["ca_file"] is None
            assert config["verify_client"] is False

    def test_get_websocket_tls_config_with_env(self):
        """Test TLS config with environment variables set."""
        with patch.dict(os.environ, {
            'AKOSHA_WS_TLS_ENABLED': 'true',
            'AKOSHA_WS_CERT_FILE': '/path/to/cert.pem',
            'AKOSHA_WS_KEY_FILE': '/path/to/key.pem',
            'AKOSHA_WS_CA_FILE': '/path/to/ca.pem',
            'AKOSHA_WS_VERIFY_CLIENT': 'true'
        }):
            config = get_websocket_tls_config()

            assert config["tls_enabled"] is True
            assert config["cert_file"] == '/path/to/cert.pem'
            assert config["key_file"] == '/path/to/key.pem'
            assert config["ca_file"] == '/path/to/ca.pem'
            assert config["verify_client"] is True

    def test_get_websocket_tls_config_partial_env(self):
        """Test TLS config with partial environment variables."""
        with patch.dict(os.environ, {
            'AKOSHA_WS_TLS_ENABLED': 'false',
            'AKOSHA_WS_CERT_FILE': '/path/to/cert.pem'
            # Other variables not set
        }):
            config = get_websocket_tls_config()

            assert config["tls_enabled"] is False
            assert config["cert_file"] == '/path/to/cert.pem'
            assert config["key_file"] is None
            assert config["ca_file"] is None
            assert config["verify_client"] is False

    def test_get_websocket_tls_config_boolean_parsing(self):
        """Test proper parsing of boolean environment variables."""
        test_cases = [
            ('true', True),
            ('True', True),
            ('1', True),
            ('false', False),
            ('False', False),
            ('0', False),
            ('invalid', False)  # Should default to False
        ]

        for value, expected in test_cases:
            with patch.dict(os.environ, {'AKOSHA_WS_TLS_ENABLED': value}):
                config = get_websocket_tls_config()
                assert config["tls_enabled"] == expected

    @patch('akosha.websocket.tls_config.create_ssl_context')
    def test_load_ssl_context_with_files(self, mock_create_ssl):
        """Test SSL context loading with provided certificate files."""
        mock_context = MagicMock()
        mock_create_ssl.return_value = mock_context

        result = load_ssl_context(
            cert_file='/path/to/cert.pem',
            key_file='/path/to/key.pem',
            ca_file='/path/to/ca.pem',
            verify_client=True
        )

        # Should have created SSL context
        mock_create_ssl.assert_called_once_with(
            cert_file='/path/to/cert.pem',
            key_file='/path/to/key.pem',
            ca_file='/path/to/ca.pem',
            verify_client=True,
        )

        assert result["ssl_context"] == mock_context
        assert result["cert_file"] == '/path/to/cert.pem'
        assert result["key_file"] == '/path/to/key.pem'
        assert result["ca_file"] == '/path/to/ca.pem'
        assert result["verify_client"] is True

    @patch('akosha.websocket.tls_config.create_ssl_context')
    def test_load_ssl_context_minimal_files(self, mock_create_ssl):
        """Test SSL context loading with minimal certificate files."""
        mock_context = MagicMock()
        mock_create_ssl.return_value = mock_context

        result = load_ssl_context(
            cert_file='/path/to/cert.pem',
            key_file='/path/to/key.pem'
        )

        # Should have created SSL context
        mock_create_ssl.assert_called_once_with(
            cert_file='/path/to/cert.pem',
            key_file='/path/to/key.pem',
            ca_file=None,
            verify_client=False,
        )

        assert result["ssl_context"] == mock_context

    @patch('akosha.websocket.tls_config.get_websocket_tls_config')
    @patch('akosha.websocket.tls_config.create_ssl_context')
    def test_load_ssl_context_from_env(self, mock_create_ssl, mock_get_config):
        """Test SSL context loading from environment variables."""
        mock_context = MagicMock()
        mock_create_ssl.return_value = mock_context
        mock_get_config.return_value = {
            "tls_enabled": True,
            "cert_file": "/env/cert.pem",
            "key_file": "/env/key.pem",
            "ca_file": "/env/ca.pem",
            "verify_client": True,
        }

        # Call without parameters - should use env
        result = load_ssl_context()

        # Should have used environment config
        mock_create_ssl.assert_called_once_with(
            cert_file="/env/cert.pem",
            key_file="/env/key.pem",
            ca_file="/env/ca.pem",
            verify_client=True,
        )

        assert result["ssl_context"] == mock_context

    @patch('akosha.websocket.tls_config.get_websocket_tls_config')
    def test_load_ssl_context_no_env_files(self, mock_get_config):
        """Test SSL context loading when no files in environment."""
        mock_get_config.return_value = {
            "tls_enabled": False,
            "cert_file": None,
            "key_file": None,
            "ca_file": None,
            "verify_client": False,
        }

        result = load_ssl_context()

        # Should not have created SSL context
        assert result["ssl_context"] is None
        assert result["cert_file"] is None
        assert result["key_file"] is None
        assert result["ca_file"] is None
        assert result["verify_client"] is False

    @patch('akosha.websocket.tls_config.create_ssl_context')
    def test_load_ssl_context_ssl_error(self, mock_create_ssl):
        """Test SSL context loading when certificate files are invalid."""
        mock_create_ssl.side_effect = Exception("Invalid certificate")

        with pytest.raises(Exception, match="Invalid certificate"):
            load_ssl_context(
                cert_file='/invalid/cert.pem',
                key_file='/invalid/key.pem'
            )

    @patch('akosha.websocket.tls_config.create_ssl_context')
    def test_load_ssl_context_tls_disabled(self, mock_create_ssl):
        """Test SSL context loading when TLS is disabled."""
        mock_create_ssl.return_value = None

        result = load_ssl_context(tls_enabled=False)

        # Should not create SSL context
        mock_create_ssl.assert_not_called()
        assert result["ssl_context"] is None
        assert result["verify_client"] is False

    @patch('akosha.websocket.tls_config.get_websocket_tls_config')
    @patch('akosha.websocket.tls_config.create_ssl_context')
    def test_load_ssl_context_partial_env_config(self, mock_create_ssl, mock_get_config):
        """Test SSL context loading with partial environment configuration."""
        mock_context = MagicMock()
        mock_create_ssl.return_value = mock_context
        mock_get_config.return_value = {
            "tls_enabled": True,
            "cert_file": "/env/cert.pem",
            "key_file": None,  # Missing key file
            "ca_file": None,
            "verify_client": False,
        }

        result = load_ssl_context()

        # Should not create SSL context without both cert and key
        mock_create_ssl.assert_not_called()
        assert result["ssl_context"] is None
        assert result["cert_file"] == "/env/cert.pem"
        assert result["key_file"] is None

    def test_get_websocket_tls_config_edge_cases(self):
        """Test TLS config with edge cases in environment variables."""
        test_cases = [
            ('', False),  # Empty string
            ('yes', False),  # Non-standard boolean
            ('no', False),
            ('on', False),
            ('off', False),
            (None, False),  # None value
        ]

        for value, expected in test_cases:
            with patch.dict(os.environ, {'AKOSHA_WS_TLS_ENABLED': str(value) if value is not None else ''}):
                config = get_websocket_tls_config()
                assert config["tls_enabled"] == expected

    @patch('akosha.websocket.tls_config.create_ssl_context')
    @patch('builtins.open', mock_open(read_data='fake cert data'))
    def test_load_ssl_context_with_file_operations(self, mock_create_ssl):
        """Test SSL context loading includes file path information."""
        mock_context = MagicMock()
        mock_create_ssl.return_value = mock_context

        cert_file = '/path/to/cert.pem'
        key_file = '/path/to/key.pem'
        ca_file = '/path/to/ca.pem'

        result = load_ssl_context(
            cert_file=cert_file,
            key_file=key_file,
            ca_file=ca_file,
            verify_client=True
        )

        # Should return all file paths in result
        assert result["cert_file"] == cert_file
        assert result["key_file"] == key_file
        assert result["ca_file"] == ca_file
        assert result["verify_client"] is True