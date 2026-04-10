import asyncio
import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pandas as pd
from pandas import Timestamp

from fastapi_app.handlers.stream_response_builder import stream_response
from fastapi_app.models.base import EventType, ResponseEnvelope
from fastapi_app.models.intraday_bar import (
    DEFAULT_EVENT_TYPE,
    BarTickData,
    IntradayBarRequest,
    IntradayBarResponse,
)
from fastapi_app.services.openbb_adapter import OpenBBAdapter
from fastapi_app.utils.numeric_utils import to_float, to_int
from shared.models.error_models import classify_and_create_error
from shared.utils.field_validator import validate_and_yield_errors

logger = logging.getLogger(__name__)


class IntradayBarHandler:
    """Async IntradayBarRequest handler."""

    def __init__(self, openbb_client: OpenBBAdapter):
        self.openbb_client = openbb_client

    async def handle_request(
        self,
        request: IntradayBarRequest,
        chunk_size: int = 10,
        stop_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[ResponseEnvelope]:
        """Async IntradayBarRequest handler that yields ResponseEnvelope objects."""

        # Validate requested fields
        error_gen = await validate_and_yield_errors(
            "IntradayBarRequest", getattr(request, "fields", None)
        )
        if error_gen:
            async for envelope in error_gen:
                yield envelope
            return

        # Convert start/end to datetime
        start_dt = datetime.fromisoformat(request.startDateTime.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(request.endDateTime.replace("Z", "+00:00"))

        try:
            # Fetch intraday bars from OpenBB
            bars_df = await self.openbb_client.fetch_intraday_bars(
                ticker=request.security,
                interval=request.mapped_interval,
                start=start_dt,
                end=end_dt,
            )

            # Transform raw bars into BarTickData objects
            bar_tick_data: list[BarTickData] = self._transform_bars(bars_df)
        except Exception as e:
            logger.error(f"Error fetching intraday bars: {e}", exc_info=True)
            security_error = classify_and_create_error(security=request.security, exc=e)
            yield ResponseEnvelope(
                eventType=EventType.RESPONSE,
                response=IntradayBarResponse(
                    security=request.security,
                    eventType=request.eventType or DEFAULT_EVENT_TYPE,
                    interval=request.interval,
                    barData={
                        "barTickData": [],
                        "totalBars": 0,
                        "message": security_error.errorInfo.message,
                        "securityError": security_error.model_dump(),
                    },
                ),
            )
            return

        end_date_str = end_dt.strftime("%Y-%m-%d")
        empty_bar_message = (
            f"No intraday bar data available for {request.security} on {end_date_str}. "
            f"The security may not have traded during this period, "
            f"or data may not be available from the provider."
        )

        def _make_bar_response(chunk: list, is_final: bool) -> IntradayBarResponse:
            bar_data: dict = {
                "barTickData": [bar.to_dict() for bar in chunk],
                "totalBars": len(bar_tick_data),
            }
            if not bar_tick_data and is_final:
                bar_data["message"] = empty_bar_message
            return IntradayBarResponse(
                security=request.security,
                eventType=request.eventType or DEFAULT_EVENT_TYPE,
                interval=request.interval,
                barData=bar_data,
            )

        async for envelope in stream_response(
            data_iterable=bar_tick_data,
            map_to_response=_make_bar_response,
            chunk_size=chunk_size,
            stop_event=stop_event,
        ):
            yield envelope

    def _transform_bars(self, df: pd.DataFrame) -> list[BarTickData]:
        """Convert provider DataFrame to list of BarTickData with UTC ISO times."""
        results = []

        for timestamp, row in df.iterrows():
            if not isinstance(timestamp, Timestamp | datetime):
                raise TypeError(f"Unsupported index type: {type(timestamp)}")

            if isinstance(timestamp, Timestamp):
                ts_utc = (
                    timestamp.tz_localize("UTC")
                    if timestamp.tzinfo is None
                    else timestamp.tz_convert("UTC")
                )
            else:
                ts_utc = (
                    timestamp.astimezone(UTC) if timestamp.tzinfo else timestamp.replace(tzinfo=UTC)
                )

            time_str = ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            results.append(
                BarTickData(
                    time=time_str,
                    PX_OPEN=to_float(row["open"]),
                    PX_HIGH=to_float(row["high"]),
                    PX_LOW=to_float(row["low"]),
                    PX_LAST=to_float(row["close"]),
                    VOLUME=to_int(row["volume"]),
                )
            )
        return results
