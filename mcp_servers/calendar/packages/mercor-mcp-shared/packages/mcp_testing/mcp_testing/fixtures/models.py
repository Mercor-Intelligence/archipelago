"""Fixture data models."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..core.types import JSONValue


class FixtureRequest(BaseModel):
    """Request portion of a fixture."""

    method: str = Field(..., description="HTTP method (GET, POST, etc.)")
    endpoint: str = Field(..., description="API endpoint path")
    params: dict[str, Any] | None = Field(None, description="Query parameters")
    body: dict[str, Any] | None = Field(None, description="Request body")
    headers: dict[str, str] | None = Field(None, description="Request headers")


class FixtureExpected(BaseModel):
    """Expected response portion of a fixture."""

    status: int = Field(..., description="Expected HTTP status code")
    data: JSONValue = Field(None, description="Expected response data (any valid JSON type)")
    error_contains: list[str] | None = Field(None, description="Expected error message keywords")
    note: str | None = Field(None, description="Additional notes about this fixture")


class FixtureModel(BaseModel):
    """Complete fixture model."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Invoice list empty",
                "request": {
                    "method": "GET",
                    "endpoint": "/v1/invoice",
                    "params": {"customer_id": "999"},
                },
                "expected": {
                    "status": 200,
                    "data": {"QueryResponse": {"Invoice": [], "maxResults": 0}},
                },
            }
        }
    )

    name: str = Field(..., description="Fixture name/description")
    request: FixtureRequest = Field(..., description="Request details")
    response: JSONValue = Field(
        None, description="Actual API response (for reference, any valid JSON type)"
    )
    expected: FixtureExpected = Field(..., description="Expected behavior")
