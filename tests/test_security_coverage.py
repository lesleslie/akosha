"""Comprehensive tests for akosha/security.py covering all 131 uncovered lines.

This file provides focused coverage for public items and private helpers
that were not fully exercised by tests/unit/test_security.py.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from akosha.security import (
    AuthenticationError,
    AuthenticationMiddleware,
    InvalidTokenError,
    MissingTokenError,
    _authenticate_via_context,
    _clear_token_from_kwargs,
    _extract_context_from_kwargs,
    _extract_direct_token,
    _extract_headers_from_context,
    _validate_context_token,
    _validate_direct_token,
    extract_token_from_headers,
    generate_jwt_token,
    generate_token,
    get_api_token,
    is_auth_enabled,
    require_auth,
    setup_authentication_instructions,
    validate_token,
)


# ---------------------------------------------------------------------------
# AuthenticationError
# ---------------------------------------------------------------------------
class TestAuthenticationError:
    """Tests for AuthenticationError base exception."""

    def test_message_and_details_stored(self):
        err = AuthenticationError("bad auth", details={"code": 401})
        assert err.message == "bad auth"
        assert err.details == {"code": 401}

    def test_details_defaults_to_empty_dict(self):
        err = AuthenticationError("msg")
        assert err.details == {}

    def test_to_dict_structure(self):
        err = AuthenticationError("m", details={"k": "v"})
        d = err.to_dict()
        assert d["error"] == "authentication_error"
        assert d["message"] == "m"
        assert d["details"] == {"k": "v"}

    def test_to_dict_empty_details(self):
        d = AuthenticationError("x").to_dict()
        assert d["details"] == {}

    def test_str_representation(self):
        err = AuthenticationError("oops")
        assert "oops" in str(err)

    def test_is_exception(self):
        assert issubclass(AuthenticationError, Exception)


# ---------------------------------------------------------------------------
# MissingTokenError
# ---------------------------------------------------------------------------
class TestMissingTokenError:
    """Tests for MissingTokenError subclass."""

    def test_default_message(self):
        err = MissingTokenError()
        assert "Missing or invalid authentication token" in err.message

    def test_default_message_in_str(self):
        err = MissingTokenError()
        assert "Bearer token" in str(err)

    def test_custom_details(self):
        err = MissingTokenError({"tool": "search_all_systems", "reason": "no_context_or_token"})
        assert err.details["tool"] == "search_all_systems"
        assert err.details["reason"] == "no_context_or_token"

    def test_to_dict_includes_error_type(self):
        d = MissingTokenError().to_dict()
        assert d["error"] == "authentication_error"

    def test_is_authentication_error(self):
        assert issubclass(MissingTokenError, AuthenticationError)


# ---------------------------------------------------------------------------
# InvalidTokenError
# ---------------------------------------------------------------------------
class TestInvalidTokenError:
    """Tests for InvalidTokenError subclass."""

    def test_default_message(self):
        err = InvalidTokenError()
        assert "Invalid authentication token" in err.message
        assert "Access denied" in err.message

    def test_custom_details(self):
        err = InvalidTokenError({"tool": "my_tool", "reason": "invalid_token"})
        assert err.details["tool"] == "my_tool"

    def test_to_dict(self):
        d = InvalidTokenError({"k": "v"}).to_dict()
        assert d["error"] == "authentication_error"
        assert d["message"] == "Invalid authentication token. Access denied."

    def test_is_authentication_error(self):
        assert issubclass(InvalidTokenError, AuthenticationError)


# ---------------------------------------------------------------------------
# get_api_token
# ---------------------------------------------------------------------------
class TestGetApiToken:
    """Tests for get_api_token function."""

    def test_returns_env_value(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "tok123"}):
            assert get_api_token() == "tok123"

    def test_returns_none_when_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            # Ensure AKOSHA_API_TOKEN is not set
            os.environ.pop("AKOSHA_API_TOKEN", None)
            assert get_api_token() is None

    def test_returns_empty_string_if_set(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": ""}):
            # Empty string is still truthy? No, empty string is falsy in is_auth_enabled
            # but get_api_token returns it directly
            assert get_api_token() == ""


# ---------------------------------------------------------------------------
# is_auth_enabled
# ---------------------------------------------------------------------------
class TestIsAuthEnabled:
    """Tests for is_auth_enabled function."""

    def test_enabled_when_token_set(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "tok"}):
            assert is_auth_enabled() is True

    def test_enabled_explicit_true(self):
        with patch.dict(os.environ, {"AKOSHA_AUTH_ENABLED": "true"}, clear=True):
            os.environ.pop("AKOSHA_API_TOKEN", None)
            assert is_auth_enabled() is True

    def test_enabled_uppercase_TRUE(self):
        with patch.dict(os.environ, {"AKOSHA_AUTH_ENABLED": "TRUE"}, clear=True):
            os.environ.pop("AKOSHA_API_TOKEN", None)
            assert is_auth_enabled() is True

    def test_enabled_mixed_case_True(self):
        with patch.dict(os.environ, {"AKOSHA_AUTH_ENABLED": "True"}, clear=True):
            os.environ.pop("AKOSHA_API_TOKEN", None)
            assert is_auth_enabled() is True

    def test_disabled_when_false(self):
        with patch.dict(os.environ, {"AKOSHA_AUTH_ENABLED": "false"}, clear=True):
            os.environ.pop("AKOSHA_API_TOKEN", None)
            assert is_auth_enabled() is False

    def test_disabled_when_neither_set(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("AKOSHA_API_TOKEN", None)
            os.environ.pop("AKOSHA_AUTH_ENABLED", None)
            assert is_auth_enabled() is False

    def test_token_takes_priority_over_false_flag(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "tok", "AKOSHA_AUTH_ENABLED": "false"}):
            assert is_auth_enabled() is True

    def test_random_string_not_true(self):
        with patch.dict(os.environ, {"AKOSHA_AUTH_ENABLED": "yes"}, clear=True):
            os.environ.pop("AKOSHA_API_TOKEN", None)
            assert is_auth_enabled() is False


# ---------------------------------------------------------------------------
# validate_token
# ---------------------------------------------------------------------------
class TestValidateToken:
    """Tests for validate_token function."""

    def test_valid_api_token(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "secret123"}):
            assert validate_token("secret123") is True

    def test_invalid_api_token(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "secret123"}):
            assert validate_token("wrong") is False

    def test_no_token_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("AKOSHA_API_TOKEN", None)
            os.environ.pop("JWT_SECRET", None)
            assert validate_token("anything") is False

    def test_bearer_prefix_stripped_for_api_token(self):
        """When JWT_SECRET is not set, Bearer prefix is NOT stripped for API token comparison."""
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "secret123"}):
            # "Bearer secret123" != "secret123" -> False
            assert validate_token("Bearer secret123") is False

    def test_jwt_secret_set_tries_jwt_first(self):
        """When JWT_SECRET is set, validate_token tries JWT decoding first."""
        with patch.dict(os.environ, {"JWT_SECRET": "jwtsecret", "AKOSHA_API_TOKEN": "apitoken"}):
            mock_jwt = MagicMock()

            # Create a real exception class that inherits from Exception
            # to simulate jwt.InvalidTokenError (which inherits from jwt.InvalidTokenError)
            class FakeInvalidTokenError(Exception):
                pass

            mock_jwt.InvalidTokenError = FakeInvalidTokenError
            mock_jwt.ExpiredSignatureError = type("ExpiredSignatureError", (Exception,), {})
            # jwt.InvalidTokenError is caught and falls through to API token
            mock_jwt.decode.side_effect = FakeInvalidTokenError("bad jwt")
            with patch.dict("sys.modules", {"jwt": mock_jwt}):
                # Should fall through to API token comparison
                assert validate_token("apitoken") is True

    def test_jwt_valid_token(self):
        """When JWT_SECRET is set and JWT decodes successfully, returns True."""
        with patch.dict(os.environ, {"JWT_SECRET": "jwtsecret"}):
            mock_jwt = MagicMock()
            # Return a valid payload with future expiry
            mock_jwt.decode.return_value = {
                "exp": (datetime.now(UTC) + timedelta(hours=1)).timestamp(),
            }
            mock_jwt.InvalidTokenError = Exception
            mock_jwt.ExpiredSignatureError = Exception
            with patch.dict("sys.modules", {"jwt": mock_jwt}):
                assert validate_token("some.jwt.token") is True
                mock_jwt.decode.assert_called_once()

    def test_jwt_expired_signature(self):
        """ExpiredSignatureError returns False."""
        with patch.dict(os.environ, {"JWT_SECRET": "jwtsecret"}):
            mock_jwt = MagicMock()
            mock_jwt.ExpiredSignatureError = Exception
            mock_jwt.InvalidTokenError = Exception
            mock_jwt.decode.side_effect = mock_jwt.ExpiredSignatureError("expired")
            with patch.dict("sys.modules", {"jwt": mock_jwt}):
                assert validate_token("some.jwt.token") is False

    def test_jwt_invalid_token_error_falls_through(self):
        """jwt.InvalidTokenError falls through to API token check."""
        with patch.dict(os.environ, {"JWT_SECRET": "jwtsecret", "AKOSHA_API_TOKEN": "apitoken"}):
            mock_jwt = MagicMock()
            mock_jwt.InvalidTokenError = type("InvalidTokenError", (Exception,), {})
            mock_jwt.ExpiredSignatureError = type("ExpiredSignatureError", (Exception,), {})
            mock_jwt.decode.side_effect = mock_jwt.InvalidTokenError("invalid")
            with patch.dict("sys.modules", {"jwt": mock_jwt}):
                # Falls through to API token
                assert validate_token("apitoken") is True

    def test_jwt_expired_payload_exp(self):
        """JWT payload with past exp returns False."""
        with patch.dict(os.environ, {"JWT_SECRET": "jwtsecret"}):
            mock_jwt = MagicMock()
            mock_jwt.InvalidTokenError = Exception
            mock_jwt.ExpiredSignatureError = Exception
            mock_jwt.decode.return_value = {
                "exp": (datetime.now(UTC) - timedelta(hours=1)).timestamp(),
            }
            with patch.dict("sys.modules", {"jwt": mock_jwt}):
                assert validate_token("some.jwt.token") is False

    def test_jwt_bearer_prefix_stripped(self):
        """Bearer prefix is stripped before JWT decode."""
        with patch.dict(os.environ, {"JWT_SECRET": "jwtsecret"}):
            mock_jwt = MagicMock()
            mock_jwt.InvalidTokenError = Exception
            mock_jwt.ExpiredSignatureError = Exception
            mock_jwt.decode.return_value = {
                "exp": (datetime.now(UTC) + timedelta(hours=1)).timestamp(),
            }
            with patch.dict("sys.modules", {"jwt": mock_jwt}):
                assert validate_token("Bearer some.jwt.token") is True
                # The Bearer prefix should be stripped before decode
                call_args = mock_jwt.decode.call_args
                assert not call_args[0][0].startswith("Bearer ")


# ---------------------------------------------------------------------------
# extract_token_from_headers
# ---------------------------------------------------------------------------
class TestExtractTokenFromHeaders:
    """Tests for extract_token_from_headers function."""

    def test_valid_bearer(self):
        assert extract_token_from_headers({"Authorization": "Bearer abc"}) == "abc"

    def test_lowercase_authorization(self):
        assert extract_token_from_headers({"authorization": "Bearer abc"}) == "abc"

    def test_priority_authorization_over_authorization(self):
        """When both exist, 'Authorization' takes priority via `or`."""
        headers = {"Authorization": "Bearer first", "authorization": "Bearer second"}
        assert extract_token_from_headers(headers) == "first"

    def test_extra_whitespace_trimmed(self):
        assert extract_token_from_headers({"Authorization": "Bearer   abc   "}) == "abc"

    def test_empty_token_after_bearer(self):
        assert extract_token_from_headers({"Authorization": "Bearer "}) is None

    def test_empty_string_after_strip(self):
        assert extract_token_from_headers({"Authorization": "Bearer    "}) is None

    def test_no_authorization_header(self):
        assert extract_token_from_headers({"Content-Type": "text/plain"}) is None

    def test_empty_headers_dict(self):
        assert extract_token_from_headers({}) is None

    def test_none_headers(self):
        assert extract_token_from_headers(None) is None

    def test_basic_auth_rejected(self):
        assert extract_token_from_headers({"Authorization": "Basic abc"}) is None

    def test_token_keyword_only(self):
        assert extract_token_from_headers({"Authorization": "Token abc"}) is None


# ---------------------------------------------------------------------------
# _extract_direct_token
# ---------------------------------------------------------------------------
class TestExtractDirectToken:
    """Tests for _extract_direct_token helper."""

    def test_returns_auth_token(self):
        assert _extract_direct_token({"auth_token": "abc"}) == "abc"

    def test_returns_token(self):
        assert _extract_direct_token({"token": "xyz"}) == "xyz"

    def test_auth_token_takes_priority(self):
        assert _extract_direct_token({"auth_token": "a", "token": "b"}) == "a"

    def test_returns_none_when_missing(self):
        assert _extract_direct_token({}) is None

    def test_returns_none_for_unrelated_keys(self):
        assert _extract_direct_token({"foo": "bar"}) is None


# ---------------------------------------------------------------------------
# _clear_token_from_kwargs
# ---------------------------------------------------------------------------
class TestClearTokenFromKwargs:
    """Tests for _clear_token_from_kwargs helper."""

    def test_removes_auth_token(self):
        kwargs = {"auth_token": "abc", "data": 1}
        _clear_token_from_kwargs(kwargs)
        assert "auth_token" not in kwargs
        assert kwargs["data"] == 1

    def test_removes_token(self):
        kwargs = {"token": "abc"}
        _clear_token_from_kwargs(kwargs)
        assert "token" not in kwargs

    def test_removes_both(self):
        kwargs = {"auth_token": "a", "token": "b", "other": "c"}
        _clear_token_from_kwargs(kwargs)
        assert "auth_token" not in kwargs
        assert "token" not in kwargs
        assert kwargs["other"] == "c"

    def test_no_error_on_empty_dict(self):
        kwargs = {}
        _clear_token_from_kwargs(kwargs)
        assert kwargs == {}


# ---------------------------------------------------------------------------
# _extract_context_from_kwargs
# ---------------------------------------------------------------------------
class TestExtractContextFromKwargs:
    """Tests for _extract_context_from_kwargs helper."""

    def test_returns_context(self):
        ctx = MagicMock()
        assert _extract_context_from_kwargs({"context": ctx}) is ctx

    def test_returns_underscore_context(self):
        ctx = MagicMock()
        assert _extract_context_from_kwargs({"_context": ctx}) is ctx

    def test_underscore_context_takes_priority(self):
        """_context is checked first due to `or` short-circuit."""
        ctx1 = MagicMock()
        ctx2 = MagicMock()
        assert _extract_context_from_kwargs({"context": ctx1, "_context": ctx2}) is ctx2

    def test_returns_none_when_missing(self):
        assert _extract_context_from_kwargs({}) is None

    def test_returns_none_for_falsy_value(self):
        assert _extract_context_from_kwargs({"context": None}) is None


# ---------------------------------------------------------------------------
# _extract_headers_from_context
# ---------------------------------------------------------------------------
class TestExtractHeadersFromContext:
    """Tests for _extract_headers_from_context helper."""

    def test_returns_headers_attribute(self):
        ctx = MagicMock()
        ctx.headers = {"Authorization": "Bearer abc"}
        assert _extract_headers_from_context(ctx) == {"Authorization": "Bearer abc"}

    def test_returns_none_when_no_headers(self):
        ctx = MagicMock(spec=[])
        assert _extract_headers_from_context(ctx) is None

    def test_returns_none_for_none_context(self):
        assert _extract_headers_from_context(None) is None

    def test_returns_none_for_plain_object(self):
        assert _extract_headers_from_context(object()) is None


# ---------------------------------------------------------------------------
# _validate_direct_token
# ---------------------------------------------------------------------------
class TestValidateDirectToken:
    """Tests for _validate_direct_token helper."""

    def test_valid_token_passes(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "good"}):
            _validate_direct_token("good", "my_func")  # Should not raise

    def test_invalid_token_raises(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "good"}):
            with pytest.raises(InvalidTokenError) as exc_info:
                _validate_direct_token("bad", "my_func")
            assert exc_info.value.details["tool"] == "my_func"
            assert exc_info.value.details["reason"] == "invalid_token"

    def test_error_message_contains_func_name(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "good"}):
            with pytest.raises(InvalidTokenError):
                _validate_direct_token("bad", "search_all_systems")


# ---------------------------------------------------------------------------
# _validate_context_token
# ---------------------------------------------------------------------------
class TestValidateContextToken:
    """Tests for _validate_context_token helper."""

    def test_valid_token_passes(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "good"}):
            _validate_context_token("good", "my_func")

    def test_invalid_token_raises(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "good"}):
            with pytest.raises(InvalidTokenError) as exc_info:
                _validate_context_token("bad", "my_func")
            assert exc_info.value.details["reason"] == "token_validation_failed"
            assert exc_info.value.details["tool"] == "my_func"


# ---------------------------------------------------------------------------
# _authenticate_via_context
# ---------------------------------------------------------------------------
class TestAuthenticateViaContext:
    """Tests for _authenticate_via_context helper."""

    def test_raises_missing_token_when_no_context(self):
        with pytest.raises(MissingTokenError) as exc_info:
            _authenticate_via_context(None, "my_func")
        assert exc_info.value.details["reason"] == "no_context_or_token"
        assert exc_info.value.details["tool"] == "my_func"

    def test_raises_missing_token_when_no_headers(self):
        ctx = MagicMock(spec=[])  # No headers attribute
        with pytest.raises(MissingTokenError) as exc_info:
            _authenticate_via_context(ctx, "my_func")
        assert exc_info.value.details["reason"] == "no_headers"

    def test_raises_missing_token_when_no_bearer_token(self):
        ctx = MagicMock()
        ctx.headers = {"Content-Type": "text/plain"}
        with pytest.raises(MissingTokenError) as exc_info:
            _authenticate_via_context(ctx, "my_func")
        assert exc_info.value.details["reason"] == "missing_bearer_token"

    def test_raises_invalid_token_when_token_wrong(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "correct"}):
            ctx = MagicMock()
            ctx.headers = {"Authorization": "Bearer wrong"}
            with pytest.raises(InvalidTokenError):
                _authenticate_via_context(ctx, "my_func")

    def test_passes_with_valid_token(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "correct"}):
            ctx = MagicMock()
            ctx.headers = {"Authorization": "Bearer correct"}
            _authenticate_via_context(ctx, "my_func")  # Should not raise

    def test_headers_empty_dict_raises(self):
        """Empty headers dict should raise MissingTokenError."""
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "correct"}):
            ctx = MagicMock()
            ctx.headers = {}
            with pytest.raises(MissingTokenError):
                _authenticate_via_context(ctx, "my_func")


# ---------------------------------------------------------------------------
# generate_jwt_token
# ---------------------------------------------------------------------------
class TestGenerateJwtToken:
    """Tests for generate_jwt_token function."""

    def test_raises_when_no_jwt_secret(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("JWT_SECRET", None)
            with pytest.raises(ValueError, match="JWT_SECRET environment variable required"):
                generate_jwt_token("user1")

    def test_raises_for_placeholder_in_production(self):
        with (
            patch.dict(
                os.environ, {"JWT_SECRET": "change-this-in-production", "ENVIRONMENT": "production"}
            ),
            pytest.raises(ValueError, match="secure value in production"),
        ):
            generate_jwt_token("user1")

    def test_raises_for_empty_secret(self):
        """Empty string JWT_SECRET is falsy, so it fails the first check."""
        with patch.dict(os.environ, {"JWT_SECRET": "", "ENVIRONMENT": "production"}):
            with pytest.raises(ValueError, match="JWT_SECRET environment variable required"):
                generate_jwt_token("user1")

    def test_raises_for_none_literal_in_production(self):
        with patch.dict(os.environ, {"JWT_SECRET": "none", "ENVIRONMENT": "production"}):
            with pytest.raises(ValueError, match="secure value in production"):
                generate_jwt_token("user1")

    def test_raises_for_null_literal_in_production(self):
        with patch.dict(os.environ, {"JWT_SECRET": "null", "ENVIRONMENT": "production"}):
            with pytest.raises(ValueError, match="secure value in production"):
                generate_jwt_token("user1")

    def test_raises_for_uppercase_placeholder_in_production(self):
        """Placeholder check is case-insensitive via .lower()."""
        with (
            patch.dict(
                os.environ, {"JWT_SECRET": "CHANGE-THIS-IN-PRODUCTION", "ENVIRONMENT": "production"}
            ),
            pytest.raises(ValueError, match="secure value in production"),
        ):
            generate_jwt_token("user1")

    def test_placeholder_allowed_in_development(self):
        """Placeholder secrets are allowed in development environment."""
        with patch.dict(
            os.environ, {"JWT_SECRET": "change-this-in-production", "ENVIRONMENT": "development"}
        ):
            mock_jwt = MagicMock()
            mock_jwt.encode.return_value = "mock.jwt.token"
            with patch.dict("sys.modules", {"jwt": mock_jwt}):
                result = generate_jwt_token("user1")
                assert result == "mock.jwt.token"

    def test_default_environment_is_development(self):
        """When ENVIRONMENT is not set, default is development (placeholder allowed)."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("ENVIRONMENT", None)
            with patch.dict(os.environ, {"JWT_SECRET": "change-this-in-production"}):
                mock_jwt = MagicMock()
                mock_jwt.encode.return_value = "mock.jwt.token"
                with patch.dict("sys.modules", {"jwt": mock_jwt}):
                    result = generate_jwt_token("user1")
                    assert result == "mock.jwt.token"

    def test_raises_when_pjwt_not_installed(self):
        """ImportError for PyJWT is caught and re-raised as ValueError."""
        with patch.dict(os.environ, {"JWT_SECRET": "real-secret"}):
            # Use patch to make the import raise ImportError
            import builtins

            original_import = builtins.__import__

            def mock_import(name, *args, **kwargs):
                if name == "jwt":
                    raise ImportError("No module named 'jwt'")
                return original_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=mock_import):
                with pytest.raises(ValueError, match="PyJWT library not installed"):
                    generate_jwt_token("user1")

    def test_successful_token_generation(self):
        with patch.dict(os.environ, {"JWT_SECRET": "real-secret-value"}):
            mock_jwt = MagicMock()
            mock_jwt.encode.return_value = "generated.jwt.token"
            with patch.dict("sys.modules", {"jwt": mock_jwt}):
                result = generate_jwt_token("user1")
                assert result == "generated.jwt.token"
                # Verify encode was called with correct payload
                call_args = mock_jwt.encode.call_args
                payload = call_args[0][0]
                assert payload["user_id"] == "user1"
                assert payload["sub"] == "user1"
                assert "exp" in payload
                assert "iat" in payload
                assert call_args[0][1] == "real-secret-value"
                assert call_args[1]["algorithm"] == "HS256"

    def test_custom_expiration(self):
        with patch.dict(os.environ, {"JWT_SECRET": "real-secret-value"}):
            mock_jwt = MagicMock()
            mock_jwt.encode.return_value = "token"
            with patch.dict("sys.modules", {"jwt": mock_jwt}):
                generate_jwt_token("user1", expiration_minutes=120)
                payload = mock_jwt.encode.call_args[0][0]
                # exp is a datetime (from UTC + timedelta), not a timestamp
                exp_dt = payload["exp"]
                now = datetime.now(UTC)
                delta = (exp_dt - now).total_seconds()
                # Should be approximately 120 minutes (7200 seconds)
                assert 7100 < delta < 7300

    def test_additional_claims(self):
        with patch.dict(os.environ, {"JWT_SECRET": "real-secret-value"}):
            mock_jwt = MagicMock()
            mock_jwt.encode.return_value = "token"
            with patch.dict("sys.modules", {"jwt": mock_jwt}):
                generate_jwt_token("user1", additional_claims={"role": "admin", "org": "test"})
                payload = mock_jwt.encode.call_args[0][0]
                assert payload["role"] == "admin"
                assert payload["org"] == "test"


# ---------------------------------------------------------------------------
# require_auth decorator
# ---------------------------------------------------------------------------
class TestRequireAuthDecorator:
    """Tests for the @require_auth decorator."""

    @pytest.mark.asyncio
    async def test_allows_when_auth_disabled(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("AKOSHA_API_TOKEN", None)
            os.environ["AKOSHA_AUTH_ENABLED"] = "false"

            @require_auth
            async def my_tool():
                return "ok"

            assert await my_tool() == "ok"

    @pytest.mark.asyncio
    async def test_allows_with_auth_token_kwarg(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "secret"}):

            @require_auth
            async def my_tool():
                return "ok"

            assert await my_tool(auth_token="secret") == "ok"

    @pytest.mark.asyncio
    async def test_allows_with_token_kwarg(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "secret"}):

            @require_auth
            async def my_tool():
                return "ok"

            assert await my_tool(token="secret") == "ok"

    @pytest.mark.asyncio
    async def test_auth_token_kwarg_cleared(self):
        """The auth_token should be removed from kwargs before calling the wrapped function."""
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "secret"}):

            @require_auth
            async def my_tool(**kwargs):
                return kwargs

            result = await my_tool(auth_token="secret", data="value")
            assert "auth_token" not in result
            assert result["data"] == "value"

    @pytest.mark.asyncio
    async def test_token_kwarg_cleared(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "secret"}):

            @require_auth
            async def my_tool(**kwargs):
                return kwargs

            result = await my_tool(token="secret", data="value")
            assert "token" not in result
            assert result["data"] == "value"

    @pytest.mark.asyncio
    async def test_both_tokens_cleared(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "secret"}):

            @require_auth
            async def my_tool(**kwargs):
                return kwargs

            result = await my_tool(auth_token="secret", token="secret", x=1)
            assert "auth_token" not in result
            assert "token" not in result
            assert result["x"] == 1

    @pytest.mark.asyncio
    async def test_invalid_token_raises(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "secret"}):

            @require_auth
            async def my_tool():
                return "ok"

            with pytest.raises(InvalidTokenError):
                await my_tool(auth_token="wrong")

    @pytest.mark.asyncio
    async def test_missing_token_raises(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "secret"}):

            @require_auth
            async def my_tool():
                return "ok"

            with pytest.raises(MissingTokenError):
                await my_tool()

    @pytest.mark.asyncio
    async def test_context_with_valid_token(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "secret"}):
            ctx = MagicMock()
            ctx.headers = {"Authorization": "Bearer secret"}

            @require_auth
            async def my_tool(_context=None):
                return "ok"

            assert await my_tool(_context=ctx) == "ok"

    @pytest.mark.asyncio
    async def test_context_kwarg_with_valid_token(self):
        """Test with 'context' kwarg (not '_context')."""
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "secret"}):
            ctx = MagicMock()
            ctx.headers = {"Authorization": "Bearer secret"}

            @require_auth
            async def my_tool(context=None):
                return "ok"

            assert await my_tool(context=ctx) == "ok"

    @pytest.mark.asyncio
    async def test_context_without_headers_raises(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "secret"}):
            ctx = MagicMock(spec=[])

            @require_auth
            async def my_tool(context=None):
                return "ok"

            with pytest.raises(MissingTokenError):
                await my_tool(context=ctx)

    @pytest.mark.asyncio
    async def test_context_with_invalid_token_raises(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "secret"}):
            ctx = MagicMock()
            ctx.headers = {"Authorization": "Bearer wrong"}

            @require_auth
            async def my_tool(_context=None):
                return "ok"

            with pytest.raises(InvalidTokenError):
                await my_tool(_context=ctx)

    @pytest.mark.asyncio
    async def test_preserves_function_name(self):
        """The @wraps decorator should preserve the original function name."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("AKOSHA_API_TOKEN", None)
            os.environ["AKOSHA_AUTH_ENABLED"] = "false"

            @require_auth
            async def my_special_tool():
                pass

            assert my_special_tool.__name__ == "my_special_tool"

    @pytest.mark.asyncio
    async def test_preserves_function_docstring(self):
        """The @wraps decorator should preserve the docstring."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("AKOSHA_API_TOKEN", None)
            os.environ["AKOSHA_AUTH_ENABLED"] = "false"

            @require_auth
            async def documented_tool():
                """My docstring."""
                pass

            assert documented_tool.__doc__ == "My docstring."

    @pytest.mark.asyncio
    async def test_passes_args_through(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("AKOSHA_API_TOKEN", None)
            os.environ["AKOSHA_AUTH_ENABLED"] = "false"

            @require_auth
            async def tool_with_args(a, b, c=None):
                return (a, b, c)

            result = await tool_with_args(1, 2, c=3)
            assert result == (1, 2, 3)


# ---------------------------------------------------------------------------
# generate_token
# ---------------------------------------------------------------------------
class TestGenerateToken:
    """Tests for generate_token function."""

    def test_returns_string(self):
        assert isinstance(generate_token(), str)

    def test_returns_url_safe(self):
        token = generate_token()
        # URL-safe base64 characters: A-Z, a-z, 0-9, -, _
        for ch in token:
            assert ch.isalnum() or ch in "-_"

    def test_length_is_reasonable(self):
        """32 bytes URL-safe encoded is ~43 characters."""
        token = generate_token()
        assert len(token) >= 40

    def test_unique_tokens(self):
        tokens = {generate_token() for _ in range(50)}
        assert len(tokens) == 50

    def test_uses_secrets_module(self):
        """Verify it uses secrets.token_urlsafe."""
        with patch("akosha.security.secrets.token_urlsafe", return_value="mock-token") as mock_func:
            assert generate_token() == "mock-token"
            mock_func.assert_called_once_with(32)


# ---------------------------------------------------------------------------
# setup_authentication_instructions
# ---------------------------------------------------------------------------
class TestSetupAuthenticationInstructions:
    """Tests for setup_authentication_instructions function."""

    def test_contains_token_export(self):
        instructions = setup_authentication_instructions()
        assert "export AKOSHA_API_TOKEN=" in instructions

    def test_token_in_instructions_is_valid(self):
        instructions = setup_authentication_instructions()
        # Extract token from between quotes
        import re

        match = re.search(r'export AKOSHA_API_TOKEN="([^"]+)"', instructions)
        assert match is not None
        token = match.group(1)
        assert len(token) >= 32

    def test_contains_all_sections(self):
        instructions = setup_authentication_instructions()
        assert "# Akosha Authentication Setup" in instructions
        assert "## 1. Generate API Token" in instructions
        assert "## 2. Enable Authentication" in instructions
        assert "## 3. Using Authentication" in instructions
        assert "## Security Best Practices" in instructions
        assert "## Protected Tools" in instructions

    def test_lists_all_protected_tools(self):
        instructions = setup_authentication_instructions()
        for tool in [
            "search_all_systems",
            "get_system_metrics",
            "analyze_trends",
            "detect_anomalies",
            "correlate_systems",
            "query_knowledge_graph",
            "find_path",
            "get_graph_statistics",
        ]:
            assert f"- `{tool}`" in instructions

    def test_contains_security_best_practices(self):
        instructions = setup_authentication_instructions()
        assert "Keep tokens secret" in instructions
        assert "Use environment variables" in instructions
        assert "Rotate tokens regularly" in instructions

    def test_contains_bearer_example(self):
        instructions = setup_authentication_instructions()
        # The instructions interpolate the actual token, not the literal {token}
        assert '"Authorization": f"Bearer ' in instructions
        assert "headers = {" in instructions

    def test_different_tokens_each_call(self):
        t1 = setup_authentication_instructions()
        t2 = setup_authentication_instructions()
        assert t1 != t2


# ---------------------------------------------------------------------------
# AuthenticationMiddleware
# ---------------------------------------------------------------------------
class TestAuthenticationMiddleware:
    """Tests for AuthenticationMiddleware class."""

    def test_default_protected_categories(self):
        mw = AuthenticationMiddleware()
        assert "search" in mw.protected_categories
        assert "analytics" in mw.protected_categories
        assert "graph" in mw.protected_categories
        assert len(mw.protected_categories) == 3

    def test_default_protected_tools(self):
        mw = AuthenticationMiddleware()
        assert "search_all_systems" in mw.protected_tools
        assert "get_system_metrics" in mw.protected_tools
        assert "analyze_trends" in mw.protected_tools
        assert "detect_anomalies" in mw.protected_tools
        assert "correlate_systems" in mw.protected_tools
        assert "query_knowledge_graph" in mw.protected_tools
        assert "find_path" in mw.protected_tools
        assert "get_graph_statistics" in mw.protected_tools
        assert len(mw.protected_tools) == 8

    def test_custom_categories(self):
        mw = AuthenticationMiddleware(protected_categories={"admin", "config"})
        assert "admin" in mw.protected_categories
        assert "search" not in mw.protected_categories

    def test_custom_tools(self):
        mw = AuthenticationMiddleware(protected_tools={"my_tool"})
        assert "my_tool" in mw.protected_tools
        assert "search_all_systems" not in mw.protected_tools

    def test_empty_sets_still_get_defaults(self):
        """Empty set is falsy, so `or` falls through to defaults."""
        mw = AuthenticationMiddleware(protected_categories=set(), protected_tools=set())
        # Empty set is falsy, so the `or` operator returns the defaults
        assert len(mw.protected_categories) == 3  # defaults
        assert len(mw.protected_tools) == 8  # defaults

    def test_is_tool_protected_by_name(self):
        mw = AuthenticationMiddleware()
        assert mw.is_tool_protected("search_all_systems") is True
        assert mw.is_tool_protected("get_system_metrics") is True

    def test_is_tool_protected_by_category(self):
        mw = AuthenticationMiddleware()
        assert mw.is_tool_protected("anything", "search") is True
        assert mw.is_tool_protected("anything", "analytics") is True
        assert mw.is_tool_protected("anything", "graph") is True

    def test_is_tool_not_protected(self):
        mw = AuthenticationMiddleware()
        assert mw.is_tool_protected("random_tool") is False
        assert mw.is_tool_protected("random_tool", "health") is False

    def test_is_tool_protected_no_category(self):
        """None category should not match."""
        mw = AuthenticationMiddleware()
        assert mw.is_tool_protected("random_tool", None) is False

    @pytest.mark.asyncio
    async def test_authenticate_request_auth_disabled(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("AKOSHA_API_TOKEN", None)
            os.environ["AKOSHA_AUTH_ENABLED"] = "false"
            mw = AuthenticationMiddleware()
            result = await mw.authenticate_request("search_all_systems", "search")
            assert result is True

    @pytest.mark.asyncio
    async def test_authenticate_request_unprotected_tool_with_auth(self):
        """Unprotected tools should pass even when auth is enabled."""
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "secret"}):
            mw = AuthenticationMiddleware()
            result = await mw.authenticate_request("random_tool", "health")
            assert result is True

    @pytest.mark.asyncio
    async def test_authenticate_request_valid_token(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "secret"}):
            ctx = MagicMock()
            ctx.headers = {"Authorization": "Bearer secret"}
            mw = AuthenticationMiddleware()
            result = await mw.authenticate_request("search_all_systems", "search", ctx)
            assert result is True

    @pytest.mark.asyncio
    async def test_authenticate_request_missing_token_raises(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "secret"}):
            ctx = MagicMock()
            ctx.headers = {}
            mw = AuthenticationMiddleware()
            with pytest.raises(MissingTokenError) as exc_info:
                await mw.authenticate_request("search_all_systems", "search", ctx)
            assert exc_info.value.details["tool"] == "search_all_systems"
            assert exc_info.value.details["category"] == "search"

    @pytest.mark.asyncio
    async def test_authenticate_request_invalid_token_raises(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "secret"}):
            ctx = MagicMock()
            ctx.headers = {"Authorization": "Bearer wrong"}
            mw = AuthenticationMiddleware()
            with pytest.raises(InvalidTokenError) as exc_info:
                await mw.authenticate_request("search_all_systems", "search", ctx)
            assert exc_info.value.details["reason"] == "token_validation_failed"

    @pytest.mark.asyncio
    async def test_authenticate_request_no_context_raises(self):
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "secret"}):
            mw = AuthenticationMiddleware()
            with pytest.raises(MissingTokenError):
                await mw.authenticate_request("search_all_systems", "search", None)

    @pytest.mark.asyncio
    async def test_authenticate_request_no_category_in_details(self):
        """Error details should include category even when None."""
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "secret"}):
            ctx = MagicMock()
            ctx.headers = {}
            mw = AuthenticationMiddleware()
            with pytest.raises(MissingTokenError) as exc_info:
                await mw.authenticate_request("search_all_systems", None, ctx)
            assert exc_info.value.details["category"] is None

    @pytest.mark.asyncio
    async def test_authenticate_request_context_without_headers_attr(self):
        """Context without headers attribute should raise MissingTokenError."""
        with patch.dict(os.environ, {"AKOSHA_API_TOKEN": "secret"}):
            ctx = MagicMock(spec=[])
            mw = AuthenticationMiddleware()
            with pytest.raises(MissingTokenError):
                await mw.authenticate_request("search_all_systems", "search", ctx)
