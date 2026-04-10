#!/usr/bin/env python3
"""Validate MCP tool definitions for UI generation compatibility.

This script validates that MCP server tool definitions meet the requirements
expected by mcp-ui-gen for automatic UI generation. It checks:

1. Tool function signatures (single Pydantic parameter, type annotations)
2. Pydantic model definitions (Field() with descriptions, proper types)
3. Model organization (models.py exports)
4. Naming conventions (Input/Output suffixes)

Usage:
    python scripts/validate_mcp_tools.py mcp_servers/greenhouse
    python scripts/validate_mcp_tools.py mcp_servers/ --strict
    python scripts/validate_mcp_tools.py mcp_servers/greenhouse --errors-only
"""

import argparse
import ast
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from logging_config import get_logger

logger = get_logger(__name__)


class Severity(Enum):
    """Violation severity levels."""

    ERROR = "ERROR"
    WARNING = "WARNING"


@dataclass
class Violation:
    """A single validation violation."""

    rule_id: str
    severity: Severity
    file_path: Path
    line: int
    message: str
    context: str | None = None

    def __str__(self) -> str:
        severity_str = self.severity.value
        location = f"{self.file_path}:{self.line}"
        result = f"{severity_str} [{self.rule_id}] {location}\n  {self.message}"
        if self.context:
            result += f"\n  Context: {self.context}"
        return result


@dataclass
class ValidationResult:
    """Result of validation across all files."""

    violations: list[Violation] = field(default_factory=list)
    files_checked: int = 0
    tools_found: int = 0
    models_found: int = 0

    def add(self, violation: Violation) -> None:
        """Add a violation to the result."""
        self.violations.append(violation)

    def merge(self, other: "ValidationResult") -> None:
        """Merge another result into this one."""
        self.violations.extend(other.violations)
        self.files_checked += other.files_checked
        self.tools_found += other.tools_found
        self.models_found += other.models_found

    @property
    def errors(self) -> list[Violation]:
        """Get all error-level violations."""
        return [v for v in self.violations if v.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[Violation]:
        """Get all warning-level violations."""
        return [v for v in self.violations if v.severity == Severity.WARNING]

    @property
    def has_errors(self) -> bool:
        """Check if there are any error-level violations."""
        return len(self.errors) > 0

    @property
    def has_warnings(self) -> bool:
        """Check if there are any warning-level violations."""
        return len(self.warnings) > 0


# =============================================================================
# AST Helper Functions
# =============================================================================


def get_annotation_name(node: ast.expr) -> str:
    """Extract the name from a type annotation AST node."""
    if isinstance(node, ast.Name):
        return node.id
    elif isinstance(node, ast.Attribute):
        return node.attr
    elif isinstance(node, ast.Subscript):
        # Handle generics like Optional[X], List[X]
        return get_annotation_name(node.value)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        # Handle X | Y union syntax
        return get_annotation_name(node.left)
    elif isinstance(node, ast.Constant):
        # Handle string annotations
        return str(node.value)
    return ""


def is_pydantic_base(bases: list[ast.expr]) -> bool:
    """Check if any base class is BaseModel."""
    for base in bases:
        name = get_annotation_name(base)
        if name == "BaseModel":
            return True
    return False


def has_mcp_tool_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if a function has an MCP tool decorator."""
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Call):
            if isinstance(decorator.func, ast.Attribute):
                if decorator.func.attr == "tool":
                    return True
            elif isinstance(decorator.func, ast.Name):
                # Handle @require_scopes or other decorators that wrap tools
                pass
        elif isinstance(decorator, ast.Attribute):
            if decorator.attr == "tool":
                return True
    return False


def get_function_param_count(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Get the number of non-self/cls parameters in a function."""
    params = node.args.args + node.args.posonlyargs + node.args.kwonlyargs
    count = len(params)
    # Exclude self/cls
    if params and params[0].arg in ("self", "cls"):
        count -= 1
    return count


def get_first_param(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> ast.arg | None:
    """Get the first non-self/cls parameter."""
    params = node.args.args + node.args.posonlyargs
    for param in params:
        if param.arg not in ("self", "cls"):
            return param
    return None


def extract_field_calls(class_body: list[ast.stmt]) -> list[tuple[str, ast.AnnAssign, int]]:
    """Extract all Field() assignments from a class body.

    Returns list of (field_name, node, line_number) tuples.
    """
    fields = []
    for stmt in class_body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            fields.append((stmt.target.id, stmt, stmt.lineno))
    return fields


def is_field_call(node: ast.expr | None) -> bool:
    """Check if a node is a Field() call."""
    if node is None:
        return False
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == "Field":
            return True
    return False


def field_has_description(node: ast.Call) -> bool:
    """Check if a Field() call has a description parameter."""
    for keyword in node.keywords:
        if keyword.arg == "description":
            return True
    return False


def field_has_ellipsis(node: ast.Call) -> bool:
    """Check if a Field() call has ... as first positional arg (required)."""
    if node.args:
        first_arg = node.args[0]
        if isinstance(first_arg, ast.Constant) and first_arg.value is ...:
            return True
    return False


def field_has_default(node: ast.Call) -> bool:
    """Check if a Field() call has a default or default_factory."""
    for keyword in node.keywords:
        if keyword.arg in ("default", "default_factory"):
            return True
    # Also check first positional arg for a default value
    if node.args:
        first_arg = node.args[0]
        # Ellipsis means required, not a default
        if not (isinstance(first_arg, ast.Constant) and first_arg.value is ...):
            return True
    return False


def is_optional_type(annotation: ast.expr | None) -> bool:
    """Check if a type annotation is Optional or has | None."""
    if annotation is None:
        return False

    # Check for X | None syntax
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        # Check if either side is None
        if isinstance(annotation.right, ast.Constant) and annotation.right.value is None:
            return True
        if isinstance(annotation.left, ast.Constant) and annotation.left.value is None:
            return True

    # Check for Optional[X] syntax
    if isinstance(annotation, ast.Subscript):
        if isinstance(annotation.value, ast.Name) and annotation.value.id == "Optional":
            return True

    return False


def annotation_contains_any(annotation: ast.expr | None) -> bool:
    """Check if a type annotation contains 'Any'."""
    if annotation is None:
        return False

    if isinstance(annotation, ast.Name) and annotation.id == "Any":
        return True

    if isinstance(annotation, ast.Subscript):
        if annotation_contains_any(annotation.value):
            return True
        if isinstance(annotation.slice, ast.Tuple):
            for elt in annotation.slice.elts:
                if annotation_contains_any(elt):
                    return True
        else:
            if annotation_contains_any(annotation.slice):
                return True

    if isinstance(annotation, ast.BinOp):
        return annotation_contains_any(annotation.left) or annotation_contains_any(annotation.right)

    return False


def is_bare_dict(annotation: ast.expr | None) -> bool:
    """Check if a type annotation is a bare 'dict' without type parameters."""
    if annotation is None:
        return False
    if isinstance(annotation, ast.Name) and annotation.id == "dict":
        return True
    return False


# =============================================================================
# Validators
# =============================================================================


class ToolSignatureValidator:
    """Validates tool function signatures (MCP001-MCP006)."""

    def __init__(self, file_path: Path):
        self.file_path = file_path
        self._registered_tool_names: set[str] = set()

    def validate(self, tree: ast.AST) -> ValidationResult:
        """Validate all tool functions in the AST."""
        result = ValidationResult()

        # First pass: find all functions registered via mcp.tool()(func)
        self._find_registered_tools(tree)

        # Second pass: validate tool functions
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                if self._is_tool_function(node):
                    result.tools_found += 1
                    self._validate_tool_function(node, result)

        return result

    def _find_registered_tools(self, tree: ast.AST) -> None:
        """Find functions registered via mcp.tool()(func) pattern."""
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                call = node.value
                # Look for mcp.tool()(func) pattern
                # The outer call has the function as argument
                if call.args:
                    func_arg = call.args[0]
                    if isinstance(func_arg, ast.Name):
                        # Check if it's mcp.tool()(func_name)
                        if isinstance(call.func, ast.Call):
                            inner_call = call.func
                            if self._is_mcp_tool_call_node(inner_call):
                                self._registered_tool_names.add(func_arg.id)

    def _is_mcp_tool_call_node(self, node: ast.Call) -> bool:
        """Check if a Call node is mcp.tool(), server.tool(), etc."""
        if isinstance(node.func, ast.Attribute):
            return (
                node.func.attr == "tool"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in ("mcp", "server", "app")
            )
        return False

    def _is_tool_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        """Determine if a function is an MCP tool."""
        # Check for @mcp.tool(), @server.tool(), @app.tool() decorators
        if has_mcp_tool_decorator(node):
            return True

        # Check if function is registered via mcp.tool()(func)
        if node.name in self._registered_tool_names:
            return True

        return False

    def _validate_tool_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, result: ValidationResult
    ) -> None:
        """Validate a single tool function."""
        func_name = node.name

        # MCP001: Must have exactly 1 parameter
        param_count = get_function_param_count(node)
        if param_count != 1:
            msg = f"Tool function '{func_name}' must have exactly 1 parameter (has {param_count})"
            result.add(
                Violation(
                    rule_id="MCP001",
                    severity=Severity.ERROR,
                    file_path=self.file_path,
                    line=node.lineno,
                    message=msg,
                )
            )
            return  # Can't validate further without the param

        first_param = get_first_param(node)
        if first_param is None:
            return

        # MCP002: Parameter must have type annotation
        if first_param.annotation is None:
            result.add(
                Violation(
                    rule_id="MCP002",
                    severity=Severity.ERROR,
                    file_path=self.file_path,
                    line=node.lineno,
                    message=f"Tool function '{func_name}' parameter must have a type annotation",
                )
            )

        # MCP003: Type must be a Pydantic BaseModel (checked via naming convention)
        # Full validation would require dynamic import; here we check naming
        if first_param.annotation:
            type_name = get_annotation_name(first_param.annotation)
            if type_name and not type_name.endswith("Input") and type_name != "BaseModel":
                # This is a weak heuristic - could be improved with dynamic import
                pass  # We'll catch this with MCP031

        # MCP004: Should have return type annotation
        if node.returns is None:
            result.add(
                Violation(
                    rule_id="MCP004",
                    severity=Severity.WARNING,
                    file_path=self.file_path,
                    line=node.lineno,
                    message=f"Tool function '{func_name}' should have a return type annotation",
                )
            )
        else:
            # MCP005: Return type should be Pydantic BaseModel
            return_type = get_annotation_name(node.returns)
            if (
                return_type
                and not return_type.endswith("Output")
                and return_type
                not in (
                    "BaseModel",
                    "None",
                )
            ):
                # Weak heuristic - caught better by MCP032
                pass

        # MCP006: Should have docstring
        docstring = ast.get_docstring(node)
        if not docstring:
            msg = f"Tool function '{func_name}' should have a docstring (becomes tool description)"
            result.add(
                Violation(
                    rule_id="MCP006",
                    severity=Severity.WARNING,
                    file_path=self.file_path,
                    line=node.lineno,
                    message=msg,
                )
            )


class PydanticModelValidator:
    """Validates Pydantic model definitions (MCP010-MCP014)."""

    def __init__(self, file_path: Path):
        self.file_path = file_path

    def validate(self, tree: ast.AST) -> ValidationResult:
        """Validate all Pydantic models in the AST."""
        result = ValidationResult()

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if is_pydantic_base(node.bases):
                    result.models_found += 1
                    self._validate_model(node, result)

        return result

    def _validate_model(self, node: ast.ClassDef, result: ValidationResult) -> None:
        """Validate a single Pydantic model class."""
        model_name = node.name
        fields = extract_field_calls(node.body)

        # Input models need strict validation (they generate UI forms)
        # Output models are less strict (just serialization)
        is_input_model = model_name.endswith("Input")

        for field_name, field_node, line in fields:
            # Skip private fields
            if field_name.startswith("_"):
                continue

            annotation = field_node.annotation
            value = field_node.value
            is_optional = is_optional_type(annotation)

            # MCP010: Fields must have Field() with description
            # ERROR for Input models, WARNING for Output models
            severity = Severity.ERROR if is_input_model else Severity.WARNING
            if is_field_call(value):
                if not field_has_description(value):
                    msg = f"Field '{field_name}' in {model_name} must have 'description' in Field()"
                    result.add(
                        Violation(
                            rule_id="MCP010",
                            severity=severity,
                            file_path=self.file_path,
                            line=line,
                            message=msg,
                        )
                    )
            else:
                # No Field() call at all - only report for Input models
                if is_input_model:
                    msg = (
                        f"Field '{field_name}' in {model_name} must use Field() with 'description'"
                    )
                    result.add(
                        Violation(
                            rule_id="MCP010",
                            severity=Severity.ERROR,
                            file_path=self.file_path,
                            line=line,
                            message=msg,
                        )
                    )

            # MCP011: Required fields should use Field(...) - only for Input models
            if is_input_model and not is_optional and is_field_call(value):
                if not field_has_ellipsis(value) and not field_has_default(value):
                    msg = (
                        f"Required field '{field_name}' in {model_name} "
                        "should use Field(...) to be explicit"
                    )
                    result.add(
                        Violation(
                            rule_id="MCP011",
                            severity=Severity.WARNING,
                            file_path=self.file_path,
                            line=line,
                            message=msg,
                        )
                    )

            # MCP012: Optional fields should have explicit default - only for Input models
            if is_input_model and is_optional and is_field_call(value):
                if not field_has_default(value):
                    msg = (
                        f"Optional field '{field_name}' in {model_name} "
                        "should have explicit default="
                    )
                    result.add(
                        Violation(
                            rule_id="MCP012",
                            severity=Severity.WARNING,
                            file_path=self.file_path,
                            line=line,
                            message=msg,
                        )
                    )

            # MCP013: Avoid Any type
            if annotation_contains_any(annotation):
                msg = (
                    f"Field '{field_name}' in {model_name} uses 'Any' - "
                    "use specific type when possible"
                )
                result.add(
                    Violation(
                        rule_id="MCP013",
                        severity=Severity.WARNING,
                        file_path=self.file_path,
                        line=line,
                        message=msg,
                    )
                )

            # MCP014: Avoid bare dict
            if is_bare_dict(annotation):
                msg = (
                    f"Field '{field_name}' in {model_name} uses bare 'dict' - "
                    "use Dict[str, X] with value type"
                )
                result.add(
                    Violation(
                        rule_id="MCP014",
                        severity=Severity.WARNING,
                        file_path=self.file_path,
                        line=line,
                        message=msg,
                    )
                )


class NamingConventionValidator:
    """Validates naming conventions (MCP031-MCP032)."""

    def __init__(self, file_path: Path):
        self.file_path = file_path

    def validate(self, tree: ast.AST) -> ValidationResult:
        """Validate naming conventions in the AST."""
        result = ValidationResult()

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if is_pydantic_base(node.bases):
                    self._validate_model_name(node, result)

        return result

    def _validate_model_name(self, node: ast.ClassDef, result: ValidationResult) -> None:
        """Validate a Pydantic model class name."""
        model_name = node.name
        docstring = ast.get_docstring(node) or ""

        # Try to infer if it's an input or output model
        # Look for hints in docstring or class purpose
        is_input_model = (
            "input" in model_name.lower()
            or "Input" in docstring
            or "API:" in docstring
            and "POST" in docstring
        )
        is_output_model = (
            "output" in model_name.lower()
            or "response" in model_name.lower()
            or "Output" in docstring
            or "Response" in docstring
        )

        # MCP031: Input models should end with Input
        if is_input_model and not model_name.endswith("Input"):
            msg = f"Model '{model_name}' appears to be an input model but doesn't end with 'Input'"
            result.add(
                Violation(
                    rule_id="MCP031",
                    severity=Severity.WARNING,
                    file_path=self.file_path,
                    line=node.lineno,
                    message=msg,
                )
            )

        # MCP032: Output models should end with Output
        if is_output_model and not model_name.endswith("Output"):
            msg = (
                f"Model '{model_name}' appears to be an output model but doesn't end with 'Output'"
            )
            result.add(
                Violation(
                    rule_id="MCP032",
                    severity=Severity.WARNING,
                    file_path=self.file_path,
                    line=node.lineno,
                    message=msg,
                )
            )


# =============================================================================
# Server Validator (Orchestrator)
# =============================================================================


class ServerValidator:
    """Orchestrates validation across an MCP server."""

    def __init__(self, server_path: Path):
        self.server_path = server_path

    def validate(self) -> ValidationResult:
        """Run all validators on the server."""
        result = ValidationResult()

        # Validate tools directory
        tools_dir = self.server_path / "tools"
        if tools_dir.exists():
            for py_file in tools_dir.glob("*.py"):
                if py_file.name.startswith("_"):
                    continue
                file_result = self._validate_file(py_file, validate_tools=True)
                result.merge(file_result)

        # Validate schemas directory
        schemas_dir = self.server_path / "schemas"
        if schemas_dir.exists():
            for py_file in schemas_dir.glob("*.py"):
                if py_file.name.startswith("_"):
                    continue
                file_result = self._validate_file(py_file, validate_models=True)
                result.merge(file_result)

        # Also check models.py directly if it exists
        models_file = self.server_path / "models.py"
        if models_file.exists():
            file_result = self._validate_file(models_file, validate_models=True)
            result.merge(file_result)

        return result

    def _validate_file(
        self,
        file_path: Path,
        validate_tools: bool = False,
        validate_models: bool = False,
    ) -> ValidationResult:
        """Validate a single Python file."""
        result = ValidationResult()
        result.files_checked += 1

        try:
            with open(file_path, encoding="utf-8") as f:
                source = f.read()
            tree = ast.parse(source, filename=str(file_path))
        except SyntaxError:
            return result

        if validate_tools:
            tool_validator = ToolSignatureValidator(file_path)
            result.merge(tool_validator.validate(tree))

        if validate_models:
            model_validator = PydanticModelValidator(file_path)
            result.merge(model_validator.validate(tree))

            naming_validator = NamingConventionValidator(file_path)
            result.merge(naming_validator.validate(tree))

        return result


# =============================================================================
# CLI
# =============================================================================


def print_violations(violations: list[Violation], show_warnings: bool = True) -> None:
    """Print violations to console."""
    for violation in violations:
        if violation.severity == Severity.WARNING and not show_warnings:
            continue
        print(violation)
        print()


def print_summary(result: ValidationResult) -> None:
    """Print summary of validation results."""
    print("─" * 60)
    error_count = len(result.errors)
    warning_count = len(result.warnings)

    print(f"Summary: {error_count} error(s), {warning_count} warning(s)")
    print(f"  Files checked: {result.files_checked}")
    print(f"  Tools found: {result.tools_found}")
    print(f"  Models found: {result.models_found}")

    # Count by rule
    rule_counts: dict[str, int] = {}
    for v in result.violations:
        rule_counts[v.rule_id] = rule_counts.get(v.rule_id, 0) + 1

    if rule_counts:
        print("\nViolations by rule:")
        for rule_id in sorted(rule_counts.keys()):
            print(f"  {rule_id}: {rule_counts[rule_id]} violation(s)")

    print("─" * 60)


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Validate MCP tool definitions for UI generation compatibility"
    )
    parser.add_argument(
        "path",
        type=Path,
        help="Path to MCP server directory or mcp_servers/ root",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as errors",
    )
    parser.add_argument(
        "--errors-only",
        action="store_true",
        help="Only show errors, hide warnings",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose output",
    )

    args = parser.parse_args()
    path: Path = args.path

    if not path.exists():
        logger.error("Path '%s' does not exist", path)
        return 1

    # Determine if we're validating one server or many
    servers_to_validate: list[Path] = []

    if path.name == "mcp_servers" or (path / "mcp_servers").exists():
        # Validate all servers
        servers_dir = path if path.name == "mcp_servers" else path / "mcp_servers"
        for server_dir in servers_dir.iterdir():
            if server_dir.is_dir() and not server_dir.name.startswith("_"):
                servers_to_validate.append(server_dir)
    elif (path / "tools").exists() or (path / "schemas").exists():
        # Single server
        servers_to_validate.append(path)
    else:
        logger.error("'%s' does not appear to be an MCP server directory", path)
        return 1

    # Validate each server
    total_result = ValidationResult()

    for server_path in sorted(servers_to_validate):
        if args.verbose:
            logger.info("Validating %s...", server_path)

        validator = ServerValidator(server_path)
        result = validator.validate()
        total_result.merge(result)

    # Print violations
    show_warnings = not args.errors_only
    violations_to_show = total_result.violations if show_warnings else total_result.errors

    if violations_to_show:
        print()
        print_violations(violations_to_show, show_warnings=show_warnings)

    # Print summary
    print_summary(total_result)

    # Determine exit code
    if args.strict:
        if total_result.has_errors or total_result.has_warnings:
            print("FAILED (strict mode: warnings treated as errors)")
            return 1
    else:
        if total_result.has_errors:
            print("FAILED (errors found)")
            return 1

    print("PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
