"""Activity repository for Greenhouse MCP Server.

Handles data access for Activity (audit log) entities.
"""

from __future__ import annotations

from typing import Any

from db.models import Activity, Note
from repositories.base import BaseRepository
from repositories.exceptions import NotFoundError
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload


class ActivityRepository(BaseRepository[Activity]):
    """Repository for Activity entity CRUD operations.

    Implements standard repository pattern for activity logs with filtering,
    pagination, and Harvest API response formatting.
    """

    model = Activity

    async def get(self, id: int) -> dict | None:
        """Get a single activity by ID.

        Args:
            id: Activity ID

        Returns:
            Activity data as dict if found, None otherwise
        """
        query = select(Activity).options(selectinload(Activity.user)).where(Activity.id == id)
        result = await self.session.execute(query)
        activity = result.scalar_one_or_none()
        if activity is None:
            return None
        return self._serialize(activity)

    async def get_or_raise(self, id: int) -> dict:
        """Get a single activity by ID or raise NotFoundError.

        Args:
            id: Activity ID

        Returns:
            Activity data as dict

        Raises:
            NotFoundError: If activity doesn't exist
        """
        activity = await self.get(id)
        if activity is None:
            raise NotFoundError(f"Activity with id {id} does not exist")
        return activity

    async def list(
        self,
        filters: dict[str, Any] | None = None,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict]:
        """List activities with optional filters and pagination.

        Args:
            filters: Optional filter criteria:
                - candidate_id: Filter by candidate
                - application_id: Filter by application
                - user_id: Filter by user who performed action
                - created_before: ISO 8601 timestamp
                - created_after: ISO 8601 timestamp
            page: Page number (1-indexed)
            per_page: Number of results per page

        Returns:
            List of activity data dicts
        """
        filters = filters or {}
        filter_clauses = self._build_filters(filters)

        query = (
            select(Activity)
            .options(selectinload(Activity.user))
            .order_by(Activity.created_at.desc())
        )

        if filter_clauses:
            query = query.where(*filter_clauses)

        query = await self._paginate(query, page, per_page)
        result = await self.session.execute(query)
        activities = result.scalars().unique().all()

        return [self._serialize(activity) for activity in activities]

    async def get_feed(
        self,
        candidate_id: int | None = None,
        application_id: int | None = None,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict]:
        """Get activity feed for a candidate or application.

        Args:
            candidate_id: Optional candidate ID to filter by
            application_id: Optional application ID to filter by
            page: Page number (1-indexed)
            per_page: Number of results per page

        Returns:
            List of activity data dicts
        """
        filters = {}
        if candidate_id:
            filters["candidate_id"] = candidate_id
        if application_id:
            filters["application_id"] = application_id

        return await self.list(filters=filters, page=page, per_page=per_page)

    async def count(self, filters: dict[str, Any] | None = None) -> int:
        """Count activities matching filters.

        Args:
            filters: Optional filter criteria (same as list)

        Returns:
            Number of matching activities
        """
        filters = filters or {}
        filter_clauses = self._build_filters(filters)

        query = select(func.count()).select_from(Activity)
        if filter_clauses:
            query = query.where(*filter_clauses)

        result = await self.session.scalar(query)
        return result or 0

    async def create(self, data: dict[str, Any]) -> dict:
        """Create a new activity log entry.

        Args:
            data: Activity data with fields:
                - body: Activity description (required)
                - candidate_id: Candidate ID
                - application_id: Application ID
                - user_id: User ID who performed action
                - activity_type: Type of activity

        Returns:
            Created activity data as dict
        """
        activity = Activity(
            body=data["body"],
            candidate_id=data.get("candidate_id"),
            application_id=data.get("application_id"),
            user_id=data.get("user_id"),
            activity_type=data.get("activity_type"),
        )
        self.session.add(activity)
        await self.session.flush()

        return await self.get_or_raise(activity.id)

    async def update(self, id: int, data: dict[str, Any]) -> dict:
        """Update an existing activity (rarely used).

        Args:
            id: Activity ID
            data: Updated activity data

        Returns:
            Updated activity data as dict

        Raises:
            NotFoundError: If activity doesn't exist
        """
        activity = await self._get_by_id_or_raise(id, "Activity")

        if "body" in data:
            activity.body = data["body"]

        await self.session.flush()
        return await self.get_or_raise(id)

    async def delete(self, id: int) -> bool:
        """Delete an activity by ID.

        Args:
            id: Activity ID

        Returns:
            True if deleted, False if not found
        """
        activity = await self._get_by_id(id)
        if activity is None:
            return False
        await self.session.delete(activity)
        await self.session.flush()
        return True

    async def get_notes(
        self,
        candidate_id: int,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict]:
        """Get notes for a candidate.

        Args:
            candidate_id: Candidate ID
            page: Page number (1-indexed)
            per_page: Number of results per page

        Returns:
            List of note data dicts
        """
        query = (
            select(Note)
            .options(selectinload(Note.user))
            .where(Note.candidate_id == candidate_id)
            .order_by(Note.created_at.desc())
        )

        offset = (page - 1) * per_page
        query = query.offset(offset).limit(per_page)

        result = await self.session.execute(query)
        notes = result.scalars().all()

        return [self._serialize_note(note) for note in notes]

    def _build_filters(self, filters: dict[str, Any]) -> list[Any]:
        """Build SQLAlchemy filter clauses from filter dict."""
        clauses: list[Any] = []

        if candidate_id := filters.get("candidate_id"):
            clauses.append(Activity.candidate_id == candidate_id)

        if application_id := filters.get("application_id"):
            clauses.append(Activity.application_id == application_id)

        if user_id := filters.get("user_id"):
            clauses.append(Activity.user_id == user_id)

        if created_before := filters.get("created_before"):
            clauses.append(Activity.created_at <= created_before)

        if created_after := filters.get("created_after"):
            clauses.append(Activity.created_at >= created_after)

        return clauses

    def _serialize(self, activity: Activity) -> dict[str, Any]:
        """Serialize Activity model to Harvest API format."""
        user = None
        if activity.user:
            user = {
                "id": activity.user.id,
                "first_name": activity.user.first_name,
                "last_name": activity.user.last_name,
                "name": activity.user.name,
                "employee_id": activity.user.employee_id,
            }

        return {
            "id": activity.id,
            "body": activity.body,
            "candidate_id": activity.candidate_id,
            "application_id": activity.application_id,
            "user": user,
            "activity_type": activity.activity_type,
            "created_at": activity.created_at,
        }

    def _serialize_note(self, note: Note) -> dict[str, Any]:
        """Serialize Note model to Harvest API format."""
        user = None
        if note.user:
            user = {
                "id": note.user.id,
                "first_name": note.user.first_name,
                "last_name": note.user.last_name,
                "name": note.user.name,
                "employee_id": note.user.employee_id,
            }

        return {
            "id": note.id,
            "body": note.body,
            "user": user,
            "visibility": note.visibility,
            "created_at": note.created_at,
        }
