"""BEQS (Bloomberg Equity Screening) Request handler."""

import asyncio
import logging
from collections.abc import AsyncGenerator

from fastapi_app.models.base import (
    ErrorResponse,
    EventType,
    ResponseEnvelope,
    SecurityResponseError,
)
from fastapi_app.models.beqs import BeqsRequest, BeqsResponse, BeqsSecurityInfo
from fastapi_app.models.enums import ScreenType
from fastapi_app.services.openbb_adapter import OpenBBAdapter
from shared.utils.field_validator import validate_and_yield_errors

logger = logging.getLogger(__name__)


class BeqsHandler:
    """Async BeqsRequest handler."""

    def __init__(self, openbb_adapter: OpenBBAdapter):
        """Initialize handler with OpenBB adapter."""
        self.adapter = openbb_adapter

    async def handle_request(
        self,
        request: BeqsRequest,
        chunk_size: int = 5,
        stop_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[ResponseEnvelope]:
        """Async handler that yields ResponseEnvelope objects."""

        screen_type_enum = request.screenType
        try:
            if isinstance(request.screenType, str):
                screen_type_enum = ScreenType(request.screenType)

            # Ensure the request object is updated with the correct Enum instance
            request.screenType = screen_type_enum

        except ValueError:
            # Handle case where the string value is invalid for the Enum
            error_response = ErrorResponse(
                code="INVALID_SCREEN_TYPE",
                message=f"Invalid screenType provided: '{request.screenType}' is not a valid ScreenType enum member.",
            )
            yield ResponseEnvelope(eventType=EventType.ERROR, response=error_response)  # type: ignore
            return

        # Check and validate fields
        error_gen = await validate_and_yield_errors("BEQS", getattr(request, "fields", None))
        if error_gen:
            async for envelope in error_gen:
                yield envelope
            return

        try:
            logger.info(
                f"Processing BeqsRequest: {request.screenName} "
                f"(Type: {request.screenType.value}, Group: {request.group})"
            )

            # Execute the screen via adapter
            screen_result = await self.adapter.execute_beqs_screen(request)
            securities = [BeqsSecurityInfo(**s) for s in screen_result.get("securities", [])]
            errors = [SecurityResponseError(**e) for e in screen_result.get("responseErrors", [])]

            # Stream chunked securities
            for i in range(0, len(securities), chunk_size):
                if stop_event and stop_event.is_set():
                    break

                chunk = securities[i : i + chunk_size]
                yield ResponseEnvelope(
                    eventType=EventType.PARTIAL_RESPONSE,
                    response=BeqsResponse(
                        screenName=screen_result["screenName"],
                        screenType=request.screenType,
                        asOfDate=screen_result.get("asOfDate", ""),
                        totalSecurities=len(securities),
                        securities=chunk,
                        responseErrors=[],
                    ),
                )

            # Final completion event
            yield ResponseEnvelope(
                eventType=EventType.RESPONSE,
                response=BeqsResponse(
                    screenName=screen_result["screenName"],
                    screenType=request.screenType,
                    asOfDate=screen_result.get("asOfDate", ""),
                    totalSecurities=len(securities),
                    securities=[],
                    responseErrors=errors,
                ),
            )

        except Exception as e:
            logger.error(f"Error executing BEQS screen: {e}", exc_info=True)
            yield ResponseEnvelope(
                eventType=EventType.RESPONSE,
                response=BeqsResponse(
                    screenName=request.screenName,
                    screenType=request.screenType,
                    asOfDate="",
                    totalSecurities=0,
                    securities=[],
                    responseErrors=[
                        SecurityResponseError(
                            security=request.screenName,
                            errorCode="SCREEN_EXECUTION_ERROR",
                            message=str(e),
                        )
                    ],
                ),
            )
