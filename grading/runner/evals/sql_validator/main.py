"""SQL Validator eval - validates SQL query results against expected values."""

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult


async def sql_validator_eval(input: EvalImplInput) -> VerifierResult:
    """
    Execute SQL query on snapshot database and compare with expected result.

    Validates that SQL query returns expected value.

    Port of: verifier/runner/verification/verifiers/config/sql_verifier.py
    """
    try:
        # Validate helper results
        if not input.helper_results:
            raise ValueError("Missing helper results")

        # Extract from verifier_values (per-criterion config)
        sql_query = input.verifier.verifier_values.get("sql_query")
        expected_result = input.verifier.verifier_values.get("expected_result")
        db_alias = input.verifier.verifier_values.get("db_alias", "main")

        # Validate required fields
        if not sql_query or expected_result is None:
            error_msg = "Missing required fields: sql_query or expected_result"
            raise ValueError(error_msg)

        # Get DB connections from helper
        dbs = input.helper_results[HelperIds.SNAPSHOT_DBS]

        if db_alias not in dbs:
            available = ", ".join(dbs.keys()) if dbs else "none"
            error_msg = f"Database alias '{db_alias}' not found. Available: {available}"
            raise ValueError(error_msg)

        # Execute query
        conn = dbs[db_alias]["connection"]
        cursor = conn.execute(sql_query)
        rows = cursor.fetchall()

        # Convert result to simple format
        if len(rows) == 1 and len(rows[0]) == 1:
            # Single value result (scalar)
            actual_result = rows[0][0]
        elif len(rows) == 0:
            # No rows returned
            actual_result = None
        else:
            # Multiple rows/columns - return count
            actual_result = len(rows)

        # Compare (string comparison for simplicity)
        passed = str(actual_result) == str(expected_result)

        # Convert to float for result_value
        result_value = 0.0
        if actual_result is not None:
            try:
                result_value = float(actual_result)
            except (ValueError, TypeError):
                # Fallback for complex types (shouldn't happen with above logic)
                result_value = 0.0

        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=1.0 if passed else 0.0,
            verifier_result_values={
                "query_type": "scalar",
                "result_value": result_value,
            },
        )

    except Exception as e:
        error_msg = f"SQL execution failed: {str(e)}"
        raise ValueError(error_msg) from e
