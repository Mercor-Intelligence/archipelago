"""Scorecard repository for Greenhouse MCP Server.

Handles data access for Scorecard (interview feedback) entities.
"""

from __future__ import annotations

from typing import Any

from db.models import Scorecard, ScorecardAttribute, ScorecardQuestion
from repositories.base import BaseRepository
from repositories.exceptions import NotFoundError
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload


class ScorecardRepository(BaseRepository[Scorecard]):
    """Repository for Scorecard entity CRUD operations.

    Implements standard repository pattern for interview feedback/scorecards
    with filtering, pagination, and Harvest API response formatting.
    """

    model = Scorecard

    async def get(self, id: int) -> dict | None:
        """Get a single scorecard by ID.

        Args:
            id: Scorecard ID

        Returns:
            Scorecard data as dict if found, None otherwise
        """
        query = (
            select(Scorecard)
            .options(
                selectinload(Scorecard.attributes),
                selectinload(Scorecard.questions),
                selectinload(Scorecard.interviewer),
                selectinload(Scorecard.application),
            )
            .where(Scorecard.id == id)
        )
        result = await self.session.execute(query)
        scorecard = result.scalar_one_or_none()
        if scorecard is None:
            return None
        return self._serialize(scorecard)

    async def get_or_raise(self, id: int) -> dict:
        """Get a single scorecard by ID or raise NotFoundError.

        Args:
            id: Scorecard ID

        Returns:
            Scorecard data as dict

        Raises:
            NotFoundError: If scorecard doesn't exist
        """
        scorecard = await self.get(id)
        if scorecard is None:
            raise NotFoundError(f"Scorecard with id {id} does not exist")
        return scorecard

    async def list(
        self,
        filters: dict[str, Any] | None = None,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict]:
        """List scorecards with optional filters and pagination.

        Args:
            filters: Optional filter criteria:
                - application_id: Filter by application
                - interviewer_id: Filter by interviewer
                - created_before: ISO 8601 timestamp
                - created_after: ISO 8601 timestamp
                - updated_before: ISO 8601 timestamp
                - updated_after: ISO 8601 timestamp
            page: Page number (1-indexed)
            per_page: Number of results per page

        Returns:
            List of scorecard data dicts
        """
        filters = filters or {}
        filter_clauses = self._build_filters(filters)

        query = (
            select(Scorecard)
            .options(
                selectinload(Scorecard.attributes),
                selectinload(Scorecard.questions),
                selectinload(Scorecard.interviewer),
                selectinload(Scorecard.application),
            )
            .order_by(Scorecard.id)
        )

        if filter_clauses:
            query = query.where(*filter_clauses)

        query = await self._paginate(query, page, per_page)
        result = await self.session.execute(query)
        scorecards = result.scalars().unique().all()

        return [self._serialize(sc) for sc in scorecards]

    async def list_by_application(self, application_id: int) -> list[dict]:
        """List all scorecards for an application.

        Args:
            application_id: Application ID

        Returns:
            List of scorecard data dicts
        """
        return await self.list(filters={"application_id": application_id})

    async def count(self, filters: dict[str, Any] | None = None) -> int:
        """Count scorecards matching filters.

        Args:
            filters: Optional filter criteria (same as list)

        Returns:
            Number of matching scorecards
        """
        filters = filters or {}
        filter_clauses = self._build_filters(filters)

        query = select(func.count()).select_from(Scorecard)
        if filter_clauses:
            query = query.where(*filter_clauses)

        result = await self.session.scalar(query)
        return result or 0

    async def create(self, data: dict[str, Any]) -> dict:
        """Submit a new scorecard (interview feedback).

        Args:
            data: Scorecard data with fields:
                - application_id: Application ID (required)
                - interviewer_id: Interviewer user ID (required)
                - interview_step_id: Interview step ID
                - overall_recommendation: strong_yes, yes, no_decision, no, strong_no
                - attributes: List of attribute ratings
                - questions: List of question answers

        Returns:
            Created scorecard data as dict
        """
        scorecard = Scorecard(
            application_id=data["application_id"],
            interviewer_id=data["interviewer_id"],
            interview_step_id=data.get("interview_step_id"),
            overall_recommendation=data.get("overall_recommendation"),
            submitted_at=data.get("submitted_at"),
        )
        self.session.add(scorecard)
        await self.session.flush()

        # Add attributes
        for attr_data in data.get("attributes", []):
            attribute = ScorecardAttribute(
                scorecard_id=scorecard.id,
                name=attr_data["name"],
                type=attr_data.get("type", "text"),
                note=attr_data.get("note"),
                rating=attr_data.get("rating"),
            )
            self.session.add(attribute)

        # Add questions
        for q_data in data.get("questions", []):
            question = ScorecardQuestion(
                scorecard_id=scorecard.id,
                question=q_data["question"],
                answer=q_data.get("answer"),
            )
            self.session.add(question)

        await self.session.flush()
        return await self.get_or_raise(scorecard.id)

    async def update(self, id: int, data: dict[str, Any]) -> dict:
        """Update an existing scorecard.

        Args:
            id: Scorecard ID
            data: Updated scorecard data

        Returns:
            Updated scorecard data as dict

        Raises:
            NotFoundError: If scorecard doesn't exist
        """
        scorecard = await self._get_by_id_or_raise(id, "Scorecard")

        if "overall_recommendation" in data:
            scorecard.overall_recommendation = data["overall_recommendation"]
        if "submitted_at" in data:
            scorecard.submitted_at = data["submitted_at"]

        await self.session.flush()
        return await self.get_or_raise(id)

    async def delete(self, id: int) -> bool:
        """Delete a scorecard by ID.

        Args:
            id: Scorecard ID

        Returns:
            True if deleted, False if not found
        """
        scorecard = await self._get_by_id(id)
        if scorecard is None:
            return False
        await self.session.delete(scorecard)
        await self.session.flush()
        return True

    def _build_filters(self, filters: dict[str, Any]) -> list[Any]:
        """Build SQLAlchemy filter clauses from filter dict."""
        clauses: list[Any] = []

        if application_id := filters.get("application_id"):
            clauses.append(Scorecard.application_id == application_id)

        if interviewer_id := filters.get("interviewer_id"):
            clauses.append(Scorecard.interviewer_id == interviewer_id)

        if created_before := filters.get("created_before"):
            clauses.append(Scorecard.created_at <= created_before)

        if created_after := filters.get("created_after"):
            clauses.append(Scorecard.created_at >= created_after)

        if updated_before := filters.get("updated_before"):
            clauses.append(Scorecard.updated_at <= updated_before)

        if updated_after := filters.get("updated_after"):
            clauses.append(Scorecard.updated_at >= updated_after)

        return clauses

    def _serialize(self, scorecard: Scorecard) -> dict[str, Any]:
        """Serialize Scorecard model to Harvest API format."""
        interviewer = None
        if scorecard.interviewer:
            interviewer = {
                "id": scorecard.interviewer.id,
                "first_name": scorecard.interviewer.first_name,
                "last_name": scorecard.interviewer.last_name,
                "name": scorecard.interviewer.name,
                "employee_id": scorecard.interviewer.employee_id,
            }

        application = None
        if scorecard.application:
            application = {
                "id": scorecard.application.id,
                "candidate_id": scorecard.application.candidate_id,
                "job_id": scorecard.application.job_id,
            }

        attributes = [
            {
                "name": a.name,
                "type": a.type,
                "note": a.note,
                "rating": a.rating,
            }
            for a in scorecard.attributes
        ]

        questions = [
            {
                "id": q.id,
                "question": q.question,
                "answer": q.answer,
            }
            for q in scorecard.questions
        ]

        return {
            "id": scorecard.id,
            "application": application,
            "interview_step_id": scorecard.interview_step_id,
            "interviewer": interviewer,
            "overall_recommendation": scorecard.overall_recommendation,
            "submitted_at": scorecard.submitted_at,
            "created_at": scorecard.created_at,
            "updated_at": scorecard.updated_at,
            "attributes": attributes,
            "questions": questions,
        }
