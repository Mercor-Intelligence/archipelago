"""Schema flattening utilities for MCP servers."""

from .schema import FlatBaseModel, GeminiBaseModel, OutputBaseModel, flatten_schema

__all__ = ["FlatBaseModel", "GeminiBaseModel", "OutputBaseModel", "flatten_schema"]
