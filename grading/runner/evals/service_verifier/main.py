"""Service-defined SQL verifier that executes checks from ArCo config.

This verifier executes SQL queries defined in arco.toml [arco.verifiers] sections,
using parameterized queries and comparing results based on the specified result_type.
"""

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus

# Valid result_type values for _compare function
VALID_RESULT_TYPES = frozenset({"string", "integer", "decimal", "boolean", "exists"})


def _config_error(
    verifier_id: str, verifier_version: int, message: str
) -> VerifierResult:
    """Return a result for configuration errors (status=ERROR, score=0.0)."""
    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        score=0.0,
        verifier_result_values={"error": message},
        status=VerifierResultStatus.ERROR,
        message=message,
    )


def _compare(actual: Any, expected: Any, result_type: str) -> bool:
    """Compare actual vs expected values based on result_type.

    Args:
        actual: The value returned from the SQL query
        expected: The expected value from verifier_values
        result_type: One of "string", "integer", "decimal", "boolean", "exists"

    Returns:
        True if values match according to the comparison type
    """
    if result_type == "exists":
        # For exists, expected should be "true" or "false" (string)
        # actual is whether any rows were returned
        expect_exists = str(expected).lower() in ("true", "1", "yes", "t")
        return bool(actual) == expect_exists

    if result_type == "boolean":
        # None means no data returned, which is distinct from a false value
        if actual is None:
            return False
        # Normalize both to boolean
        actual_bool = str(actual).lower() in ("true", "1", "yes", "t")
        expected_bool = str(expected).lower() in ("true", "1", "yes", "t")
        return actual_bool == expected_bool

    if result_type == "integer":
        try:
            return int(actual) == int(expected)
        except (ValueError, TypeError):
            return False

    if result_type == "decimal":
        try:
            return Decimal(str(actual)) == Decimal(str(expected))
        except InvalidOperation:
            return False

    # Default: string comparison
    # None means no data returned, which should fail the comparison
    if actual is None:
        return False
    return str(actual) == str(expected)


def _normalize_query_params(query: str) -> str:
    """Convert ${param_name} style placeholders to :param_name for SQLite.

    SQLite's native parameterized queries use :param_name syntax.
    This function normalizes ${param_name} style to that format.
    """
    return re.sub(r"\$\{(\w+)\}", r":\1", query)


def _extract_check_params(
    verifier_values: dict[str, Any],
    check_name: str,
) -> dict[str, Any]:
    """Extract parameters for a specific check from verifier_values.

    Field IDs follow the pattern svc__{check_name}__{param_name}.
    The 'expected' parameter is excluded as it's used for result comparison,
    not SQL substitution.
    """
    prefix = f"svc__{check_name}__"
    params = {}
    for field_id, value in verifier_values.items():
        if field_id.startswith(prefix):
            param_name = field_id[len(prefix) :]
            # Exclude 'expected' - it's used for comparison, not substitution
            if param_name != "expected":
                params[param_name] = value
    return params


async def service_verifier_eval(input: EvalImplInput) -> VerifierResult:
    """Execute service-defined SQL verifier checks.

    Reads check definitions from eval_config.eval_config_values["checks"],
    extracts parameters from verifier.verifier_values, executes the SQL
    queries using parameterized queries, and compares results based on result_type.
    """
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version

    # Get check definitions from eval_config
    checks = input.eval_config.eval_config_values.get("checks", [])
    if not checks:
        return _config_error(
            verifier_id,
            verifier_version,
            "No checks defined in eval_config.eval_config_values['checks']",
        )

    # Get helper results
    if not input.helper_results:
        return _config_error(
            verifier_id,
            verifier_version,
            "Missing helper results (SNAPSHOT_DBS required)",
        )

    dbs = input.helper_results.get(HelperIds.SNAPSHOT_DBS)
    if not dbs:
        return _config_error(
            verifier_id,
            verifier_version,
            "SNAPSHOT_DBS helper result is empty",
        )

    # Get default db_alias from eval_config
    default_db_alias = input.eval_config.eval_config_values.get("db_alias", "main")

    # Execute all checks and aggregate results
    check_results: list[dict[str, Any]] = []
    all_passed = True

    for check in checks:
        check_name = check.get("name")
        query = check.get("query")
        result_type = check.get("result_type", "string")
        db_alias = check.get("db_alias") or default_db_alias

        if not check_name or not query:
            check_results.append(
                {
                    "check_name": check_name or "unknown",
                    "error": "Missing name or query in check definition",
                    "passed": False,
                }
            )
            all_passed = False
            continue

        # Validate result_type to prevent silent fallthrough to string comparison
        if result_type not in VALID_RESULT_TYPES:
            check_results.append(
                {
                    "check_name": check_name,
                    "error": f"Invalid result_type '{result_type}'. Must be one of: {', '.join(sorted(VALID_RESULT_TYPES))}",
                    "passed": False,
                }
            )
            all_passed = False
            continue

        # Validate database alias
        if db_alias not in dbs:
            available = ", ".join(dbs.keys()) if dbs else "none"
            check_results.append(
                {
                    "check_name": check_name,
                    "error": f"Database alias '{db_alias}' not found. Available: {available}",
                    "passed": False,
                }
            )
            all_passed = False
            continue

        # Extract parameters and normalize query for SQLite parameterized queries
        params = _extract_check_params(input.verifier.verifier_values, check_name)
        normalized_query = _normalize_query_params(query)

        # Execute query using parameterized queries (prevents SQL injection)
        try:
            conn = dbs[db_alias]["connection"]
            cursor = conn.execute(normalized_query, params)
            rows = cursor.fetchall()

            # Determine actual result based on result_type
            if result_type == "exists":
                actual = len(rows) > 0
            elif len(rows) == 1 and len(rows[0]) == 1:
                actual = rows[0][0]
            elif len(rows) == 0:
                actual = None
            else:
                actual = len(rows)

        except Exception as e:
            check_results.append(
                {
                    "check_name": check_name,
                    "error": f"SQL execution failed: {e}",
                    # Log template only, not interpolated query (avoid leaking param values)
                    "query_template": query,
                    "passed": False,
                }
            )
            all_passed = False
            continue

        # Get expected value from verifier_values
        expected_field_id = f"svc__{check_name}__expected"
        expected = input.verifier.verifier_values.get(expected_field_id)

        if expected is None:
            # If no expected value, treat as "exists" check defaulting to True
            if result_type == "exists":
                expected = True
            else:
                check_results.append(
                    {
                        "check_name": check_name,
                        "error": f"Missing expected value for field '{expected_field_id}'",
                        "actual": str(actual),
                        "passed": False,
                    }
                )
                all_passed = False
                continue

        # Compare result
        passed = _compare(actual, expected, result_type)

        check_results.append(
            {
                "check_name": check_name,
                "result_type": result_type,
                "expected": str(expected),
                "actual": str(actual),
                "passed": passed,
            }
        )

        if not passed:
            all_passed = False

    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        score=1.0 if all_passed else 0.0,
        verifier_result_values={
            "checks": check_results,
            "all_passed": all_passed,
        },
        status=VerifierResultStatus.OK,
        message="" if all_passed else "One or more checks failed",
    )
