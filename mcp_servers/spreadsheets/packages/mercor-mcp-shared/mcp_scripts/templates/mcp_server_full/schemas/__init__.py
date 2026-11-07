"""API schemas for __SNAKE_NAME__.

This package contains Pydantic models that define:
- Input schemas: Request validation
- Output schemas: Response structure
- API configuration: URL templates, HTTP methods

Organize schemas by domain (e.g., orders.py, customers.py).
"""

from .base import APIConfigurable

__all__ = ["APIConfigurable"]
