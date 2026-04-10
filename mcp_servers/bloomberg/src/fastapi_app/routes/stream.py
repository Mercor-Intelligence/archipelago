"""Streaming endpoint for random numbers."""

import json
from collections.abc import AsyncGenerator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ..services import generate_random_numbers

router = APIRouter()


@router.get("/stream")
async def stream_random_numbers(interval: float = 1.0) -> StreamingResponse:
    """Stream random numbers via Server-Sent Events.

    Args:
        interval: Time in seconds between numbers (default: 1.0)

    Returns:
        StreamingResponse with SSE format
    """

    async def event_stream() -> AsyncGenerator[str]:
        """Generate SSE formatted events."""
        async for number in generate_random_numbers(interval):
            # Format as SSE
            data = json.dumps({"value": number})
            yield f"data: {data}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
