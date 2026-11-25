"""Configuration interface for MCP servers.

Provides a composable pattern for configuring MCP servers with multiple
configuration sources (logging, auth, middleware, etc.).

Usage:
    from mcp_middleware import LoggingConfigurator, apply_configurations

    parser = argparse.ArgumentParser()
    args, remaining = apply_configurations(
        parser,
        mcp,
        configurators=[LoggingConfigurator()]
    )
"""

import argparse
from typing import Any, Protocol

from fastmcp import FastMCP


class Configurator(Protocol):
    """Protocol for MCP server configurators.

    Configurators follow a two-phase setup:
    1. setup(parser) - Add arguments to the argument parser
    2. configure(mcp, **kwargs) - Configure the server based on parsed arguments
    """

    def setup(self, parser: argparse.ArgumentParser) -> None:
        """Add configuration arguments to the parser.

        Args:
            parser: ArgumentParser to add arguments to
        """
        ...

    def configure(self, mcp: FastMCP, **kwargs: Any) -> None:
        """Configure the MCP server based on parsed arguments.

        Args:
            mcp: FastMCP server instance to configure
            **kwargs: Parsed command-line arguments
        """
        ...


def apply_configurations(
    parser: argparse.ArgumentParser,
    mcp: FastMCP,
    configurators: list[Configurator],
) -> tuple[argparse.Namespace, list[str]]:
    """Apply a list of configurators to an MCP server.

    This utility function handles the two-phase configuration process:
    1. Call setup() on each configurator to add arguments to the parser
    2. Parse the arguments
    3. Call configure() on each configurator with the parsed arguments

    Args:
        parser: ArgumentParser instance
        mcp: FastMCP server instance to configure
        configurators: List of configurator instances

    Returns:
        Tuple of (parsed args, remaining args for FastMCP)

    Example:
        parser = argparse.ArgumentParser()
        args, remaining = apply_configurations(
            parser,
            mcp,
            configurators=[LoggingConfigurator(), AuthConfigurator()]
        )
        sys.argv = [sys.argv[0]] + remaining
        mcp.run()
    """
    # Phase 1: Setup - add arguments to parser
    for configurator in configurators:
        configurator.setup(parser)

    # Parse arguments
    args, remaining = parser.parse_known_args()

    # Phase 2: Configure - apply configuration to MCP server
    args_dict = vars(args)
    for configurator in configurators:
        configurator.configure(mcp, **args_dict)

    return args, remaining
