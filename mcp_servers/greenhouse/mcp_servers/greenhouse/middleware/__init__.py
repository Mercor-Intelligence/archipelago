"""Middleware package for Greenhouse MCP server."""

from .auth import setup_auth

__all__ = ["setup_auth"]
