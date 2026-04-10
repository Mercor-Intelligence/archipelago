"""User repository for Greenhouse MCP Server.

Handles data access for User entities with Harvest API response formatting.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from db.models import (
    Candidate,
    Department,
    Office,
    User,
    UserDepartment,
    UserEmail,
    UserOffice,
)
from repositories.base import BaseRepository
from repositories.exceptions import NotFoundError
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import selectinload


class UserRepository(BaseRepository[User]):
    """Repository for User entity CRUD operations.

    Implements standard repository pattern for users with filtering,
    pagination, and Harvest API response formatting.
    """

    model = User

    async def get(self, id: int) -> dict | None:
        """Get a single user by ID.

        Args:
            id: User ID

        Returns:
            User data as dict if found, None otherwise
        """
        query = (
            select(User)
            .options(
                selectinload(User.emails),
                selectinload(User.departments)
                .selectinload(UserDepartment.department)
                .selectinload(Department.children),
                selectinload(User.offices)
                .selectinload(UserOffice.office)
                .selectinload(Office.children),
            )
            .where(User.id == id)
        )
        result = await self.session.execute(query)
        user = result.scalar_one_or_none()
        if user is None:
            return None
        linked_candidate_ids = await self._load_linked_candidate_ids([user.id])
        return self._serialize(user, linked_candidate_ids)

    async def get_or_raise(self, id: int) -> dict:
        """Get a single user by ID or raise NotFoundError.

        Args:
            id: User ID

        Returns:
            User data as dict

        Raises:
            NotFoundError: If user doesn't exist
        """
        user = await self.get(id)
        if user is None:
            raise NotFoundError(f"User with id {id} does not exist")
        return user

    async def list(
        self,
        filters: dict[str, Any] | None = None,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict]:
        """List users with optional filters and pagination.

        Args:
            filters: Optional filter criteria:
                - email: Filter by email address
                - employee_id: Filter by employee ID
                - created_before: ISO 8601 timestamp
                - created_after: ISO 8601 timestamp
                - updated_before: ISO 8601 timestamp
                - updated_after: ISO 8601 timestamp
            page: Page number (1-indexed)
            per_page: Number of results per page

        Returns:
            List of user data dicts
        """
        filters = filters or {}
        filter_clauses = self._build_filters(filters)

        query = (
            select(User)
            .options(
                selectinload(User.emails),
                selectinload(User.departments)
                .selectinload(UserDepartment.department)
                .selectinload(Department.children),
                selectinload(User.offices)
                .selectinload(UserOffice.office)
                .selectinload(Office.children),
            )
            .order_by(User.id)
        )

        if filter_clauses:
            query = query.where(*filter_clauses)

        query = await self._paginate(query, page, per_page)
        result = await self.session.execute(query)
        users = result.scalars().unique().all()

        user_ids = [user.id for user in users if user.id is not None]
        linked_candidate_ids = await self._load_linked_candidate_ids(user_ids)

        return [self._serialize(user, linked_candidate_ids) for user in users]

    async def count(self, filters: dict[str, Any] | None = None) -> int:
        """Count users matching filters.

        Args:
            filters: Optional filter criteria (same as list)

        Returns:
            Number of matching users
        """
        filters = filters or {}
        filter_clauses = self._build_filters(filters)

        query = select(func.count()).select_from(User)
        if filter_clauses:
            query = query.where(*filter_clauses)

        result = await self.session.scalar(query)
        return result or 0

    async def create(self, data: dict[str, Any]) -> dict:
        """Create a new user.

        Args:
            data: User data with fields:
                - first_name: First name
                - last_name: Last name
                - email: Primary email address
                - employee_id: Optional employee ID
                - site_admin: Optional admin flag

        Returns:
            Created user data as dict
        """
        user = User(
            first_name=data.get("first_name", ""),
            last_name=data.get("last_name", ""),
            primary_email_address=data.get("email", ""),
            employee_id=data.get("employee_id"),
            site_admin=data.get("site_admin", False),
            disabled=data.get("disabled", False),
        )
        self.session.add(user)
        await self.session.flush()
        return await self.get_or_raise(user.id)

    async def update(self, id: int, data: dict[str, Any]) -> dict:
        """Update an existing user.

        Args:
            id: User ID
            data: Updated user data

        Returns:
            Updated user data as dict

        Raises:
            NotFoundError: If user doesn't exist
        """
        user = await self._get_by_id_or_raise(id, "User")

        if "first_name" in data:
            user.first_name = data["first_name"]
        if "last_name" in data:
            user.last_name = data["last_name"]
        if "email" in data:
            user.primary_email_address = data["email"]
        if "employee_id" in data:
            user.employee_id = data["employee_id"]
        if "site_admin" in data:
            user.site_admin = data["site_admin"]
        if "disabled" in data:
            user.disabled = data["disabled"]

        await self.session.flush()
        return await self.get_or_raise(id)

    async def delete(self, id: int) -> bool:
        """Delete a user by ID.

        Args:
            id: User ID

        Returns:
            True if deleted, False if not found
        """
        user = await self._get_by_id(id)
        if user is None:
            return False
        await self.session.delete(user)
        await self.session.flush()
        return True

    def _build_filters(self, filters: dict[str, Any]) -> list[Any]:
        """Build SQLAlchemy filter clauses from filter dict.

        Args:
            filters: Filter criteria dict

        Returns:
            List of SQLAlchemy filter clauses
        """
        clauses: list[Any] = []

        if email := filters.get("email"):
            email_exists = exists(
                select(UserEmail.id)
                .where(UserEmail.user_id == User.id)
                .where(UserEmail.email == email)
            )
            clauses.append(or_(User.primary_email_address == email, email_exists))

        if employee_id := filters.get("employee_id"):
            clauses.append(User.employee_id == employee_id)

        if created_before := filters.get("created_before"):
            clauses.append(User.created_at <= created_before)

        if created_after := filters.get("created_after"):
            clauses.append(User.created_at >= created_after)

        if updated_before := filters.get("updated_before"):
            clauses.append(User.updated_at <= updated_before)

        if updated_after := filters.get("updated_after"):
            clauses.append(User.updated_at >= updated_after)

        return clauses

    async def _load_linked_candidate_ids(self, user_ids: list[int]) -> dict[int, list[int]]:
        """Load candidate IDs linked to users as recruiter or coordinator.

        Args:
            user_ids: List of user IDs to query

        Returns:
            Dict mapping user ID to list of linked candidate IDs
        """
        if not user_ids:
            return {}

        query = select(
            Candidate.id,
            Candidate.recruiter_id,
            Candidate.coordinator_id,
        ).where(
            or_(
                Candidate.recruiter_id.in_(user_ids),
                Candidate.coordinator_id.in_(user_ids),
            )
        )

        result = await self.session.execute(query)
        mapping: dict[int, set[int]] = defaultdict(set)

        for candidate_id, recruiter_id, coordinator_id in result:
            if recruiter_id in user_ids:
                mapping[recruiter_id].add(candidate_id)
            if coordinator_id in user_ids:
                mapping[coordinator_id].add(candidate_id)

        return {user_id: sorted(ids) for user_id, ids in mapping.items()}

    def _serialize(
        self,
        user: User,
        linked_candidate_ids: dict[int, list[int]] | None = None,
    ) -> dict[str, Any]:
        """Serialize User model to Harvest API format.

        Args:
            user: User model instance
            linked_candidate_ids: Optional mapping of user ID to linked candidate IDs

        Returns:
            Dict matching Greenhouse Harvest API response format
        """
        linked_candidate_ids = linked_candidate_ids or {}

        emails = [entry.email for entry in user.emails if entry.email]
        if user.primary_email_address and user.primary_email_address not in emails:
            emails.insert(0, user.primary_email_address)

        departments = []
        for association in user.departments:
            department = association.department
            if department is None:
                continue
            departments.append(
                {
                    "id": department.id,
                    "name": department.name,
                    "parent_id": department.parent_id,
                    "child_ids": [child.id for child in department.children],
                    "external_id": department.external_id,
                }
            )

        offices = []
        for association in user.offices:
            office = association.office
            if office is None:
                continue
            offices.append(
                {
                    "id": office.id,
                    "name": office.name,
                    "location": {"name": office.location_name},
                    "primary_contact_user_id": office.primary_contact_user_id,
                    "parent_id": office.parent_id,
                    "child_ids": [child.id for child in office.children],
                    "external_id": office.external_id,
                }
            )

        return {
            "id": user.id,
            "name": user.name,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "primary_email_address": user.primary_email_address,
            "emails": emails,
            "employee_id": user.employee_id,
            "disabled": user.disabled,
            "site_admin": user.site_admin,
            "created_at": user.created_at,
            "updated_at": user.updated_at,
            "linked_candidate_ids": linked_candidate_ids.get(user.id, []),
            "departments": departments,
            "offices": offices,
        }
