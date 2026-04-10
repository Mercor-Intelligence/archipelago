import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime

from fastapi_app.handlers.stream_response_builder import stream_response
from fastapi_app.models import IntradayTickRequest
from fastapi_app.models.base import EventType, ResponseEnvelope
from fastapi_app.models.intraday_tick import (
    EIDData,
    IntradayTickResponse,
    TickData,
    TickDataContainer,
)
from fastapi_app.services.openbb_adapter import OpenBBAdapter
from shared.models.error_models import classify_and_create_error
from shared.utils.field_validator import validate_and_yield_errors


class IntradayTickHandler:
    """Handles IntradayTickRequest processing."""

    def __init__(self, openbb_client: OpenBBAdapter):
        self.openbb_client = openbb_client

    async def handle_request(
        self,
        request: IntradayTickRequest,
        chunk_size: int = 10,
        stop_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[ResponseEnvelope]:
        """Process the IntradayTickRequest and yield ResponseEnvelopes."""

        # Validate fields
        error_gen = await validate_and_yield_errors(
            "IntradayTickRequest", getattr(request, "fields", None)
        )
        if error_gen:
            async for envelope in error_gen:
                yield envelope
            return

        start_time = datetime.fromisoformat(request.startDateTime.replace("Z", "+00:00"))
        end_time = datetime.fromisoformat(request.endDateTime.replace("Z", "+00:00"))

        try:
            response_data = await self.openbb_client.fetch_intraday_ticks(
                ticker=request.security,
                event_types=request.eventTypes,
                start=start_time,
                end=end_time,
                include_condition_codes=request.includeConditionCodes or False,
                include_exchange_codes=request.includeExchangeCodes or False,
                include_broker_codes=request.includeBrokerCodes or False,
                include_spread_price=request.includeSpreadPrice or False,
                include_yield=request.includeYield or False,
            )
        except Exception as e:
            security_error = classify_and_create_error(security=request.security, exc=e)
            yield ResponseEnvelope(
                eventType=EventType.RESPONSE,
                response=IntradayTickResponse(
                    tickData=TickDataContainer(eidData=[], tickData=[]),
                    responseError=None,
                    securityError=security_error.model_dump(),
                ),
            )
            return

        eid_data_list = self._transform_eid_data(response_data.get("eidData", []))
        tick_data_list = self._transform_ticks(response_data.get("tickData", []))

        end_date_str = end_time.strftime("%Y-%m-%d")
        empty_tick_message = (
            f"No intraday tick data available for {request.security} on {end_date_str}. "
            f"The security may not have traded during this period, "
            f"or data may not be available from the provider."
        )

        def _make_tick_response(chunk: list, is_final: bool) -> IntradayTickResponse:
            security_error = None
            message = None
            if not tick_data_list and is_final:
                message = empty_tick_message
            return IntradayTickResponse(
                tickData=TickDataContainer(
                    eidData=eid_data_list,
                    tickData=chunk,
                ),
                responseError=None,
                securityError=security_error,
                message=message,
            )

        async for envelope in stream_response(
            data_iterable=tick_data_list,
            map_to_response=_make_tick_response,
            chunk_size=chunk_size,
            stop_event=stop_event,
        ):
            yield envelope

    def _transform_eid_data(self, eid_data_dicts: list[dict]) -> list[EIDData]:
        """Transform EID data dictionaries from client to EIDData objects."""
        return [
            EIDData(
                EID=eid.get("EID"),
                description=eid.get("description"),
            )
            for eid in eid_data_dicts
        ]

    def _transform_ticks(self, tick_dicts: list[dict]) -> list[TickData]:
        """Transform tick dictionaries from client to TickData objects."""
        return [
            TickData(
                time=tick["time"],
                type=tick["type"],
                value=tick["value"],
                size=tick["size"],
                conditionCodes=tick.get("conditionCodes"),
                exchangeCode=tick.get("exchangeCode"),
                brokerCode=tick.get("brokerCode"),
                rpsCode=tick.get("rpsCode"),
                bicMicCode=tick.get("bicMicCode"),
                functionCode=tick.get("functionCode"),
                spreadPrice=tick.get("spreadPrice"),
                yield_=tick.get("yield"),
            )
            for tick in tick_dicts
        ]
