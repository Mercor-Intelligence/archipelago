from collections.abc import AsyncGenerator

from fastapi_app.models.base import ErrorResponse, EventType, ResponseEnvelope
from shared.models.errors import FieldError
from shared.models.fields import FIELDS


async def validate_and_yield_errors(
    request_type: str,
    fields: list[str] | None,
) -> AsyncGenerator[ResponseEnvelope] | None:
    """
    Validates fields and yields error envelope if invalid.
    Returns None if validation passes.
    """
    if not fields:
        return None

    field_errors = validate_fields(request_type, fields)
    if field_errors:

        async def error_gen():
            error_response = ErrorResponse(
                code="INVALID_FIELDS",
                message=f"Invalid fields requested: {[e.field for e in field_errors]}",
            )
            yield ResponseEnvelope(eventType=EventType.ERROR, response=error_response)  # type: ignore

        return error_gen()

    return None


def validate_fields(request_type: str, requested_fields: list[str]) -> list[FieldError]:
    errors = []

    limits = {
        "ReferenceDataRequest": 400,
        "HistoricalDataRequest": 25,
        "BeqsRequest": 50,
        "IntradayBarRequest": 50,
    }
    max_fields = limits.get(request_type, None)
    if max_fields and len(requested_fields) > max_fields:
        errors.append(
            FieldError(
                source="validator",
                category="limit",
                message=f"{request_type} allows max {max_fields} fields, got {len(requested_fields)}",
                subcategory="field_count",
            )
        )

    # Group fields by mnemonic to handle duplicate mnemonics with different request_types
    fields_by_mnemonic: dict[str, list] = {}
    for f in FIELDS:
        if f.mnemonic not in fields_by_mnemonic:
            fields_by_mnemonic[f.mnemonic] = []
        fields_by_mnemonic[f.mnemonic].append(f)

    for field in requested_fields:
        if field not in fields_by_mnemonic:
            errors.append(
                FieldError(
                    source="validator",
                    category="invalid_field",
                    message=f"Field {field} does not exist",
                    subcategory="field_name",
                    field=field,
                )
            )
        else:
            # Check if ANY of the field definitions support this request_type
            field_defs = fields_by_mnemonic[field]
            supported = any(request_type in f.request_types for f in field_defs)
            if not supported:
                errors.append(
                    FieldError(
                        source="validator",
                        category="incompatible_field",
                        message=f"Field {field} cannot be used in request type {request_type}",
                        subcategory="request_type",
                        field=field,
                    )
                )

    return errors
