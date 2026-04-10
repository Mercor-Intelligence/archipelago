import asyncio
from collections.abc import AsyncGenerator, Callable, Iterable
from typing import Any

from fastapi_app.models import EventType, ResponseEnvelope


async def stream_response(
    data_iterable: Iterable[Any],
    map_to_response: Callable[[list[Any], bool], Any],
    chunk_size: int = 5,
    stop_event: asyncio.Event | None = None,
) -> AsyncGenerator[ResponseEnvelope]:
    """
    Args:
        map_to_response: Function that takes (chunk: list[Any], is_final: bool) -> Response object
    """
    # Convert to list
    data_list = list(data_iterable)

    # Process data: emit PARTIAL_RESPONSE for each full chunk
    collected = []
    stopped_early = False

    for item in data_list:
        if stop_event and stop_event.is_set():
            stopped_early = True
            break

        collected.append(item)

        if len(collected) >= chunk_size:
            response = map_to_response(collected, False)
            yield ResponseEnvelope(eventType=EventType.PARTIAL_RESPONSE, response=response)
            collected = []

    # Emit remaining data if any
    if collected and not stopped_early:
        response = map_to_response(collected, False)
        yield ResponseEnvelope(eventType=EventType.PARTIAL_RESPONSE, response=response)

    # Only emit final empty RESPONSE if we did NOT stop early
    if not stopped_early:
        empty_response = map_to_response([], True)
        yield ResponseEnvelope(eventType=EventType.RESPONSE, response=empty_response)
