"""Tests for structured error codes."""

from __future__ import annotations


from pmcp.errors import (
    ERROR_MESSAGES,
    ERROR_SUGGESTIONS,
    RETRYABLE_ERRORS,
    ErrorCode,
    GatewayError,
    GatewayException,
    make_error,
)


class TestErrorCode:
    """Tests for ErrorCode enum."""

    def test_all_codes_have_messages(self) -> None:
        """Every error code should have a default message."""
        for code in ErrorCode:
            assert code in ERROR_MESSAGES, f"Missing message for {code}"

    def test_all_codes_have_suggestions(self) -> None:
        """Every error code should have a suggestion."""
        for code in ErrorCode:
            assert code in ERROR_SUGGESTIONS, f"Missing suggestion for {code}"

    def test_code_categories(self) -> None:
        """Error codes should follow category pattern."""
        for code in ErrorCode:
            code_num = int(code.value[1:])  # Remove 'E' prefix
            if code.name.startswith("E1"):
                assert 100 <= code_num < 200, f"Config error {code} should be E1xx"
            elif code.name.startswith("E2"):
                assert 200 <= code_num < 300, f"Connection error {code} should be E2xx"
            elif code.name.startswith("E3"):
                assert 300 <= code_num < 400, f"Tool error {code} should be E3xx"
            elif code.name.startswith("E4"):
                assert 400 <= code_num < 500, f"Policy error {code} should be E4xx"
            elif code.name.startswith("E5"):
                assert 500 <= code_num < 600, f"Install error {code} should be E5xx"


class TestGatewayError:
    """Tests for GatewayError model."""

    def test_basic_error(self) -> None:
        """Test creating a basic error."""
        error = GatewayError(
            code="E301",
            message="Tool not found",
        )
        assert error.code == "E301"
        assert error.message == "Tool not found"
        assert error.details is None
        assert error.suggestion is None
        assert error.retryable is False

    def test_error_with_all_fields(self) -> None:
        """Test creating an error with all fields."""
        error = GatewayError(
            code="E201",
            message="Server offline",
            details={"server": "playwright"},
            suggestion="Check if server is running",
            retryable=True,
        )
        assert error.code == "E201"
        assert error.details == {"server": "playwright"}
        assert error.suggestion == "Check if server is running"
        assert error.retryable is True

    def test_error_serialization(self) -> None:
        """Test error serializes to JSON."""
        error = GatewayError(
            code="E301",
            message="Tool not found",
            details={"tool_id": "test::tool"},
        )
        json_str = error.model_dump_json()
        assert "E301" in json_str
        assert "tool_id" in json_str


class TestGatewayException:
    """Tests for GatewayException."""

    def test_exception_with_default_message(self) -> None:
        """Test exception uses default message from code."""
        exc = GatewayException(ErrorCode.E301_TOOL_NOT_FOUND)
        assert exc.code == ErrorCode.E301_TOOL_NOT_FOUND
        assert exc.message == "Tool not found"
        assert exc.suggestion == "Use gateway.catalog_search to find available tools"

    def test_exception_with_custom_message(self) -> None:
        """Test exception with custom message."""
        exc = GatewayException(
            ErrorCode.E301_TOOL_NOT_FOUND,
            message="Custom message",
        )
        assert exc.message == "Custom message"

    def test_exception_with_details(self) -> None:
        """Test exception with details."""
        exc = GatewayException(
            ErrorCode.E301_TOOL_NOT_FOUND,
            details={"tool_id": "test::tool"},
        )
        assert exc.details == {"tool_id": "test::tool"}

    def test_exception_retryable(self) -> None:
        """Test retryable flag is set correctly."""
        # Non-retryable
        exc = GatewayException(ErrorCode.E301_TOOL_NOT_FOUND)
        assert exc.retryable is False

        # Retryable
        exc = GatewayException(ErrorCode.E201_SERVER_OFFLINE)
        assert exc.retryable is True

    def test_to_error(self) -> None:
        """Test converting exception to GatewayError."""
        exc = GatewayException(
            ErrorCode.E301_TOOL_NOT_FOUND,
            details={"tool_id": "test::tool"},
        )
        error = exc.to_error()

        assert isinstance(error, GatewayError)
        assert error.code == "E301"
        assert error.message == "Tool not found"
        assert error.details == {"tool_id": "test::tool"}

    def test_exception_str(self) -> None:
        """Test exception string representation."""
        exc = GatewayException(ErrorCode.E301_TOOL_NOT_FOUND)
        assert str(exc) == "Tool not found"


class TestMakeError:
    """Tests for make_error helper function."""

    def test_make_error_basic(self) -> None:
        """Test make_error with code only."""
        error = make_error(ErrorCode.E301_TOOL_NOT_FOUND)
        assert error.code == "E301"
        assert error.message == "Tool not found"
        assert error.suggestion is not None

    def test_make_error_with_details(self) -> None:
        """Test make_error with keyword details."""
        error = make_error(
            ErrorCode.E301_TOOL_NOT_FOUND,
            tool_id="test::tool",
            server="playwright",
        )
        assert error.details == {"tool_id": "test::tool", "server": "playwright"}

    def test_make_error_with_custom_message(self) -> None:
        """Test make_error with custom message."""
        error = make_error(
            ErrorCode.E301_TOOL_NOT_FOUND,
            message="Custom: tool xyz not found",
        )
        assert error.message == "Custom: tool xyz not found"

    def test_make_error_retryable(self) -> None:
        """Test retryable is set correctly."""
        # Non-retryable
        error = make_error(ErrorCode.E301_TOOL_NOT_FOUND)
        assert error.retryable is False

        # Retryable
        error = make_error(ErrorCode.E303_TOOL_TIMEOUT)
        assert error.retryable is True


class TestRetryableErrors:
    """Tests for retryable error classification."""

    def test_connection_errors_are_retryable(self) -> None:
        """Connection-related errors should be retryable."""
        assert ErrorCode.E201_SERVER_OFFLINE in RETRYABLE_ERRORS
        assert ErrorCode.E202_SERVER_TIMEOUT in RETRYABLE_ERRORS
        assert ErrorCode.E204_CONNECTION_REFUSED in RETRYABLE_ERRORS

    def test_timeout_errors_are_retryable(self) -> None:
        """Timeout errors should be retryable."""
        assert ErrorCode.E303_TOOL_TIMEOUT in RETRYABLE_ERRORS
        assert ErrorCode.E504_INSTALL_TIMEOUT in RETRYABLE_ERRORS

    def test_policy_errors_not_retryable(self) -> None:
        """Policy errors should not be retryable."""
        assert ErrorCode.E401_SERVER_DENIED not in RETRYABLE_ERRORS
        assert ErrorCode.E402_TOOL_DENIED not in RETRYABLE_ERRORS

    def test_not_found_errors_not_retryable(self) -> None:
        """Not found errors should not be retryable."""
        assert ErrorCode.E301_TOOL_NOT_FOUND not in RETRYABLE_ERRORS
        assert ErrorCode.E203_SERVER_NOT_FOUND not in RETRYABLE_ERRORS
