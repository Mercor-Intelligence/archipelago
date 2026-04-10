"""Historical Data Request handler."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

import pandas as pd

from fastapi_app.handlers.stream_response_builder import stream_response
from fastapi_app.models.base import ResponseEnvelope
from fastapi_app.models.historical_data import (
    HistoricalDataRequest,
    HistoricalDataResponse,
    SecurityData,
)
from fastapi_app.services.mock_adapter import MockAdapter
from fastapi_app.services.openbb_adapter import OpenBBAdapter
from shared.utils.field_validator import validate_and_yield_errors

logger = logging.getLogger(__name__)

HISTORICAL_DATA_REQUEST = "HistoricalDataRequest"


class HistoricalDataHandler:
    """Async HistoricalDataRequest handler."""

    def __init__(self, openbb_adapter: OpenBBAdapter | MockAdapter):
        """Initialize handler with OpenBB adapter."""
        self.adapter = openbb_adapter

    async def handle_request(
        self,
        request: HistoricalDataRequest,
        chunk_size: int = 5,
        stop_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[ResponseEnvelope]:
        """Process historical data request and yield ResponseEnvelopes in a streaming fashion."""

        logger.info(
            f"[{request.request_id}] Processing HistoricalDataRequest for {len(request.securities)} securities"
        )

        # Validate fields
        error_gen = await validate_and_yield_errors(
            HISTORICAL_DATA_REQUEST, getattr(request, "fields", None)
        )
        if error_gen:
            async for envelope in error_gen:
                yield envelope
            return

        start_dt = datetime.fromisoformat(request.start_date.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(request.end_date.replace("Z", "+00:00"))

        # Fetch data for all securities
        data_results = await self.adapter.fetch_multiple_securities(
            securities=request.securities,
            fields=request.fields,
            start_date=start_dt,
            end_date=end_dt,
            periodicity=request.periodicity_selection,  # type: ignore
            adjustmentSplit=request.adjustment_split,
            adjustmentNormal=request.adjustment_normal,
            adjustmentAbnormal=request.adjustment_abnormal,
        )

        # Stream using generic helper
        async for envelope in stream_response(
            data_iterable=data_results,
            map_to_response=lambda chunk, is_final: HistoricalDataResponse(
                securityData=[
                    self._build_security_data(
                        security_id=sec_id,
                        sequence_number=idx,
                        df=df,
                        error=error,
                    )
                    for idx, (sec_id, df, error) in enumerate(chunk)
                ]
            ),
            chunk_size=chunk_size,
            stop_event=stop_event,
        ):
            yield envelope

    def _build_security_data(
        self,
        security_id: str,
        sequence_number: int,
        df: pd.DataFrame | None,
        error: dict[str, Any] | None,
    ) -> SecurityData:
        """Build SecurityData object from results."""

        if df is not None and error is None:
            records = df.to_dict("records")
            field_data: dict[str, Any] = {"data": records, "dataPoints": len(records)}
            if len(records) == 0:
                field_data["message"] = (
                    f"No historical data available for {security_id} in the requested date range. "
                    f"The security may not have been trading during this period, "
                    f"or data may not be available from the data provider."
                )
            return SecurityData(
                security=security_id,
                sequenceNumber=sequence_number,
                fieldData=field_data,
            )

        # Build an error entry
        error_info = (
            error
            if isinstance(error, dict) and "errorInfo" in error
            else {
                "source": "bloomberg-emulator",
                "code": 99,
                "category": "UNKNOWN",
                "message": str(error) if error else "Unknown error",
                "subcategory": "UNKNOWN",
            }
        )

        return SecurityData(
            security=security_id,
            sequenceNumber=sequence_number,
            fieldData={},
            fieldExceptions=[{"fieldId": "ERROR", "errorInfo": error_info}],
        )
