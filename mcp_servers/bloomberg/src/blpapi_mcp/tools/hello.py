import httpx


async def hello() -> dict:
    """Get hello world message."""

    async with httpx.AsyncClient() as client:
        response = await client.get("http://localhost:8000/hello")
        return response.json()
