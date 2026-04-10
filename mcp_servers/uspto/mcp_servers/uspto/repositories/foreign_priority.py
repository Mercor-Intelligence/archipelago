"""Foreign priority repository for session-scoped database operations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_servers.uspto.db.models import ForeignPriorityRecord
from mcp_servers.uspto.models import ForeignPriorityClaim
from mcp_servers.uspto.utils.dates import coerce_iso_date


class ForeignPriorityRepository:
    """Session-scoped database operations for foreign priority records."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_foreign_priority_record(
        self,
        workspace_id: str,
        application_number_text: str,
        foreign_application_number: str | None,
        foreign_filing_date: str | None,
        ip_office_code: str | None,
        ip_office_name: str | None,
    ) -> ForeignPriorityRecord:
        """Create new foreign priority record in session database."""
        # Convert UTC datetime to ISO string with Z suffix
        retrieved_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        record = ForeignPriorityRecord(
            workspace_id=workspace_id,
            application_number_text=application_number_text,
            foreign_application_number=foreign_application_number,
            foreign_filing_date=foreign_filing_date,
            ip_office_code=ip_office_code,
            ip_office_name=ip_office_name,
            retrieved_at=retrieved_at,
        )
        self.session.add(record)
        await self.session.flush()
        await self.session.refresh(record)
        return record

    async def get_foreign_priority_records(
        self,
        workspace_id: str,
        application_number_text: str,
    ) -> list[ForeignPriorityRecord]:
        """Get foreign priority records for an application in a workspace."""
        result = await self.session.execute(
            select(ForeignPriorityRecord).where(
                ForeignPriorityRecord.workspace_id == workspace_id,
                ForeignPriorityRecord.application_number_text == application_number_text,
            )
        )
        return list(result.scalars().all())

    async def has_foreign_priority_record(
        self,
        workspace_id: str,
        application_number_text: str,
        foreign_application_number: str | None,
    ) -> bool:
        """Check if a foreign priority record already exists.

        Handles NULL foreign_application_number properly since SQLite treats
        NULL values as distinct in unique constraints. For NULL values, checks
        if any record exists with NULL for the workspace/application combination.
        """
        query = select(ForeignPriorityRecord).where(
            ForeignPriorityRecord.workspace_id == workspace_id,
            ForeignPriorityRecord.application_number_text == application_number_text,
        )

        # Handle NULL values: SQLite treats NULL as distinct in unique constraints
        # So we need to check for NULL explicitly using IS NULL
        if foreign_application_number is None:
            query = query.where(ForeignPriorityRecord.foreign_application_number.is_(None))
        else:
            query = query.where(
                ForeignPriorityRecord.foreign_application_number == foreign_application_number
            )

        result = await self.session.execute(query)
        return result.scalar_one_or_none() is not None

    async def store_foreign_priority_claims(
        self,
        workspace_id: str,
        application_number_text: str,
        claims: list[dict],
    ) -> int:
        """Store multiple foreign priority claims for an application.

        Returns the number of claims successfully stored (excluding duplicates).
        Uses savepoints to isolate each insert, so failures don't rollback
        previous successful inserts.
        """
        stored_count = 0
        for claim in claims:
            # Skip None values (malformed API responses can include None in array)
            if not claim:
                continue

            (
                foreign_application_number,
                foreign_filing_date,
                ip_office_code,
            ) = self._extract_claim_fields(claim)
            # Skip if already exists (handles unique constraint)
            if await self.has_foreign_priority_record(
                workspace_id=workspace_id,
                application_number_text=application_number_text,
                foreign_application_number=foreign_application_number,
            ):
                continue

            # Use savepoint to isolate this insert from others
            # This allows rolling back just this insert without affecting
            # previous successful inserts in the loop
            # Exception handling must wrap the context manager so IntegrityError
            # propagates to begin_nested() for automatic savepoint rollback
            try:
                async with self.session.begin_nested():
                    await self.create_foreign_priority_record(
                        workspace_id=workspace_id,
                        application_number_text=application_number_text,
                        foreign_application_number=foreign_application_number,
                        foreign_filing_date=foreign_filing_date,
                        ip_office_code=ip_office_code,
                        ip_office_name=claim.get("ipOfficeName"),
                    )
                    stored_count += 1
            except IntegrityError:
                # Race condition: another request stored the same record
                # Savepoint rollback happens automatically via context manager
                # when exception propagates to begin_nested() __aexit__
                # This only rolls back this insert, not previous ones
                continue

        return stored_count

    @staticmethod
    def parse_foreign_priority_claim(data: dict | None) -> ForeignPriorityClaim | None:
        """Parse foreign priority claim from USPTO response data."""
        if not data:
            return None

        try:
            (
                foreign_application_number,
                foreign_filing_date,
                ip_office_code,
            ) = ForeignPriorityRepository._extract_claim_fields(data)

            return ForeignPriorityClaim(
                foreign_application_number=foreign_application_number,
                foreign_filing_date=foreign_filing_date,
                ip_office_code=ip_office_code,
                ip_office_name=data.get("ipOfficeName"),
                priority_claim_indicator=data.get("priorityClaimIndicator"),
            )
        except Exception:
            # Gracefully skip malformed claims
            return None

    @staticmethod
    def _extract_claim_fields(data: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
        foreign_application_number = data.get("foreignApplicationNumber")
        if foreign_application_number is None:
            foreign_application_number = data.get("applicationNumberText")

        foreign_filing_date = data.get("foreignFilingDate")
        if foreign_filing_date is None:
            foreign_filing_date = data.get("filingDate")
        foreign_filing_date = coerce_iso_date(foreign_filing_date)

        ip_office_code = data.get("ipOfficeCode")
        if ip_office_code is None:
            ip_office_code = data.get("ipOfficeCountry")

        return foreign_application_number, foreign_filing_date, ip_office_code

    @staticmethod
    def parse_foreign_priority_claims(
        raw_claims: list[dict[str, Any]],
    ) -> list[ForeignPriorityClaim]:
        """Parse multiple foreign priority claims from USPTO response data."""
        claims = []
        for claim_data in raw_claims:
            claim = ForeignPriorityRepository.parse_foreign_priority_claim(claim_data)
            if claim:
                claims.append(claim)
        return claims


__all__ = ["ForeignPriorityRepository"]
