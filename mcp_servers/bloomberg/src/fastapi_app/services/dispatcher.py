import inspect
from collections.abc import AsyncIterator
from typing import Any

from fastapi_app.models.base import (
    BaseRequest,
    ErrorResponse,
    EventType,
    ResponseEnvelope,
)


class RequestDispatcher:
    """
    Async dispatcher that routes requests to async handlers.

    Usage:
        dispatcher = RequestDispatcher()

        dispatcher.register_handler(
            request_type="IntradayBarRequest",
            handler=IntradayBarHandler(openbb_client),
            request_model=IntradayBarRequest
        )

        # in route:
        async for envelope in dispatcher.dispatch_async(request_data, streaming=True):
            ...
    """

    def __init__(self):
        """Initialize dispatcher with empty registry"""
        self.handlers: dict[str, Any] = {}
        self.request_models: dict[str, type[BaseRequest]] = {}

    def register_handler(
        self: "RequestDispatcher", request_type: str, handler: Any, request_model: type[BaseRequest]
    ):
        """Register an async handler for a request type"""
        self.handlers[request_type] = handler
        self.request_models[request_type] = request_model

    async def dispatch_async(
        self: "RequestDispatcher", request_data: dict[str, Any], **kwargs
    ) -> AsyncIterator[ResponseEnvelope]:
        """
        Async dispatch that yields ResponseEnvelope items.

        Args:
            request_data: Request payload dictionary with 'requestType'
            streaming: Whether to produce partial responses
            **kwargs: Additional args passed to handler

        Yields:
            ResponseEnvelope objects

        Raises:
            ValueError if requestType is missing or request invalid
            ValidationError if requestType is unknown
        """
        # --- validate request type ---
        request_type = request_data.get("requestType")
        if not request_type:
            error_response = ErrorResponse(
                code="BAD_ARGS.MISSING_REQUEST_TYPE",
                message="Missing 'requestType' in request",
            )
            yield ResponseEnvelope(eventType=EventType.ERROR, response=error_response)  # type: ignore
            return

        if request_type not in self.handlers:
            error_response = ErrorResponse(
                code="BAD_ARGS.UNKNOWN_REQUEST",
                message=f"Unknown requestType: {request_type}",
            )
            yield ResponseEnvelope(eventType=EventType.ERROR, response=error_response)  # type: ignore
            return

        handler = self.handlers[request_type]
        request_model = self.request_models[request_type]

        # --- create request object ---
        try:
            request_obj = request_model(**request_data)
        except TypeError as e:
            raise ValueError(f"Invalid request parameters: {str(e)}") from e

        # --- dispatch to async handler ---
        handle = handler.handle_request
        if not (inspect.iscoroutinefunction(handle) or inspect.isasyncgenfunction(handle)):
            raise TypeError(f"Handler {handler} must implement async handle_request()")

        async for envelope in handle(request_obj, **kwargs):
            yield envelope
