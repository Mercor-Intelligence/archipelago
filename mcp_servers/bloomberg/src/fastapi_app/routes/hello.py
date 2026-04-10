"""Hello world endpoint."""

from fastapi import APIRouter

from ..models import HelloResponse

router = APIRouter()


@router.get("/hello", response_model=HelloResponse)
async def hello() -> HelloResponse:
    """Return hello world message."""
    return HelloResponse(message="Hello World")
