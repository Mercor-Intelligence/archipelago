import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from fastapi_app.models import (
    ErrorResponse,
    ValidationError,
)
from fastapi_app.services.service_manager import get_service_manager

router = APIRouter()

logger = logging.getLogger(__name__)


@router.post("/blp/refdata")
async def refdata_endpoint(request: Request):
    manager = get_service_manager()
    manager.initialize()  # Safe to call multiple times

    """Unified Bloomberg RefData endpoint with optional SSE streaming."""
    try:
        request_data = await request.json()
    except Exception as e:
        error = ErrorResponse(code="BAD_REQUEST", message=f"Invalid JSON: {str(e)}")
        return JSONResponse(content=error.to_dict(), status_code=400)

    if not request_data:
        error = ErrorResponse(code="BAD_REQUEST", message="Empty request body")
        return JSONResponse(content=error.to_dict(), status_code=400)

    # Validate requestType before starting stream
    if not request_data.get("requestType"):
        error = ErrorResponse(code="BAD_REQUEST", message="Missing 'requestType' in request")
        return JSONResponse(content=error.to_dict(), status_code=400)

    # Optional: create a stop_event to handle client disconnect
    stop_event = asyncio.Event()
    try:
        async_gen = manager.dispatcher.dispatch_async(request_data, stop_event=stop_event)

        async def sse_gen():
            async for envelope in async_gen:
                # Convert envelope to SSE format
                yield f"data: {json.dumps(envelope.to_dict(), default=str)}\n\n"
                # Optionally, stop if client disconnects
                if stop_event.is_set():
                    break

        return StreamingResponse(
            sse_gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    except ValueError as e:
        error = ErrorResponse(code="BAD_REQUEST", message=str(e))
        return JSONResponse(content=error.to_dict(), status_code=400)
    except ValidationError as e:
        error = ErrorResponse(code=e.code, message=e.message)
        return JSONResponse(content=error.to_dict(), status_code=400)
    except Exception:
        error = ErrorResponse(code="INTERNAL_ERROR", message="An internal error occurred")
        return JSONResponse(content=error.to_dict(), status_code=500)
