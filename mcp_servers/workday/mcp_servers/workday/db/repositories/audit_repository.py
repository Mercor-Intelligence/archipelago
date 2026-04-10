"""AuditRepository for append-only audit logging.

This repository handles all audit trail operations for the V2 pre-onboarding
coordination system. Audit entries are immutable once created.
"""

import json
from datetime import datetime, timedelta
from uuid import uuid4

from models import AuditEntryOutput, AuditHistoryOutput, GetAuditHistoryInput
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import AuditEntry


class AuditRepository:
    """Repository for audit logging operations.

    The audit log is append-only - entries cannot be modified or deleted.
    All significant actions on cases should be logged through this repository.
    """

    def log_action(
        self,
        session: Session,
        case_id: str,
        action_type: str,
        actor_persona: str,
        rationale: str | None = None,
        policy_refs: list[str] | None = None,
        evidence_links: list[str] | None = None,
        details: dict | None = None,
    ) -> AuditEntryOutput:
        """Log an action to the audit trail.

        Args:
            session: Database session
            case_id: Case ID this action applies to
            action_type: Type of action (e.g., case_created, status_updated)
            actor_persona: Persona who performed the action
            rationale: Optional reason for the action
            policy_refs: Optional list of policy IDs referenced
            evidence_links: Optional list of evidence URLs
            details: Optional action-specific details (JSON serializable)

        Returns:
            Created audit entry

        Note:
            Does not commit the transaction. Caller is responsible for committing.
        """
        entry = AuditEntry(
            entry_id=f"AUDIT-{uuid4().hex[:12].upper()}",
            case_id=case_id,
            action_type=action_type,
            actor_persona=actor_persona,
            rationale=rationale,
            policy_refs=json.dumps(policy_refs) if policy_refs else None,
            evidence_links=json.dumps(evidence_links) if evidence_links else None,
            details=json.dumps(details) if details else None,
        )
        session.add(entry)
        session.flush()

        return self._to_output(entry)

    def get_history(self, session: Session, request: GetAuditHistoryInput) -> AuditHistoryOutput:
        """Get audit history for a case with optional filters.

        Args:
            session: Database session
            request: Audit history request with filters

        Returns:
            Filtered audit history
        """
        # Build query
        query = select(AuditEntry).where(AuditEntry.case_id == request.case_id)

        # Apply filters
        if request.action_type:
            query = query.where(AuditEntry.action_type == request.action_type)
        if request.actor_persona:
            query = query.where(AuditEntry.actor_persona == request.actor_persona)
        if request.start_date:
            start_dt = datetime.fromisoformat(request.start_date)
            query = query.where(AuditEntry.timestamp >= start_dt)
        if request.end_date:
            end_dt = datetime.fromisoformat(request.end_date)
            # If date-only string (no time component), add 1 day for inclusive behavior
            # e.g., "2025-01-15" should include all entries on Jan 15th
            if "T" not in request.end_date:
                end_dt = end_dt + timedelta(days=1)
                query = query.where(AuditEntry.timestamp < end_dt)
            else:
                query = query.where(AuditEntry.timestamp <= end_dt)

        # Order by timestamp descending (most recent first)
        query = query.order_by(AuditEntry.timestamp.desc())

        entries = list(session.execute(query).scalars().all())

        return AuditHistoryOutput(
            entries=[self._to_output(e) for e in entries],
            total_count=len(entries),
        )

    def log_case_created(
        self,
        session: Session,
        case_id: str,
        actor_persona: str,
        details: dict | None = None,
    ) -> AuditEntryOutput:
        """Log a case creation event.

        Args:
            session: Database session
            case_id: Created case ID
            actor_persona: Persona who created the case
            details: Optional case creation details

        Returns:
            Created audit entry
        """
        return self.log_action(
            session=session,
            case_id=case_id,
            action_type="case_created",
            actor_persona=actor_persona,
            rationale="Case created",
            details=details,
        )

    def log_milestone_updated(
        self,
        session: Session,
        case_id: str,
        milestone_type: str,
        old_status: str,
        new_status: str,
        actor_persona: str,
        evidence_link: str | None = None,
        rationale: str | None = None,
    ) -> AuditEntryOutput:
        """Log a milestone status update.

        Args:
            session: Database session
            case_id: Case ID
            milestone_type: Type of milestone updated
            old_status: Previous status
            new_status: New status
            actor_persona: Persona who made the update
            evidence_link: Optional evidence URL
            rationale: Optional reason for update

        Returns:
            Created audit entry
        """
        return self.log_action(
            session=session,
            case_id=case_id,
            action_type="milestone_updated",
            actor_persona=actor_persona,
            rationale=rationale,
            evidence_links=[evidence_link] if evidence_link else None,
            details={
                "milestone_type": milestone_type,
                "old_status": old_status,
                "new_status": new_status,
            },
        )

    def log_hcm_write(
        self,
        session: Session,
        case_id: str,
        write_type: str,
        actor_persona: str,
        policy_refs: list[str],
        evidence_links: list[str],
        rationale: str,
        old_value: dict | None = None,
        new_value: dict | None = None,
    ) -> AuditEntryOutput:
        """Log an HCM write-back event.

        Args:
            session: Database session
            case_id: Case ID
            write_type: Type of HCM write (confirm_start_date, update_readiness, etc.)
            actor_persona: Persona who performed the write
            policy_refs: Policy IDs justifying the write
            evidence_links: Evidence links supporting the write
            rationale: Reason for the write
            old_value: Previous value (if applicable)
            new_value: New value being written

        Returns:
            Created audit entry
        """
        return self.log_action(
            session=session,
            case_id=case_id,
            action_type=f"hcm_write_{write_type}",
            actor_persona=actor_persona,
            rationale=rationale,
            policy_refs=policy_refs,
            evidence_links=evidence_links,
            details={
                "write_type": write_type,
                "old_value": old_value,
                "new_value": new_value,
            },
        )

    def log_exception_requested(
        self,
        session: Session,
        case_id: str,
        exception_id: str,
        milestone_type: str,
        actor_persona: str,
        reason: str,
        affected_policies: list[str] | None = None,
    ) -> AuditEntryOutput:
        """Log an exception request.

        Args:
            session: Database session
            case_id: Case ID
            exception_id: Created exception ID
            milestone_type: Milestone requiring exception
            actor_persona: Persona requesting exception
            reason: Reason for exception request
            affected_policies: Policies being excepted

        Returns:
            Created audit entry
        """
        return self.log_action(
            session=session,
            case_id=case_id,
            action_type="exception_requested",
            actor_persona=actor_persona,
            rationale=reason,
            policy_refs=affected_policies,
            details={
                "exception_id": exception_id,
                "milestone_type": milestone_type,
            },
        )

    def log_exception_decided(
        self,
        session: Session,
        case_id: str,
        exception_id: str,
        decision: str,
        actor_persona: str,
        approval_notes: str,
    ) -> AuditEntryOutput:
        """Log an exception approval or denial.

        Args:
            session: Database session
            case_id: Case ID
            exception_id: Exception ID
            decision: Decision (approved or denied)
            actor_persona: Persona making the decision
            approval_notes: Notes explaining the decision

        Returns:
            Created audit entry
        """
        return self.log_action(
            session=session,
            case_id=case_id,
            action_type=f"exception_{decision}",
            actor_persona=actor_persona,
            rationale=approval_notes,
            details={"exception_id": exception_id, "decision": decision},
        )

    def _to_output(self, entry: AuditEntry) -> AuditEntryOutput:
        """Convert ORM model to Pydantic output model."""
        return AuditEntryOutput(
            entry_id=entry.entry_id,
            case_id=entry.case_id,
            action_type=entry.action_type,
            actor_persona=entry.actor_persona,
            rationale=entry.rationale,
            policy_refs=json.loads(entry.policy_refs) if entry.policy_refs else [],
            evidence_links=(json.loads(entry.evidence_links) if entry.evidence_links else []),
            details=json.loads(entry.details) if entry.details else None,
            timestamp=entry.timestamp.isoformat(),
        )
