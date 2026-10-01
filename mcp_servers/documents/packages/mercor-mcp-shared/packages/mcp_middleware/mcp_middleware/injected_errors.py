"""
Error injection middleware for testing agent robustness.

This middleware intercepts MCP tool calls and injects controlled errors
based on configuration rules. It enables testing how agents handle
production-like failures (timeouts, rate limits, permission errors, etc.)
without modifying individual foundry apps.

Configuration is read from a well-known path:
    /.apps_data/{app_name}/.config/injected_errors.json

Usage:
    The middleware is automatically registered by run_server() when a
    config file exists. Apps do not need any code changes.

    For apps that need domain-specific error types beyond the base enum,
    pass extra_error_types to setup_error_injection():

        XERO_ERROR_TYPES = {"invalid_tenant", "expired_token"}
        setup_error_injection(mcp, extra_error_types=XERO_ERROR_TYPES)
"""

import fnmatch
import logging
import os
import random
from enum import StrEnum
from typing import Any

import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class InjectedErrorType(StrEnum):
    """Base error types available to all apps.

    Apps can extend this with domain-specific error types by passing
    extra_error_types to setup_error_injection().
    """

    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    PERMISSION_DENIED = "permission_denied"
    SERVER_ERROR = "server_error"
    MALFORMED_RESPONSE = "malformed_response"


class InjectedErrorRule(BaseModel):
    """A single error injection rule.

    Attributes:
        error_type: The type of error to inject (InjectedErrorType or app extension)
        tool: The tool name to match against
        probability: Likelihood of triggering (0.0-1.0, default 1.0)
        message: Custom error message (optional)
        paths: Glob patterns for path filtering on file-related tools (optional)
        max_occurrences: Maximum times this rule can fire (optional)
        metadata: Arbitrary extra data for custom error types (optional)
    """

    error_type: str
    tool: str
    probability: float = Field(default=1.0, ge=0.0, le=1.0)
    message: str | None = None
    paths: list[str] | None = None
    max_occurrences: int | None = None
    metadata: dict[str, Any] | None = None


class InjectedErrorsConfig(BaseModel):
    """Error injection configuration for a task.

    Attributes:
        error_rules: List of error injection rules to apply
        seed: Optional seed for deterministic probability resolution
    """

    error_rules: list[InjectedErrorRule] = []
    seed: int | None = None


class ErrorInjectionMiddleware(Middleware):
    """FastMCP middleware that intercepts tool calls and injects errors.

    This middleware checks each tool call against configured rules and
    raises ToolError when a rule matches. From the agent's perspective,
    injected errors are indistinguishable from real ones.

    Args:
        config: The error injection configuration
        extra_error_types: App-specific error types beyond the base enum
    """

    def __init__(
        self,
        config: InjectedErrorsConfig,
        extra_error_types: set[str] | None = None,
    ):
        self.config = config
        self.rng = random.Random(config.seed)
        self.occurrence_counts: dict[int, int] = {}
        # Build set of valid error types from base enum + app-specific extensions
        self.valid_error_types = {e.value for e in InjectedErrorType}
        if extra_error_types:
            self.valid_error_types |= extra_error_types

        # Validate all rules have known error types
        for rule in config.error_rules:
            if rule.error_type not in self.valid_error_types:
                raise ValueError(
                    f"Unknown error_type '{rule.error_type}' in rule for tool "
                    f"'{rule.tool}'. Valid types: {sorted(self.valid_error_types)}"
                )

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Intercept tool calls and inject errors when rules match.

        Args:
            context: The middleware context containing request information
            call_next: Callable to invoke the next middleware or handler

        Returns:
            The response from the downstream handler

        Raises:
            ToolError: When an error injection rule matches
        """
        tool_name = context.message.name
        arguments = context.message.arguments or {}

        rule = self._check(tool_name, arguments)
        if rule is not None:
            message = rule.message or "Error"
            # Structured logging with metadata for trajectory capture
            logger.info(
                f"Injecting {rule.error_type} error on tool '{tool_name}'",
                extra={
                    "error_type": rule.error_type,
                    "tool": tool_name,
                    "injected": True,
                },
            )
            raise ToolError(message)

        return await call_next(context)

    def _check(self, tool_name: str, arguments: dict) -> InjectedErrorRule | None:
        """Check if any rule matches the current tool call.

        Uses first-match-wins semantics: once a rule matches by tool name and
        paths, it either fires (based on probability) or returns None. Later
        rules are not evaluated.

        Args:
            tool_name: The name of the tool being called
            arguments: The arguments passed to the tool

        Returns:
            The matching rule, or None if no rule matches or probability fails
        """
        for i, rule in enumerate(self.config.error_rules):
            if rule.tool != tool_name:
                continue
            if rule.paths and not self._matches_path(rule.paths, arguments):
                continue
            # Rule matches by tool name and paths - this is the first match.
            # Check max_occurrences before probability (exhausted rules don't block)
            if (
                rule.max_occurrences is not None
                and self.occurrence_counts.get(i, 0) >= rule.max_occurrences
            ):
                # Rule exhausted, continue to next rule
                continue
            # First-match-wins: either fire based on probability or return None.
            # Do not fall through to subsequent rules.
            if self.rng.random() < rule.probability:
                self.occurrence_counts[i] = self.occurrence_counts.get(i, 0) + 1
                return rule
            # Probability check failed for matching rule - return None, don't check other rules
            return None
        return None

    def _matches_path(self, patterns: list[str], arguments: dict) -> bool:
        """Check if any string argument matches the glob patterns.

        This enables path-based filtering for file-related tools. It inspects
        all string values in the arguments dict and matches against the
        configured glob patterns.

        Args:
            patterns: Glob patterns to match against
            arguments: Tool arguments to inspect

        Returns:
            True if any string argument matches any pattern
        """
        for value in arguments.values():
            if isinstance(value, str):
                for pattern in patterns:
                    if fnmatch.fnmatch(value, pattern):
                        return True
        return False


def setup_error_injection(
    mcp,
    apps_data_root: str = "/.apps_data",
    extra_error_types: set[str] | None = None,
) -> bool:
    """Register error injection middleware if a config file exists.

    Called by run_server(). This function checks for a configuration file
    at the well-known path and registers the ErrorInjectionMiddleware if
    the file exists and contains rules.

    Args:
        mcp: The FastMCP server instance
        apps_data_root: Root directory for app data (default: /.apps_data)
        extra_error_types: App-specific error types beyond the base
            InjectedErrorType enum. Apps pass these when extending
            the enum for domain-specific failures.

    Returns:
        True if middleware was registered, False otherwise
    """
    config_path = os.path.join(apps_data_root, mcp.name, ".config", "injected_errors.json")
    if not os.path.exists(config_path):
        return False

    with open(config_path, encoding="utf-8") as f:
        config = InjectedErrorsConfig.model_validate_json(f.read())

    if not config.error_rules:
        return False

    logger.info(f"Error injection: {len(config.error_rules)} rule(s), seed={config.seed}")
    mcp.add_middleware(ErrorInjectionMiddleware(config, extra_error_types=extra_error_types))
    return True
