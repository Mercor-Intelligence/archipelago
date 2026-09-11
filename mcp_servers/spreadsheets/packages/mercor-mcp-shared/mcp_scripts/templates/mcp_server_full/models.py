"""Pydantic models for __SNAKE_NAME__.

Define your API specification here using Pydantic models.
These models will:
1. Validate inputs/outputs automatically
2. Generate type hints for IDE support
3. Serve as documentation
4. Enable test generation

TIP: Start simple, add fields as you need them!
"""

from pydantic import BaseModel, Field


class __PASCAL_NAME__Request(BaseModel):
    """Input specification for __SNAKE_NAME__.

    TODO: Define your input parameters here.
    Example:
        query: str = Field(..., description="Search query")
        limit: int = Field(10, description="Max results")
    """

    input_param: str = Field(..., description="TODO: Replace with your actual input parameters")


class __PASCAL_NAME__Response(BaseModel):
    """Output specification for __SNAKE_NAME__.

    TODO: Define your response structure here.
    Example:
        results: list[str] = Field(..., description="Search results")
        total: int = Field(..., description="Total count")
    """

    result: str = Field(..., description="TODO: Replace with your actual response structure")
