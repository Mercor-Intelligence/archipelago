"""ADP Calculation Verifier - Validates calculated values from ADP Payroll data.

This verifier performs specific calculations on the ADP Payroll data:
- Gross to Net: gross_wages - employee_taxes - employee_benefits - other_deductions = net_pay
- Tax Totals: Sum of employee and employer taxes
- Labor Cost: gross_wages + employer_taxes + employer_benefits
- Period Variance: Compare values between payroll periods

All queries verified against actual SQLAlchemy schema in mercor-adp/mcp_servers/adp_payroll/db/models/
"""

from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus

# Calculation type definitions with their SQL queries
CALCULATION_TYPES: dict[str, dict[str, Any]] = {
    "Gross to Net Validation": {
        "description": "Verify gross_wages - employee_taxes - employee_benefits - other_deductions = net_pay",
        "components": {
            "gross_wages": {
                "query": "SELECT gross_wages FROM payroll_aggregates WHERE run_id = :run_id",
                "sign": 1,
            },
            "employee_taxes": {
                "query": """
                    SELECT COALESCE(SUM(ta.employee_amount), 0)
                    FROM tax_aggregates ta
                    JOIN payroll_aggregates pa ON ta.aggregate_id = pa.id
                    WHERE pa.run_id = :run_id
                """,
                "sign": -1,
            },
            "employee_benefits": {
                "query": """
                    SELECT COALESCE(SUM(ba.employee_amount), 0)
                    FROM benefit_aggregates ba
                    JOIN payroll_aggregates pa ON ba.aggregate_id = pa.id
                    WHERE pa.run_id = :run_id
                """,
                "sign": -1,
            },
            "other_deductions": {
                "query": """
                    SELECT COALESCE(SUM(da.amount), 0)
                    FROM deduction_aggregates da
                    JOIN payroll_aggregates pa ON da.aggregate_id = pa.id
                    WHERE pa.run_id = :run_id
                """,
                "sign": -1,
            },
            "net_pay": {
                "query": "SELECT net_pay FROM payroll_aggregates WHERE run_id = :run_id",
                "sign": 0,  # This is the expected result
            },
        },
        "required_params": ["run_id"],
        "calculation": "gross - taxes - benefits - deductions = net",
    },
    "Total Employee Taxes": {
        "description": "Sum of all employee tax withholdings",
        "query": """
            SELECT COALESCE(SUM(ta.employee_amount), 0)
            FROM tax_aggregates ta
            JOIN payroll_aggregates pa ON ta.aggregate_id = pa.id
            WHERE pa.run_id = :run_id
        """,
        "required_params": ["run_id"],
    },
    "Total Employer Taxes": {
        "description": "Sum of all employer tax contributions",
        "query": """
            SELECT COALESCE(SUM(ta.employer_amount), 0)
            FROM tax_aggregates ta
            JOIN payroll_aggregates pa ON ta.aggregate_id = pa.id
            WHERE pa.run_id = :run_id
        """,
        "required_params": ["run_id"],
    },
    "Total Taxes Combined": {
        "description": "Sum of employee + employer taxes",
        "query": """
            SELECT COALESCE(SUM(COALESCE(ta.employee_amount, 0) + COALESCE(ta.employer_amount, 0)), 0)
            FROM tax_aggregates ta
            JOIN payroll_aggregates pa ON ta.aggregate_id = pa.id
            WHERE pa.run_id = :run_id
        """,
        "required_params": ["run_id"],
    },
    "Total Labor Cost": {
        "description": "gross_wages + employer_taxes + employer_benefits (total employer cost)",
        "components": {
            "gross_wages": {
                "query": "SELECT COALESCE(gross_wages, 0) FROM payroll_aggregates WHERE run_id = :run_id",
            },
            "employer_taxes": {
                "query": """
                    SELECT COALESCE(SUM(ta.employer_amount), 0)
                    FROM tax_aggregates ta
                    JOIN payroll_aggregates pa ON ta.aggregate_id = pa.id
                    WHERE pa.run_id = :run_id
                """,
            },
            "employer_benefits": {
                "query": """
                    SELECT COALESCE(SUM(ba.employer_amount), 0)
                    FROM benefit_aggregates ba
                    JOIN payroll_aggregates pa ON ba.aggregate_id = pa.id
                    WHERE pa.run_id = :run_id
                """,
            },
        },
        "required_params": ["run_id"],
        "calculation": "gross + employer_taxes + employer_benefits",
        "sum_components": True,
    },
    "Total Benefits Combined": {
        "description": "Sum of employee + employer benefit costs",
        "query": """
            SELECT COALESCE(SUM(COALESCE(ba.employee_amount, 0) + COALESCE(ba.employer_amount, 0)), 0)
            FROM benefit_aggregates ba
            JOIN payroll_aggregates pa ON ba.aggregate_id = pa.id
            WHERE pa.run_id = :run_id
        """,
        "required_params": ["run_id"],
    },
    "Net Pay Calculated": {
        "description": "gross_wages - all_employee_deductions (calculated, not stored)",
        "components": {
            "gross_wages": {
                "query": "SELECT COALESCE(gross_wages, 0) FROM payroll_aggregates WHERE run_id = :run_id",
            },
            "employee_taxes": {
                "query": """
                    SELECT COALESCE(SUM(ta.employee_amount), 0)
                    FROM tax_aggregates ta
                    JOIN payroll_aggregates pa ON ta.aggregate_id = pa.id
                    WHERE pa.run_id = :run_id
                """,
            },
            "employee_benefits": {
                "query": """
                    SELECT COALESCE(SUM(ba.employee_amount), 0)
                    FROM benefit_aggregates ba
                    JOIN payroll_aggregates pa ON ba.aggregate_id = pa.id
                    WHERE pa.run_id = :run_id
                """,
            },
            "other_deductions": {
                "query": """
                    SELECT COALESCE(SUM(da.amount), 0)
                    FROM deduction_aggregates da
                    JOIN payroll_aggregates pa ON da.aggregate_id = pa.id
                    WHERE pa.run_id = :run_id
                """,
            },
        },
        "required_params": ["run_id"],
        "calculation": "gross - taxes - benefits - deductions",
        "formula": lambda c: c["gross_wages"]
        - c["employee_taxes"]
        - c["employee_benefits"]
        - c["other_deductions"],
    },
    "Period Gross Wages Variance": {
        "description": "Compare gross wages between two payroll runs (variance)",
        "components": {
            "run1_gross": {
                "query": "SELECT COALESCE(gross_wages, 0) FROM payroll_aggregates WHERE run_id = :run_id",
            },
            "run2_gross": {
                "query": "SELECT COALESCE(gross_wages, 0) FROM payroll_aggregates WHERE run_id = :compare_run_id",
            },
        },
        "required_params": ["run_id", "compare_run_id"],
        "calculation": "run1 - run2 variance",
        "formula": lambda c: c["run1_gross"] - c["run2_gross"],
    },
    "Period Net Pay Variance": {
        "description": "Compare net pay between two payroll runs (variance)",
        "components": {
            "run1_net": {
                "query": "SELECT COALESCE(net_pay, 0) FROM payroll_aggregates WHERE run_id = :run_id",
            },
            "run2_net": {
                "query": "SELECT COALESCE(net_pay, 0) FROM payroll_aggregates WHERE run_id = :compare_run_id",
            },
        },
        "required_params": ["run_id", "compare_run_id"],
        "calculation": "run1 - run2 variance",
        "formula": lambda c: c["run1_net"] - c["run2_net"],
    },
    "Tax Rate Effective": {
        "description": "Calculate effective tax rate: employee_taxes / gross_wages",
        "components": {
            "gross_wages": {
                "query": "SELECT COALESCE(gross_wages, 0) FROM payroll_aggregates WHERE run_id = :run_id",
            },
            "employee_taxes": {
                "query": """
                    SELECT COALESCE(SUM(ta.employee_amount), 0)
                    FROM tax_aggregates ta
                    JOIN payroll_aggregates pa ON ta.aggregate_id = pa.id
                    WHERE pa.run_id = :run_id
                """,
            },
        },
        "required_params": ["run_id"],
        "calculation": "employee_taxes / gross_wages",
        "formula": lambda c: (
            c["employee_taxes"] / c["gross_wages"]
            if c["gross_wages"] > 0
            else Decimal(0)
        ),
    },
    "Overtime Percentage": {
        "description": "Calculate overtime as percentage of gross: overtime_pay / gross_wages",
        "components": {
            "gross_wages": {
                "query": "SELECT COALESCE(gross_wages, 0) FROM payroll_aggregates WHERE run_id = :run_id",
            },
            "overtime_pay": {
                "query": "SELECT COALESCE(overtime_pay, 0) FROM payroll_aggregates WHERE run_id = :run_id",
            },
        },
        "required_params": ["run_id"],
        "calculation": "overtime / gross",
        "formula": lambda c: (
            c["overtime_pay"] / c["gross_wages"] if c["gross_wages"] > 0 else Decimal(0)
        ),
    },
}


async def adp_calculation_eval(input: EvalImplInput) -> VerifierResult:
    """Verify calculations derived from ADP Payroll data.

    Supports:
    - Gross to Net validation
    - Tax total calculations
    - Labor cost calculations
    - Period variance comparisons
    """
    # Validate helper results available
    if not input.helper_results:
        raise ValueError("Helper results not available")

    # Get calculation type
    calculation_type_raw = input.verifier.verifier_values.get("calculation_type")
    if not calculation_type_raw:
        raise ValueError("calculation_type is required")

    # Strip arrow notation (e.g., "Total Labor Cost → Run ID" -> "Total Labor Cost")
    # The arrow indicates which fields need to be filled
    calculation_type = calculation_type_raw.split(" → ")[0].strip()

    # Get expected value
    expected_value = input.verifier.verifier_values.get("expected_value")
    if expected_value is None:
        raise ValueError("expected_value is required")

    # Get tolerance (default 0.01 for decimal comparisons)
    try:
        tolerance_value = input.verifier.verifier_values.get("tolerance", 0.01)
        if tolerance_value is None:
            tolerance_value = 0.01
        tolerance = Decimal(str(tolerance_value))
    except (ValueError, TypeError, InvalidOperation) as e:
        raise ValueError(f"Invalid tolerance value: {e!s}") from e

    # Get calculation configuration
    calc_config = CALCULATION_TYPES.get(calculation_type)
    if not calc_config:
        raise ValueError(f"Unknown calculation_type: {calculation_type}")

    # Extract required parameters
    params = {}
    for param in calc_config["required_params"]:
        value = input.verifier.verifier_values.get(param)
        if value is None:
            raise ValueError(
                f"Required parameter '{param}' is missing for {calculation_type}"
            )
        params[param] = value

    # Get database connection from snapshot_dbs helper
    dbs = input.helper_results.get(HelperIds.SNAPSHOT_DBS, {})
    if not dbs:
        raise ValueError("No databases found in snapshot")

    # Filter to ADP Payroll database only
    # Expected alias: .apps_data_adp_payroll_data (from /.apps_data/adp_payroll/data.db)
    adp_dbs = [
        (alias, db_info)
        for alias, db_info in dbs.items()
        if "adp_payroll" in alias.lower()
    ]

    if len(adp_dbs) != 1:
        raise ValueError(
            f"Expected exactly 1 ADP Payroll database (alias containing 'adp_payroll'), found {len(adp_dbs)}. Available: {list(dbs.keys())}"
        )

    _alias, db_info = adp_dbs[0]
    conn = db_info.get("connection")
    if not conn:
        raise ValueError("ADP database found but connection is None")

    try:
        cursor = conn.cursor()

        # Execute calculation
        if "query" in calc_config:
            # Simple single-query calculation
            result_row = cursor.execute(calc_config["query"], params).fetchone()
            if result_row is None:
                result = Decimal(0)
            else:
                result = (
                    Decimal(str(result_row[0]))
                    if result_row[0] is not None
                    else Decimal(0)
                )
            components_detail = {}

        elif "components" in calc_config:
            # Multi-component calculation
            components = {}
            for comp_name, comp_config in calc_config["components"].items():
                result_row = cursor.execute(comp_config["query"], params).fetchone()
                if result_row is None or result_row[0] is None:
                    components[comp_name] = Decimal(0)
                else:
                    components[comp_name] = Decimal(str(result_row[0]))

            components_detail = {k: float(v) for k, v in components.items()}

            # Handle Gross to Net Validation specially
            if calculation_type == "Gross to Net Validation":
                gross = components.get("gross_wages", Decimal(0))
                taxes = components.get("employee_taxes", Decimal(0))
                benefits = components.get("employee_benefits", Decimal(0))
                deductions = components.get("other_deductions", Decimal(0))
                net_actual = components.get("net_pay", Decimal(0))

                calculated_net = gross - taxes - benefits - deductions
                variance = calculated_net - net_actual

                # For validation, we check if the variance is within tolerance
                try:
                    expected_decimal = Decimal(str(expected_value))
                except (ValueError, TypeError, InvalidOperation) as e:
                    raise ValueError(
                        f"Invalid expected_value: {expected_value}. Error: {e!s}"
                    ) from e
                matches = abs(variance - expected_decimal) <= tolerance

                return VerifierResult(
                    verifier_id=input.verifier.verifier_id,
                    verifier_version=input.verifier.verifier_version,
                    score=1.0 if matches else 0.0,
                    status=VerifierResultStatus.OK,
                    verifier_result_values={
                        "calculation_type": calculation_type,
                        "components": components_detail,
                        "calculated_net": float(calculated_net),
                        "actual_net": float(net_actual),
                        "variance": float(variance),
                        "expected_variance": float(expected_decimal),
                        "matches": matches,
                    },
                    message=f"Gross to Net Validation: variance=${float(variance):,.2f} "
                    f"(expected=${float(expected_decimal):,.2f}) "
                    f"{'✓' if matches else '✗'}",
                )

            elif "formula" in calc_config:
                # Use custom formula
                result = calc_config["formula"](components)
            elif calc_config.get("sum_components"):
                # Sum all components
                result = sum(components.values())
            else:
                result = Decimal(0)
        else:
            raise ValueError(f"Invalid configuration for {calculation_type}")

        logger.info(
            f"ADP calculation: {calculation_type} = {result} (expected: {expected_value})"
        )

    except Exception as e:
        logger.error(f"Error executing calculation for {calculation_type}: {e}")
        raise ValueError(f"Calculation failed: {e!s}") from e

    # Compare result to expected
    try:
        expected_decimal = Decimal(str(expected_value))
    except (ValueError, TypeError, InvalidOperation) as e:
        raise ValueError(
            f"Invalid expected_value: {expected_value}. Error: {e!s}"
        ) from e

    matches = abs(result - expected_decimal) <= tolerance

    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=1.0 if matches else 0.0,
        status=VerifierResultStatus.OK,
        verifier_result_values={
            "calculation_type": calculation_type,
            "result": float(result),
            "expected_value": float(expected_decimal),
            "tolerance": float(tolerance),
            "components": components_detail if "components" in calc_config else None,
            "matches": matches,
        },
        message=f"{calculation_type}: ${float(result):,.2f} "
        f"{'✓' if matches else f'✗ expected ${float(expected_decimal):,.2f}'}",
    )
