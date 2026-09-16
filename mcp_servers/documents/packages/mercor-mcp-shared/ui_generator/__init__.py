"""
MCP UI Generator - Auto-generate web UIs for MCP servers.
"""

__version__ = "0.1.0"

from .codegen.generator import CodeGenerator
from .converter.schema_converter import SchemaConverter
from .parser.build_spec_parser import BuildSpec, BuildSpecParser
from .scanner.mcp_runtime_scanner import MCPRuntimeScanner

__all__ = [
    "BuildSpecParser",
    "BuildSpec",
    "MCPRuntimeScanner",
    "SchemaConverter",
    "CodeGenerator",
]
