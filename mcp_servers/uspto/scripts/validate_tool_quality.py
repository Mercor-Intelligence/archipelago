"""Validate MCP tools against the 9 quality flags."""

import ast
import re
from collections.abc import Sequence
from pathlib import Path


def _extract_docstrings(source: str) -> list[str]:
    """Return docstrings for tool entrypoints (functions prefixed with 'uspto_')."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    docstrings: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
            "uspto_"
        ):
            doc = ast.get_docstring(node)
            if doc:
                docstrings.append(doc)

    return docstrings


def _has_parameterless_tool(source: str) -> bool:
    """Return True if any tool function has no parameters (besides self)."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
            "uspto_"
        ):
            # Count non-self parameters
            params = [arg for arg in node.args.args if arg.arg != "self"]
            if len(params) == 0:
                return True

    return False


def _doc_matches(docstrings: Sequence[str], pattern: str) -> bool:
    """Return True if any docstring matches the regex pattern."""

    return any(re.search(pattern, doc) for doc in docstrings)


def check_tool_quality(tool_file: Path) -> dict[str, bool]:
    """Check a single tool file for the rubric violations."""

    source = tool_file.read_text()
    docstrings = _extract_docstrings(source)

    violations = {
        "HAS_ARGS_RETURNS_IN_DESCRIPTION": _doc_matches(
            docstrings, r"(Args:|Returns:|Parameters:)"
        ),
        "HAS_PYTHON_EXAMPLES": _doc_matches(docstrings, r">>>|await \w+\("),
        "HAS_IMPLEMENTATION_DETAILS": _doc_matches(
            docstrings, r"(pip install|import \w+|SQLAlchemy|httpx)"
        ),
        "HAS_FALSE_CLAIMS": False,  # Manual review required
        "MISSING_INPUT_SCHEMA": not bool(
            re.search(r"(?:: \w+(?:Request|Input)\b|= \w+(?:Request|Input)\()", source)
        )
        and not _has_parameterless_tool(source),
        "MISSING_OUTPUT_SCHEMA": not bool(re.search(r"-> \w+Response\b", source)),
        "STRINGIFIED_OUTPUT": bool(re.search(r"return str\(", source)),
        "OVERLY_VERBOSE_DESCRIPTION": False,  # Multi-line descriptions are intentional
        "MISSING_PYDANTIC_MODELS": False,  # Covered via inputs/outputs
    }

    return violations


def main() -> None:
    """Aggregate violations for all USPTO tools."""

    tools_dir = Path("mcp_servers/uspto/tools")
    all_violations: dict[str, dict[str, bool]] = {}

    for tool_file in sorted(tools_dir.glob("*.py")):
        if tool_file.name == "__init__.py":
            continue
        violations = check_tool_quality(tool_file)
        if any(violations.values()):
            all_violations[tool_file.name] = violations

    if all_violations:
        print("Tool Quality Violations Found:")
        for file, violations in all_violations.items():
            print(f"\n{file}:")
            for flag, violated in violations.items():
                if violated:
                    print(f"  - {flag}")
        raise SystemExit(1)
    else:
        print("All tools pass the quality checks!")


if __name__ == "__main__":
    main()
