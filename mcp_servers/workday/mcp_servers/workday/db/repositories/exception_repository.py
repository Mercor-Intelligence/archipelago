"""ExceptionRepository for managing exception requests and approvals.

This repository handles exception workflow for cases where standard
policy requirements cannot be met.
"""

import json
from uuid import uuid4

from models import ApproveExceptionInput, ExceptionOutput, RequestExceptionInput
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Case, CaseException


class ExceptionRepository:
    """Repository for exception request operations."""

    def create_exception(self, session: Session, request: RequestExceptionInput) -> ExceptionOutput:
        """Create a new exception request.

        Args:
            session: Database session
            request: Exception request details

        Returns:
            Created exception details

        Raises:
            ValueError: If case not found
        """
        # Verify case exists
        case = session.execute(
            select(Case).where(Case.case_id == request.case_id)
        ).scalar_one_or_none()

        if not case:
            raise ValueError(f"Case {request.case_id} not found")

        exception = CaseException(
            exception_id=f"EXC-{uuid4().hex[:8].upper()}",
            case_id=request.case_id,
            milestone_type=request.milestone_type,
            reason=request.reason,
            affected_policy_refs=(
                json.dumps(request.affected_policy_refs) if request.affected_policy_refs else None
            ),
            requested_by=request.actor_persona,
        )
        session.add(exception)
        session.flush()

        # Log audit entry
        from db.repositories.audit_repository import AuditRepository

        audit_repo = AuditRepository()
        audit_repo.log_exception_requested(
            session=session,
            case_id=request.case_id,
            exception_id=exception.exception_id,
            milestone_type=request.milestone_type,
            actor_persona=request.actor_persona,
            reason=request.reason,
            affected_policies=request.affected_policy_refs,
        )

        return self._to_output(exception)

    def approve_exception(
        self, session: Session, request: ApproveExceptionInput
    ) -> ExceptionOutput:
        """Approve or deny an exception request.

        Args:
            session: Database session
            request: Approval decision

        Returns:
            Updated exception details

        Raises:
            ValueError: If exception not found or already decided
        """
        from datetime import UTC, datetime

        exception = session.execute(
            select(CaseException)
            .where(CaseException.exception_id == request.exception_id)
            .with_for_update()
        ).scalar_one_or_none()

        if not exception:
            raise ValueError(f"Exception {request.exception_id} not found")

        if exception.approval_status != "pending":
            raise ValueError(f"Exception already decided (status: {exception.approval_status})")

        # Check actor has appropriate role (must be hr_admin for approvals)
        if request.actor_persona != "hr_admin":
            raise ValueError("Only hr_admin can approve/deny exceptions")

        exception.approval_status = request.approval_status
        exception.approved_by = request.actor_persona
        exception.approval_notes = request.approval_notes
        exception.approved_at = datetime.now(UTC)
        session.flush()

        # Log audit entry
        from db.repositories.audit_repository import AuditRepository

        audit_repo = AuditRepository()
        audit_repo.log_exception_decided(
            session=session,
            case_id=exception.case_id,
            exception_id=exception.exception_id,
            decision=request.approval_status,
            actor_persona=request.actor_persona,
            approval_notes=request.approval_notes,
        )

        return self._to_output(exception)

    def get_by_id(self, session: Session, exception_id: str) -> ExceptionOutput | None:
        """Get exception by ID.

        Args:
            session: Database session
            exception_id: Exception ID

        Returns:
            Exception details if found, None otherwise
        """
        exception = session.execute(
            select(CaseException).where(CaseException.exception_id == exception_id)
        ).scalar_one_or_none()

        if not exception:
            return None

        return self._to_output(exception)

    def list_for_case(self, session: Session, case_id: str) -> list[ExceptionOutput]:
        """List all exceptions for a case.

        Args:
            session: Database session
            case_id: Case ID

        Returns:
            List of exception details
        """
        exceptions = list(
            session.execute(
                select(CaseException)
                .where(CaseException.case_id == case_id)
                .order_by(CaseException.requested_at.desc())
            )
            .scalars()
            .all()
        )

        return [self._to_output(e) for e in exceptions]

    def list_pending(self, session: Session) -> list[ExceptionOutput]:
        """List all pending exception requests.

        Args:
            session: Database session

        Returns:
            List of pending exception details
        """
        exceptions = list(
            session.execute(
                select(CaseException)
                .where(CaseException.approval_status == "pending")
                .order_by(CaseException.requested_at.asc())  # Oldest first
            )
            .scalars()
            .all()
        )

        return [self._to_output(e) for e in exceptions]

    def _to_output(self, exception: CaseException) -> ExceptionOutput:
        """Convert ORM model to Pydantic output model."""
        return ExceptionOutput(
            exception_id=exception.exception_id,
            case_id=exception.case_id,
            milestone_type=exception.milestone_type,
            reason=exception.reason,
            affected_policy_refs=(
                json.loads(exception.affected_policy_refs) if exception.affected_policy_refs else []
            ),
            requested_by=exception.requested_by,
            requested_at=exception.requested_at.isoformat(),
            approval_status=exception.approval_status,
            approved_by=exception.approved_by,
            approval_notes=exception.approval_notes,
            approved_at=(exception.approved_at.isoformat() if exception.approved_at else None),
        )
