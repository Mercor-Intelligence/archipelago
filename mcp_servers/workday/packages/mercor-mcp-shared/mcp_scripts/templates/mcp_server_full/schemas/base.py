"""Base schema definitions and protocols.

This module defines the APIConfigurable protocol that input schemas
can implement to provide API configuration for the repository pattern.
"""

from typing import Any, Protocol


class APIConfigurable(Protocol):
    """Protocol for input models that provide API configuration.

    Implement this protocol in your input schemas to enable
    automatic URL construction and API calls.
    """

    @staticmethod
    def get_api_config() -> dict:
        """Get API configuration including URL template and method.

        Returns:
            Dictionary with keys:
                - url_template: URL template with {field} placeholders
                - method: HTTP method (GET, POST, etc.)
                - body_template: Optional POST body template

        Example:
            return {
                "url_template": "/v2/orders/{order_id}",
                "method": "GET",
            }
        """
        ...

    def to_template_values(self) -> dict[str, str]:
        """Convert model fields to template substitution values.

        Returns:
            Dictionary mapping field names to string values
        """
        ...

    def matches(self, lookup_key: dict[str, Any]) -> bool:
        """Check if this input matches the given lookup key.

        Used for finding matching records in synthetic data.

        Args:
            lookup_key: Dictionary of field names to values

        Returns:
            True if this model matches the lookup criteria
        """
        ...
