import httpx

latest_values = []


async def stream_numbers():
    url = "http://127.0.0.1:8000/stream"  # your FastAPI SSE endpoint
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("GET", url) as response:
            async for line in response.aiter_lines():
                if not line:
                    continue
                if line.startswith("data:"):
                    # parse the JSON value from the SSE line
                    import json

                    try:
                        payload = json.loads(line[len("data:") :].strip())
                    except json.JSONDecodeError:
                        continue

                    # store in buffer if needed
                    latest_values.append(payload)

                    # yield to MCP clients
                    yield payload
