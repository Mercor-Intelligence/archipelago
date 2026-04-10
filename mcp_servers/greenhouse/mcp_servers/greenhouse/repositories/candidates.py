"""Candidate repository for Greenhouse MCP Server.

Handles data access for Candidate entities with Harvest API response formatting.
"""

from __future__ import annotations

from typing import Any

from db.models import (
    Candidate,
    CandidateAddress,
    CandidateEducation,
    CandidateEmailAddress,
    CandidateEmployment,
    CandidatePhoneNumber,
    CandidateSocialMediaAddress,
    CandidateTag,
    CandidateWebsiteAddress,
    Note,
    Tag,
)
from repositories.base import BaseRepository
from repositories.exceptions import NotFoundError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload


class CandidateRepository(BaseRepository[Candidate]):
    """Repository for Candidate entity CRUD operations.

    Implements standard repository pattern for candidates with filtering,
    pagination, and Harvest API response formatting.
    """

    model = Candidate

    async def get(self, id: int) -> dict | None:
        """Get a single candidate by ID.

        Args:
            id: Candidate ID

        Returns:
            Candidate data as dict if found, None otherwise
        """
        query = (
            select(Candidate)
            .options(
                selectinload(Candidate.recruiter),
                selectinload(Candidate.coordinator),
                selectinload(Candidate.phone_numbers),
                selectinload(Candidate.email_addresses),
                selectinload(Candidate.addresses),
                selectinload(Candidate.website_addresses),
                selectinload(Candidate.social_media_addresses),
                selectinload(Candidate.educations),
                selectinload(Candidate.employments),
                selectinload(Candidate.attachments),
                selectinload(Candidate.tags).selectinload(CandidateTag.tag),
            )
            .where(Candidate.id == id)
        )
        result = await self.session.execute(query)
        candidate = result.scalar_one_or_none()
        if candidate is None:
            return None
        return self._serialize(candidate)

    async def get_or_raise(self, id: int) -> dict:
        """Get a single candidate by ID or raise NotFoundError.

        Args:
            id: Candidate ID

        Returns:
            Candidate data as dict

        Raises:
            NotFoundError: If candidate doesn't exist
        """
        candidate = await self.get(id)
        if candidate is None:
            raise NotFoundError(f"Candidate with id {id} does not exist")
        return candidate

    async def list(
        self,
        filters: dict[str, Any] | None = None,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict]:
        """List candidates with optional filters and pagination.

        Args:
            filters: Optional filter criteria:
                - created_before: ISO 8601 timestamp
                - created_after: ISO 8601 timestamp
                - updated_before: ISO 8601 timestamp
                - updated_after: ISO 8601 timestamp
            page: Page number (1-indexed)
            per_page: Number of results per page

        Returns:
            List of candidate data dicts
        """
        filters = filters or {}
        filter_clauses = self._build_filters(filters)

        query = (
            select(Candidate)
            .options(
                selectinload(Candidate.recruiter),
                selectinload(Candidate.coordinator),
                selectinload(Candidate.phone_numbers),
                selectinload(Candidate.email_addresses),
                selectinload(Candidate.addresses),
                selectinload(Candidate.website_addresses),
                selectinload(Candidate.social_media_addresses),
                selectinload(Candidate.educations),
                selectinload(Candidate.employments),
                selectinload(Candidate.attachments),
                selectinload(Candidate.tags).selectinload(CandidateTag.tag),
            )
            .order_by(Candidate.id)
        )

        if filter_clauses:
            query = query.where(*filter_clauses)

        query = await self._paginate(query, page, per_page)
        result = await self.session.execute(query)
        candidates = result.scalars().unique().all()

        return [self._serialize(candidate) for candidate in candidates]

    async def search(
        self,
        query_string: str,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict]:
        """Search candidates by name or email.

        Args:
            query_string: Search query
            page: Page number (1-indexed)
            per_page: Number of results per page

        Returns:
            List of matching candidate data dicts
        """
        search_pattern = f"%{query_string}%"

        query = (
            select(Candidate)
            .options(
                selectinload(Candidate.recruiter),
                selectinload(Candidate.coordinator),
                selectinload(Candidate.phone_numbers),
                selectinload(Candidate.email_addresses),
                selectinload(Candidate.addresses),
                selectinload(Candidate.website_addresses),
                selectinload(Candidate.social_media_addresses),
                selectinload(Candidate.educations),
                selectinload(Candidate.employments),
                selectinload(Candidate.attachments),
                selectinload(Candidate.tags).selectinload(CandidateTag.tag),
            )
            .where(
                or_(
                    Candidate.first_name.ilike(search_pattern),
                    Candidate.last_name.ilike(search_pattern),
                    Candidate.company.ilike(search_pattern),
                )
            )
            .order_by(Candidate.id)
        )

        query = await self._paginate(query, page, per_page)
        result = await self.session.execute(query)
        candidates = result.scalars().unique().all()

        return [self._serialize(candidate) for candidate in candidates]

    async def count(self, filters: dict[str, Any] | None = None) -> int:
        """Count candidates matching filters.

        Args:
            filters: Optional filter criteria (same as list)

        Returns:
            Number of matching candidates
        """
        filters = filters or {}
        filter_clauses = self._build_filters(filters)

        query = select(func.count()).select_from(Candidate)
        if filter_clauses:
            query = query.where(*filter_clauses)

        result = await self.session.scalar(query)
        return result or 0

    async def create(self, data: dict[str, Any]) -> dict:
        """Create a new candidate.

        Args:
            data: Candidate data with fields:
                - first_name: First name (required)
                - last_name: Last name (required)
                - company: Company name
                - title: Job title
                - phone_numbers: List of phone numbers
                - email_addresses: List of emails
                - addresses: List of addresses
                - website_addresses: List of websites
                - social_media_addresses: List of social media
                - educations: List of education entries
                - employments: List of employment entries
                - recruiter_id: Recruiter user ID
                - coordinator_id: Coordinator user ID

        Returns:
            Created candidate data as dict
        """
        candidate = Candidate(
            first_name=data["first_name"],
            last_name=data["last_name"],
            company=data.get("company"),
            title=data.get("title"),
            is_private=data.get("is_private", False),
            can_email=data.get("can_email", True),
            recruiter_id=data.get("recruiter_id"),
            coordinator_id=data.get("coordinator_id"),
        )
        self.session.add(candidate)
        await self.session.flush()

        # Add related data
        await self._add_phone_numbers(candidate.id, data.get("phone_numbers", []))
        await self._add_email_addresses(candidate.id, data.get("email_addresses", []))
        await self._add_addresses(candidate.id, data.get("addresses", []))
        await self._add_website_addresses(candidate.id, data.get("website_addresses", []))
        await self._add_social_media_addresses(candidate.id, data.get("social_media_addresses", []))
        await self._add_educations(candidate.id, data.get("educations", []))
        await self._add_employments(candidate.id, data.get("employments", []))

        await self.session.flush()
        return await self.get_or_raise(candidate.id)

    async def update(self, id: int, data: dict[str, Any]) -> dict:
        """Update an existing candidate.

        Args:
            id: Candidate ID
            data: Updated candidate data

        Returns:
            Updated candidate data as dict

        Raises:
            NotFoundError: If candidate doesn't exist
        """
        candidate = await self._get_by_id_or_raise(id, "Candidate")

        if "first_name" in data:
            candidate.first_name = data["first_name"]
        if "last_name" in data:
            candidate.last_name = data["last_name"]
        if "company" in data:
            candidate.company = data["company"]
        if "title" in data:
            candidate.title = data["title"]
        if "is_private" in data:
            candidate.is_private = data["is_private"]
        if "can_email" in data:
            candidate.can_email = data["can_email"]
        if "recruiter_id" in data:
            candidate.recruiter_id = data["recruiter_id"]
        if "coordinator_id" in data:
            candidate.coordinator_id = data["coordinator_id"]

        await self.session.flush()
        return await self.get_or_raise(id)

    async def delete(self, id: int) -> bool:
        """Delete a candidate by ID.

        Args:
            id: Candidate ID

        Returns:
            True if deleted, False if not found
        """
        candidate = await self._get_by_id(id)
        if candidate is None:
            return False
        await self.session.delete(candidate)
        await self.session.flush()
        return True

    async def add_note(self, candidate_id: int, user_id: int, body: str) -> dict:
        """Add a note to a candidate.

        Args:
            candidate_id: Candidate ID
            user_id: User ID who created the note
            body: Note content

        Returns:
            Created note data as dict

        Raises:
            NotFoundError: If candidate doesn't exist
        """
        await self._get_by_id_or_raise(candidate_id, "Candidate")

        note = Note(
            candidate_id=candidate_id,
            user_id=user_id,
            body=body,
            visibility="public",
        )
        self.session.add(note)
        await self.session.flush()

        return {
            "id": note.id,
            "body": note.body,
            "user_id": note.user_id,
            "candidate_id": note.candidate_id,
            "visibility": note.visibility,
            "created_at": note.created_at,
        }

    async def add_tag(self, candidate_id: int, tag_name: str) -> dict:
        """Add a tag to a candidate.

        Args:
            candidate_id: Candidate ID
            tag_name: Tag name to add

        Returns:
            Updated candidate data as dict

        Raises:
            NotFoundError: If candidate doesn't exist
        """
        await self._get_by_id_or_raise(candidate_id, "Candidate")

        # Get or create tag
        query = select(Tag).where(Tag.name == tag_name)
        result = await self.session.execute(query)
        tag = result.scalar_one_or_none()

        if tag is None:
            tag = Tag(name=tag_name)
            self.session.add(tag)
            await self.session.flush()

        # Check if already associated
        existing = await self.session.execute(
            select(CandidateTag).where(
                CandidateTag.candidate_id == candidate_id,
                CandidateTag.tag_id == tag.id,
            )
        )
        if existing.scalar_one_or_none() is None:
            candidate_tag = CandidateTag(candidate_id=candidate_id, tag_id=tag.id)
            self.session.add(candidate_tag)
            await self.session.flush()

        return await self.get_or_raise(candidate_id)

    def _build_filters(self, filters: dict[str, Any]) -> list[Any]:
        """Build SQLAlchemy filter clauses from filter dict."""
        clauses: list[Any] = []

        if created_before := filters.get("created_before"):
            clauses.append(Candidate.created_at <= created_before)

        if created_after := filters.get("created_after"):
            clauses.append(Candidate.created_at >= created_after)

        if updated_before := filters.get("updated_before"):
            clauses.append(Candidate.updated_at <= updated_before)

        if updated_after := filters.get("updated_after"):
            clauses.append(Candidate.updated_at >= updated_after)

        return clauses

    async def _add_phone_numbers(self, candidate_id: int, phone_numbers: list[dict]) -> None:
        """Add phone numbers to a candidate."""
        for phone in phone_numbers:
            self.session.add(
                CandidatePhoneNumber(
                    candidate_id=candidate_id,
                    value=phone["value"],
                    type=phone.get("type", "mobile"),
                )
            )

    async def _add_email_addresses(self, candidate_id: int, emails: list[dict]) -> None:
        """Add email addresses to a candidate."""
        for email in emails:
            self.session.add(
                CandidateEmailAddress(
                    candidate_id=candidate_id,
                    value=email["value"],
                    type=email.get("type", "personal"),
                )
            )

    async def _add_addresses(self, candidate_id: int, addresses: list[dict]) -> None:
        """Add addresses to a candidate."""
        for addr in addresses:
            self.session.add(
                CandidateAddress(
                    candidate_id=candidate_id,
                    value=addr["value"],
                    type=addr.get("type", "home"),
                )
            )

    async def _add_website_addresses(self, candidate_id: int, websites: list[dict]) -> None:
        """Add website addresses to a candidate."""
        for website in websites:
            self.session.add(
                CandidateWebsiteAddress(
                    candidate_id=candidate_id,
                    value=website["value"],
                    type=website.get("type", "personal"),
                )
            )

    async def _add_social_media_addresses(self, candidate_id: int, socials: list[dict]) -> None:
        """Add social media addresses to a candidate."""
        for social in socials:
            self.session.add(
                CandidateSocialMediaAddress(
                    candidate_id=candidate_id,
                    value=social["value"],
                )
            )

    async def _add_educations(self, candidate_id: int, educations: list[dict]) -> None:
        """Add education entries to a candidate."""
        for edu in educations:
            self.session.add(
                CandidateEducation(
                    candidate_id=candidate_id,
                    school_name=edu.get("school_name"),
                    degree=edu.get("degree"),
                    discipline=edu.get("discipline"),
                    start_date=edu.get("start_date"),
                    end_date=edu.get("end_date"),
                )
            )

    async def _add_employments(self, candidate_id: int, employments: list[dict]) -> None:
        """Add employment entries to a candidate."""
        for emp in employments:
            self.session.add(
                CandidateEmployment(
                    candidate_id=candidate_id,
                    company_name=emp.get("company_name"),
                    title=emp.get("title"),
                    start_date=emp.get("start_date"),
                    end_date=emp.get("end_date"),
                )
            )

    def _serialize(self, candidate: Candidate) -> dict[str, Any]:
        """Serialize Candidate model to Harvest API format."""
        recruiter = None
        if candidate.recruiter:
            recruiter = {
                "id": candidate.recruiter.id,
                "first_name": candidate.recruiter.first_name,
                "last_name": candidate.recruiter.last_name,
                "name": candidate.recruiter.name,
                "employee_id": candidate.recruiter.employee_id,
            }

        coordinator = None
        if candidate.coordinator:
            coordinator = {
                "id": candidate.coordinator.id,
                "first_name": candidate.coordinator.first_name,
                "last_name": candidate.coordinator.last_name,
                "name": candidate.coordinator.name,
                "employee_id": candidate.coordinator.employee_id,
            }

        return {
            "id": candidate.id,
            "first_name": candidate.first_name,
            "last_name": candidate.last_name,
            "company": candidate.company,
            "title": candidate.title,
            "is_private": candidate.is_private,
            "can_email": candidate.can_email,
            "photo_url": candidate.photo_url,
            "created_at": candidate.created_at,
            "updated_at": candidate.updated_at,
            "last_activity": candidate.last_activity,
            "recruiter": recruiter,
            "coordinator": coordinator,
            "phone_numbers": [{"value": p.value, "type": p.type} for p in candidate.phone_numbers],
            "email_addresses": [
                {"value": e.value, "type": e.type} for e in candidate.email_addresses
            ],
            "addresses": [{"value": a.value, "type": a.type} for a in candidate.addresses],
            "website_addresses": [
                {"value": w.value, "type": w.type} for w in candidate.website_addresses
            ],
            "social_media_addresses": [
                {"value": s.value} for s in candidate.social_media_addresses
            ],
            "educations": [
                {
                    "id": e.id,
                    "school_name": e.school_name,
                    "degree": e.degree,
                    "discipline": e.discipline,
                    "start_date": e.start_date,
                    "end_date": e.end_date,
                }
                for e in candidate.educations
            ],
            "employments": [
                {
                    "id": e.id,
                    "company_name": e.company_name,
                    "title": e.title,
                    "start_date": e.start_date,
                    "end_date": e.end_date,
                }
                for e in candidate.employments
            ],
            "tags": [{"id": ct.tag.id, "name": ct.tag.name} for ct in candidate.tags if ct.tag],
            "attachments": [
                {
                    "id": a.id,
                    "filename": a.filename,
                    "url": a.url,
                    "type": a.type,
                    "created_at": a.created_at,
                }
                for a in candidate.attachments
            ],
        }
