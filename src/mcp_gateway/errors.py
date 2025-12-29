"""Structured error codes for MCP Gateway.

Error Code Categories:
- E1xx: Configuration errors
- E2xx: Connection/server errors
- E3xx: Tool invocation errors
- E4xx: Policy violations
- E5xx: Installation errors
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ErrorCode(str, Enum):
    """Error codes for MCP Gateway operations."""

    # E1xx - Configuration errors
    E101_CONFIG_NOT_FOUND = "E101"
    E102_CONFIG_INVALID = "E102"
    E103_CONFIG_PARSE_ERROR = "E103"

    # E2xx - Connection/server errors
    E201_SERVER_OFFLINE = "E201"
    E202_SERVER_TIMEOUT = "E202"
    E203_SERVER_NOT_FOUND = "E203"
    E204_CONNECTION_REFUSED = "E204"
    E205_SERVER_PROCESS_DIED = "E205"

    # E3xx - Tool invocation errors
    E301_TOOL_NOT_FOUND = "E301"
    E302_TOOL_EXECUTION_FAILED = "E302"
    E303_TOOL_TIMEOUT = "E303"
    E304_INVALID_ARGUMENTS = "E304"
    E305_OUTPUT_TRUNCATED = "E305"

    # E4xx - Policy violations
    E401_SERVER_DENIED = "E401"
    E402_TOOL_DENIED = "E402"
    E403_OUTPUT_LIMIT_EXCEEDED = "E403"

    # E5xx - Installation errors
    E501_INSTALL_FAILED = "E501"
    E502_API_KEY_MISSING = "E502"
    E503_PLATFORM_NOT_SUPPORTED = "E503"
    E504_INSTALL_TIMEOUT = "E504"
    E505_JOB_NOT_FOUND = "E505"
    E506_INSTALL_CANCELLED = "E506"


# Default messages for each error code
ERROR_MESSAGES: dict[ErrorCode, str] = {
    # Configuration
    ErrorCode.E101_CONFIG_NOT_FOUND: "Configuration file not found",
    ErrorCode.E102_CONFIG_INVALID: "Configuration file is invalid",
    ErrorCode.E103_CONFIG_PARSE_ERROR: "Failed to parse configuration file",
    # Connection
    ErrorCode.E201_SERVER_OFFLINE: "Server is offline",
    ErrorCode.E202_SERVER_TIMEOUT: "Server connection timed out",
    ErrorCode.E203_SERVER_NOT_FOUND: "Server not found",
    ErrorCode.E204_CONNECTION_REFUSED: "Connection refused by server",
    ErrorCode.E205_SERVER_PROCESS_DIED: "Server process terminated unexpectedly",
    # Tool invocation
    ErrorCode.E301_TOOL_NOT_FOUND: "Tool not found",
    ErrorCode.E302_TOOL_EXECUTION_FAILED: "Tool execution failed",
    ErrorCode.E303_TOOL_TIMEOUT: "Tool execution timed out",
    ErrorCode.E304_INVALID_ARGUMENTS: "Invalid tool arguments",
    ErrorCode.E305_OUTPUT_TRUNCATED: "Tool output was truncated",
    # Policy
    ErrorCode.E401_SERVER_DENIED: "Server blocked by policy",
    ErrorCode.E402_TOOL_DENIED: "Tool blocked by policy",
    ErrorCode.E403_OUTPUT_LIMIT_EXCEEDED: "Output exceeds size limit",
    # Installation
    ErrorCode.E501_INSTALL_FAILED: "Installation failed",
    ErrorCode.E502_API_KEY_MISSING: "Required API key is missing",
    ErrorCode.E503_PLATFORM_NOT_SUPPORTED: "Platform not supported for this server",
    ErrorCode.E504_INSTALL_TIMEOUT: "Installation timed out",
    ErrorCode.E505_JOB_NOT_FOUND: "Installation job not found",
    ErrorCode.E506_INSTALL_CANCELLED: "Installation was cancelled",
}

# Suggestions for each error code
ERROR_SUGGESTIONS: dict[ErrorCode, str] = {
    # Configuration
    ErrorCode.E101_CONFIG_NOT_FOUND: "Create a .mcp.json file or specify --config",
    ErrorCode.E102_CONFIG_INVALID: "Check the configuration file format",
    ErrorCode.E103_CONFIG_PARSE_ERROR: "Verify JSON syntax in configuration file",
    # Connection
    ErrorCode.E201_SERVER_OFFLINE: "Check if the server is running",
    ErrorCode.E202_SERVER_TIMEOUT: "Increase timeout or check server health",
    ErrorCode.E203_SERVER_NOT_FOUND: "Verify server name in configuration",
    ErrorCode.E204_CONNECTION_REFUSED: "Check server is running and accessible",
    ErrorCode.E205_SERVER_PROCESS_DIED: "Check server logs for errors",
    # Tool invocation
    ErrorCode.E301_TOOL_NOT_FOUND: "Use gateway.catalog_search to find available tools",
    ErrorCode.E302_TOOL_EXECUTION_FAILED: "Check tool arguments and server status",
    ErrorCode.E303_TOOL_TIMEOUT: "Increase timeout in options or simplify request",
    ErrorCode.E304_INVALID_ARGUMENTS: "Use gateway.describe to see required arguments",
    ErrorCode.E305_OUTPUT_TRUNCATED: "Increase max_output_chars in options",
    # Policy
    ErrorCode.E401_SERVER_DENIED: "Update policy file to allow this server",
    ErrorCode.E402_TOOL_DENIED: "Update policy file to allow this tool",
    ErrorCode.E403_OUTPUT_LIMIT_EXCEEDED: "Increase limits in policy or filter results",
    # Installation
    ErrorCode.E501_INSTALL_FAILED: "Check installation logs for details",
    ErrorCode.E502_API_KEY_MISSING: "Set the required environment variable in .env",
    ErrorCode.E503_PLATFORM_NOT_SUPPORTED: "Check manifest for supported platforms",
    ErrorCode.E504_INSTALL_TIMEOUT: "Retry installation or check network connection",
    ErrorCode.E505_JOB_NOT_FOUND: "Job may have expired, start a new installation",
    ErrorCode.E506_INSTALL_CANCELLED: "Restart installation if needed",
}

# Which errors are retryable
RETRYABLE_ERRORS: set[ErrorCode] = {
    ErrorCode.E201_SERVER_OFFLINE,
    ErrorCode.E202_SERVER_TIMEOUT,
    ErrorCode.E204_CONNECTION_REFUSED,
    ErrorCode.E303_TOOL_TIMEOUT,
    ErrorCode.E504_INSTALL_TIMEOUT,
}


class GatewayError(BaseModel):
    """Structured error with code, message, and context."""

    code: str = Field(description="Error code (e.g., 'E301')")
    message: str = Field(description="Human-readable error message")
    details: dict[str, Any] | None = Field(
        default=None, description="Additional error context"
    )
    suggestion: str | None = Field(default=None, description="How to resolve the error")
    retryable: bool = Field(
        default=False, description="Whether the operation can be retried"
    )


class GatewayException(Exception):
    """Exception with structured error information."""

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        suggestion: str | None = None,
    ) -> None:
        self.code = code
        self.message = message or ERROR_MESSAGES.get(code, str(code))
        self.details = details
        self.suggestion = suggestion or ERROR_SUGGESTIONS.get(code)
        self.retryable = code in RETRYABLE_ERRORS
        super().__init__(self.message)

    def to_error(self) -> GatewayError:
        """Convert to GatewayError model."""
        return GatewayError(
            code=self.code.value,
            message=self.message,
            details=self.details,
            suggestion=self.suggestion,
            retryable=self.retryable,
        )


def make_error(
    code: ErrorCode,
    message: str | None = None,
    **details: Any,
) -> GatewayError:
    """Create a GatewayError with the given code and details.

    Args:
        code: Error code enum value
        message: Optional custom message (defaults to standard message for code)
        **details: Additional context to include in details dict

    Returns:
        GatewayError instance
    """
    return GatewayError(
        code=code.value,
        message=message or ERROR_MESSAGES.get(code, str(code)),
        details=details if details else None,
        suggestion=ERROR_SUGGESTIONS.get(code),
        retryable=code in RETRYABLE_ERRORS,
    )
