"""Example schema for __SNAKE_NAME__.

This file demonstrates how to define API schemas with:
- Input validation using Pydantic
- API configuration for the repository pattern
- Response models

Rename this file to match your domain (e.g., orders.py, customers.py).
"""

from typing import Any

from pydantic import BaseModel, Field


class __PASCAL_NAME__Input(BaseModel):
    """Input schema for __SNAKE_NAME__ queries.

    Implements APIConfigurable protocol for repository pattern.
    """

    id: str = Field(..., description="Unique identifier")

    @staticmethod
    def get_api_config() -> dict:
        """API configuration for this input type."""
        return {
            "url_template": "/v1/__SNAKE_NAME__/{id}",
            "method": "GET",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to URL template values."""
        return {"id": self.id}

    def matches(self, lookup_key: dict[str, Any]) -> bool:
        """Check if this input matches lookup criteria."""
        if not lookup_key:
            return True
        return lookup_key.get("id") == self.id


class __PASCAL_NAME__Response(BaseModel):
    """Response schema for __SNAKE_NAME__.

    Define your response structure here.
    """

    id: str = Field(..., description="Unique identifier")
    name: str = Field(..., description="Display name")
    # Add more fields as needed


class __PASCAL_NAME__ListInput(BaseModel):
    """Input for listing multiple __SNAKE_NAME__ items."""

    limit: int = Field(100, description="Maximum items to return")
    offset: int = Field(0, description="Pagination offset")

    @staticmethod
    def get_api_config() -> dict:
        return {
            "url_template": "/v1/__SNAKE_NAME__",
            "method": "GET",
        }


class __PASCAL_NAME__ListResponse(BaseModel):
    """Response for list operations."""

    items: list[__PASCAL_NAME__Response] = Field(default_factory=list)
    total: int = Field(0, description="Total count")
