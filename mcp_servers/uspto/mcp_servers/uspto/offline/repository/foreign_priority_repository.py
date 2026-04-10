"""Repository for handling foreign priority queries in offline mode."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from mcp_servers.uspto.offline.db.connection import get_sync_connection
from mcp_servers.uspto.offline.repository.patent_repository import PatentRepository

# Country code to IP office name mapping
# Format matches BUILD_PLAN.md specification: proper IP office names (e.g., "Japan Patent Office")
# not just country names (e.g., "JAPAN")
COUNTRY_CODE_TO_NAME: dict[str, str] = {
    "JP": "Japan Patent Office",
    "FR": "France Patent Office",
    "WO": "WIPO",
    "EP": "European Patent Office",
    "DE": "Germany Patent Office",
    "GB": "United Kingdom Patent Office",
    "CN": "China National Intellectual Property Administration",
    "KR": "Korea Intellectual Property Office",
    "CA": "Canadian Intellectual Property Office",
    "AU": "IP Australia",
    "TW": "Taiwan Intellectual Property Office",
    "IN": "Indian Patent Office",
    "BR": "Brazilian Patent Office",
    "RU": "Russian Federal Service for Intellectual Property",
    "MX": "Mexican Institute of Industrial Property",
    "IT": "Italian Patent and Trademark Office",
    "ES": "Spanish Patent and Trademark Office",
    "NL": "Netherlands Patent Office",
    "SE": "Swedish Patent and Registration Office",
    "CH": "Swiss Federal Institute of Intellectual Property",
    "AT": "Austrian Patent Office",
    "BE": "Belgian Intellectual Property Office",
    "DK": "Danish Patent and Trademark Office",
    "FI": "Finnish Patent and Registration Office",
    "NO": "Norwegian Industrial Property Office",
    "PL": "Polish Patent Office",
    "IL": "Israel Patent Office",
    "SG": "Intellectual Property Office of Singapore",
    "NZ": "Intellectual Property Office of New Zealand",
    "ZA": "Companies and Intellectual Property Commission",
}


class ForeignPriorityRepository:
    """Repository for foreign priority operations in offline mode."""

    def get_foreign_priority(self, application_number: str) -> dict[str, Any]:
        """Return foreign priority claims from offline database.

        This is a synchronous method designed to be called from asyncio.to_thread()
        to avoid blocking the event loop.

        Args:
            application_number: USPTO application number (raw or normalized)

        Returns:
            Dictionary with foreignPriorityClaims array matching BUILD_PLAN.md format:
            {
                "applicationNumberText": "16/123,456",
                "foreignPriorityClaims": [...],
                "metadata": {
                    "retrievedAt": "2025-12-24T17:25:00Z",
                    "totalClaims": 2
                }
            }
            Or error response if application not found:
            {
                "error": {
                    "code": "FOREIGN_PRIORITY_UNAVAILABLE",
                    "message": "Foreign priority data not available for application ...",
                    "details": {"reason": "..."}
                }
            }
        """
        try:
            with get_sync_connection() as conn:
                repo = PatentRepository(conn)

                # Normalize application number for consistent lookups
                normalized_app_number = self._normalize_application_number(application_number)

                # Query patent record by application number
                # Try grant first (matches USPTO API behavior), fall back to application
                patent = repo.get_by_application_number(normalized_app_number, "grant")
                if not patent:
                    patent = repo.get_by_application_number(normalized_app_number, "application")

                if not patent:
                    # Application not found - return error per BUILD_PLAN.md
                    return self._error_response_not_found(application_number)

                # Parse priority claims JSON
                raw_claims = []
                if patent.priority_claims_json:
                    try:
                        raw_claims = json.loads(patent.priority_claims_json)
                    except json.JSONDecodeError:
                        # Malformed JSON - return error (data unavailable)
                        return self._error_response_unavailable(
                            application_number,
                            "Foreign priority data is malformed in database",
                        )

                # Transform claims to match API format
                foreign_priority_claims = []
                for claim in raw_claims:
                    if not isinstance(claim, dict):
                        continue

                    transformed_claim = self._transform_claim(claim)
                    if transformed_claim:
                        foreign_priority_claims.append(transformed_claim)

                # Return in format matching BUILD_PLAN.md specification
                return {
                    "applicationNumberText": application_number,
                    "foreignPriorityClaims": foreign_priority_claims,
                    "metadata": {
                        "retrievedAt": self._get_utc_timestamp(),
                        "totalClaims": len(foreign_priority_claims),
                    },
                }

        except Exception:
            # Database error - return error response
            return self._error_response_unavailable(
                application_number,
                "Database error occurred while retrieving foreign priority data",
            )

    def _normalize_application_number(self, value: str) -> str:
        """Normalize application number by removing non-digit characters.

        Args:
            value: Raw application number (e.g., "16/123,456")

        Returns:
            Normalized application number (e.g., "16123456")
        """
        digits = "".join(ch for ch in value if ch.isdigit())
        return digits or value

    def _error_response_not_found(self, application_number: str) -> dict[str, Any]:
        """Generate error response for application not found (404).

        Args:
            application_number: Original application number from request

        Returns:
            Error response structure per BUILD_PLAN.md
        """
        return {
            "error": {
                "code": "FOREIGN_PRIORITY_UNAVAILABLE",
                "message": (
                    f"Foreign priority data not available for application {application_number}"
                ),
                "details": {
                    "reason": "Application not found in database",
                },
            }
        }

    def _error_response_unavailable(self, application_number: str, reason: str) -> dict[str, Any]:
        """Generate error response for foreign priority data unavailable (422).

        Args:
            application_number: Original application number from request
            reason: Reason why data is unavailable

        Returns:
            Error response structure per BUILD_PLAN.md
        """
        return {
            "error": {
                "code": "FOREIGN_PRIORITY_UNAVAILABLE",
                "message": (
                    f"Foreign priority data not available for application {application_number}"
                ),
                "details": {
                    "reason": reason,
                },
            }
        }

    def _get_utc_timestamp(self) -> str:
        """Return current UTC timestamp in ISO 8601 format.

        Returns:
            ISO 8601 timestamp string (e.g., "2025-12-24T17:25:00Z")
        """
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")

    def _transform_claim(self, claim: dict[str, Any]) -> dict[str, Any] | None:
        """Transform a single foreign priority claim to API format.

        Args:
            claim: Raw claim from database (XML-derived structure)

        Returns:
            Transformed claim in API format, or None if invalid
        """
        # Extract fields from XML-derived structure
        country_code = claim.get("country", "")
        doc_number = claim.get("doc_number", "")
        date_str = claim.get("date", "")

        # Map country code to full name
        ip_office_name = self._map_country_code(country_code)

        # Format date from YYYYMMDD to YYYY-MM-DD
        filing_date = self._format_date(date_str)

        # Build claim in API response format with correct field names
        transformed_claim: dict[str, Any] = {
            "ipOfficeName": ip_office_name,
            "foreignApplicationNumber": doc_number,
        }

        if filing_date:
            transformed_claim["foreignFilingDate"] = filing_date

        if country_code and isinstance(country_code, str):
            transformed_claim["ipOfficeCode"] = country_code.upper()

        # Add priorityClaimIndicator if data exists
        # For XML-sourced data, presence in priority_claims implies "YES"
        transformed_claim["priorityClaimIndicator"] = "YES"

        return transformed_claim

    def _map_country_code(self, country_code: str | Any) -> str:
        """Map country code to full IP office name.

        Args:
            country_code: Two-letter country code (e.g., "JP", "FR")

        Returns:
            Full country/office name, or "UNKNOWN" if invalid
        """
        if not country_code or not isinstance(country_code, str):
            return "UNKNOWN"

        return COUNTRY_CODE_TO_NAME.get(country_code.upper(), country_code.upper())

    def _format_date(self, date_str: str | Any) -> str | None:
        """Convert date from YYYYMMDD to YYYY-MM-DD format.

        Args:
            date_str: Date string in YYYYMMDD format

        Returns:
            Date string in YYYY-MM-DD format, or None if input is invalid
        """
        if not date_str or not isinstance(date_str, str) or len(date_str) != 8:
            return None

        try:
            return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        except (ValueError, IndexError):
            return None


__all__ = ["ForeignPriorityRepository", "COUNTRY_CODE_TO_NAME"]
