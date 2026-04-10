from typing import Any

from fastapi_app.models import ReferenceDataRequest, ValidationError

VALID_OVERRIDES = {"EQY_FUND_CRNCY"}


class ReferenceDataValidator:
    """Validates ReferenceDataRequest parameters"""

    def validate(self, request: ReferenceDataRequest) -> ValidationError | None:
        """
        Validate the request according to MVP rules matrix
        Returns ValidationError if invalid, None if valid
        """
        # Check requestType
        if request.requestType != "ReferenceDataRequest":
            return ValidationError(
                code="BAD_ARGS.UNKNOWN_REQUEST",
                message=f"Invalid requestType: {request.requestType}. Must be 'ReferenceDataRequest'",
            )

        # Validate securities
        if not request.securities or len(request.securities) == 0:
            return ValidationError(
                code="BAD_ARGS.NO_SECURITIES", message="At least one security is required"
            )

        # Validate maximum securities limit
        if len(request.securities) > 50:
            return ValidationError(
                code="BAD_ARGS.TOO_MANY_SECURITIES", message="Maximum 50 securities allowed"
            )

        # Validate fields
        if not request.fields or len(request.fields) == 0:
            return ValidationError(
                code="BAD_ARGS.NO_FIELDS", message="At least one field is required"
            )

        # Validate overrides
        if request.overrides:
            override_error = self._validate_overrides(request.overrides)
            if override_error:
                return ValidationError(
                    code=override_error["code"], message=override_error["message"]
                )

        return None

    def _validate_overrides(self, overrides: Any) -> dict[str, str] | None:
        """
        Validate overrides per MVP rules.

        Returns error dict if invalid, None if valid/acceptable.
        Works with both dict and Pydantic model overrides.

        Args:
            overrides: Override list to validate

        Returns:
            Error dict if invalid, None if valid
        """
        if not overrides:
            return None

        # Ensure overrides is a list/iterable, not a single dict
        if isinstance(overrides, dict):
            return {
                "code": "BAD_FLD.INVALID_OVERRIDE_FIELD",
                "message": "Overrides must be a list",
            }

        for override in overrides:
            # Handle both dict and object types
            if isinstance(override, dict):
                field_id = override.get("fieldId")
                has_value = "value" in override
            else:
                field_id = getattr(override, "fieldId", None)
                has_value = hasattr(override, "value")

            # Validate field_id is present and in whitelist
            if not field_id or field_id not in VALID_OVERRIDES:
                return {
                    "code": "BAD_FLD.INVALID_OVERRIDE_FIELD",
                    "message": f"Invalid override field: {field_id}",
                }

            # Validate value field exists (but allow falsy values like 0 or "0")
            if not has_value:
                return {
                    "code": "BAD_FLD.INVALID_OVERRIDE_FIELD",
                    "message": f"Override for {field_id} missing required 'value' field",
                }

        return None
