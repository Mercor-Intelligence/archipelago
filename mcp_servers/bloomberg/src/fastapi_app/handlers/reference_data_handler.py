import asyncio
import logging
from collections.abc import AsyncGenerator

from fastapi_app.models import (
    EventType,
    ReferenceDataRequest,
    ReferenceDataResponse,
    ResponseEnvelope,
    SecurityData,
)
from fastapi_app.models.base import ErrorResponse
from fastapi_app.services import OpenBBAdapter
from fastapi_app.validator import ReferenceDataValidator
from shared.models.error_models import classify_and_create_error
from shared.models.fields import FIELDS

logger = logging.getLogger(__name__)

_FMP_FIELDS: set[str] = {
    f.mnemonic
    for f in FIELDS
    if f.openbb_mapping
    and f.openbb_mapping.startswith("fmp_")
    and "ReferenceDataRequest" in f.request_types
}


class ReferenceDataHandler:
    """Unified async ReferenceDataRequest handler."""

    def __init__(self, openbb_adapter: OpenBBAdapter):
        self.adapter = openbb_adapter
        self.validator = ReferenceDataValidator()

    async def handle_request(
        self,
        request: ReferenceDataRequest,
        chunk_size: int = 5,
        stop_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[ResponseEnvelope]:
        logger.info(f"Processing {len(request.securities)} securities (streaming always enabled)")

        validation_error = await asyncio.to_thread(self.validator.validate, request)
        if validation_error:
            error_response = ErrorResponse(
                code=validation_error.code,
                message=validation_error.message,
            )
            yield ResponseEnvelope(eventType=EventType.ERROR, response=error_response)  # type: ignore
            return

        collected_data: list[SecurityData] = []

        for i in range(0, len(request.securities), chunk_size):
            chunk = request.securities[i : i + chunk_size]
            if stop_event and stop_event.is_set():
                break

            chunk_results: list[SecurityData] = []
            for idx, security in enumerate(chunk, start=i):
                try:
                    ticker, _, instrument_type = self.adapter.reference_data_request(security)
                    field_data = await self.adapter.fetch_reference_data(
                        ticker,
                        request.fields,
                        overrides=request.overrides,
                        instrument_type=instrument_type,
                    )
                    if not field_data:
                        has_fmp = hasattr(self.adapter.client, "fetch_profile")
                        needs_fmp = instrument_type in ("Bond", "Treasury") or any(
                            f in _FMP_FIELDS for f in request.fields
                        )
                        if needs_fmp and not has_fmp:
                            no_data_msg = (
                                f"Data for {security} requires FMP data source "
                                f"(set FMP_API_KEY). Not available with "
                                f"yfinance-only mode."
                            )
                        elif instrument_type == "Bond":
                            no_data_msg = f"Bond/fixed-income data not found for {security}."
                        else:
                            no_data_msg = f"No reference data for {security}."
                        chunk_results.append(
                            SecurityData(
                                security=security,
                                sequenceNumber=idx,
                                fieldData={"message": no_data_msg},
                                fieldExceptions=[
                                    {
                                        "fieldId": f,
                                        "errorInfo": {
                                            "source": "bloomberg-emulator",
                                            "code": 10,
                                            "category": "FIELD_NOT_APPLICABLE",
                                            "message": no_data_msg,
                                            "subcategory": "NOT_APPLICABLE",
                                        },
                                    }
                                    for f in request.fields
                                ],
                            )
                        )
                    else:
                        missing_fields = [f for f in request.fields if f not in field_data]
                        field_exceptions = []
                        if missing_fields:
                            has_fmp = hasattr(self.adapter.client, "fetch_profile")
                            is_bond = instrument_type == "Bond"
                            is_treasury = instrument_type == "Treasury"
                            for f in missing_fields:
                                needs_fmp = f in _FMP_FIELDS or is_bond or is_treasury
                                if needs_fmp and not has_fmp:
                                    msg = (
                                        f"Field '{f}' requires FMP data source "
                                        f"(set FMP_API_KEY). Not available with "
                                        f"yfinance-only mode."
                                    )
                                elif is_bond and has_fmp:
                                    msg = (
                                        f"Bond data for field '{f}' not found "
                                        f"for {security} in FMP."
                                    )
                                elif is_treasury and has_fmp:
                                    msg = (
                                        f"Treasury data for field '{f}' not "
                                        f"found for {security} in FMP."
                                    )
                                elif f in _FMP_FIELDS:
                                    msg = f"No FMP data for field '{f}' on {security}."
                                else:
                                    msg = f"Field '{f}' is not supported for {security}."
                                field_exceptions.append(
                                    {
                                        "fieldId": f,
                                        "errorInfo": {
                                            "source": "bloomberg-emulator",
                                            "code": 10,
                                            "category": "FIELD_NOT_APPLICABLE",
                                            "message": msg,
                                            "subcategory": "NOT_APPLICABLE",
                                        },
                                    }
                                )
                        chunk_results.append(
                            SecurityData(
                                security=security,
                                sequenceNumber=idx,
                                fieldData=field_data,
                                fieldExceptions=field_exceptions if field_exceptions else None,
                            )
                        )
                except Exception as e:
                    logger.error(f"Error processing {security}: {e}")
                    security_error = classify_and_create_error(security=security, exc=e)
                    chunk_results.append(
                        SecurityData(
                            security=security,
                            sequenceNumber=idx,
                            fieldData={},
                            fieldExceptions=[
                                {
                                    "fieldId": "ERROR",
                                    "errorInfo": security_error.errorInfo.model_dump(),
                                }
                            ],
                        )
                    )

            collected_data.extend(chunk_results)

            # Always emit partial results
            yield ResponseEnvelope(
                eventType=EventType.PARTIAL_RESPONSE,
                response=ReferenceDataResponse(securityData=chunk_results),
            )

        # Emit final complete response
        yield ResponseEnvelope(
            eventType=EventType.RESPONSE,
            response=ReferenceDataResponse(securityData=[]),
        )
