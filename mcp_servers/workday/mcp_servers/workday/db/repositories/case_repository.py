"""CaseRepository for managing pre-onboarding case CRUD operations.

This repository handles all database operations for cases, milestones,
tasks, and case-related entities in the V2 pre-onboarding coordination system.
"""

import json
from datetime import UTC, date, datetime
from uuid import uuid4

from models import (
    VALID_MILESTONE_TYPES,
    AssignOwnerInput,
    AuditEntryOutput,
    CaseDetailOutput,
    CaseOutput,
    CasePolicyLinkOutput,
    CaseSnapshotInput,
    CaseSnapshotOutput,
    CreateCaseInput,
    CreateTaskInput,
    GetCaseInput,
    HCMWorkerStateOutput,
    HCMWriteLogOutput,
    ListMilestonesInput,
    MilestoneListOutput,
    MilestoneOutput,
    PolicyRefOutput,
    SearchCasesInput,
    SearchCasesOutput,
    TaskOutput,
    UpdateCaseStatusInput,
    UpdateMilestoneInput,
    UpdateTaskInput,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import (
    AuditEntry,
    Case,
    CasePolicyLink,
    HCMWorkerState,
    HCMWriteLog,
    Milestone,
    PolicyReference,
    Task,
)


class CaseRepository:
    """Repository for case database operations."""

    def create(self, session: Session, request: CreateCaseInput) -> CaseOutput:
        """Create a new pre-onboarding case with default milestones.

        Args:
            session: Database session
            request: Case creation request

        Returns:
            Created case details with milestones

        Note:
            Does not commit the transaction. Caller is responsible for committing.
            Automatically creates the 4 standard milestones per BUILD_PLAN_v2.md.
        """
        # Create the case
        case = Case(
            case_id=request.case_id,
            candidate_id=request.candidate_id,
            requisition_id=request.requisition_id,
            role=request.role,
            country=request.country.upper(),
            employment_type=request.employment_type,
            owner_persona=request.owner_persona,
            proposed_start_date=request.proposed_start_date,
            due_date=request.due_date,
            notes=request.notes,
        )
        session.add(case)
        session.flush()

        # Create default milestones (per BUILD_PLAN_v2.md Section 5.2)
        milestones = []
        for milestone_type in VALID_MILESTONE_TYPES:
            milestone = Milestone(
                milestone_id=f"MILE-{request.case_id}-{milestone_type.upper()[:4]}",
                case_id=request.case_id,
                milestone_type=milestone_type,
            )
            session.add(milestone)
            milestones.append(milestone)
        session.flush()

        # Create audit entry for case creation
        from db.repositories.audit_repository import AuditRepository

        audit_repo = AuditRepository()
        audit_repo.log_case_created(
            session=session,
            case_id=request.case_id,
            actor_persona=request.owner_persona,
            details={
                "candidate_id": request.candidate_id,
                "role": request.role,
                "country": request.country.upper(),
                "employment_type": request.employment_type,
            },
        )

        return self._to_output(case, milestones)

    def get_by_id(self, session: Session, request: GetCaseInput) -> CaseDetailOutput | None:
        """Get case by ID with optional related data.

        Args:
            session: Database session
            request: Get case request

        Returns:
            Case details if found, None otherwise
        """
        case = session.execute(
            select(Case).where(Case.case_id == request.case_id)
        ).scalar_one_or_none()

        if not case:
            return None

        # Load milestones
        milestones = list(
            session.execute(select(Milestone).where(Milestone.case_id == request.case_id))
            .scalars()
            .all()
        )

        # Load tasks if requested
        tasks = None
        if request.include_tasks:
            tasks = list(
                session.execute(select(Task).where(Task.case_id == request.case_id)).scalars().all()
            )

        # Load audit trail if requested
        audit_entries = None
        if request.include_audit:
            audit_entries = list(
                session.execute(
                    select(AuditEntry)
                    .where(AuditEntry.case_id == request.case_id)
                    .order_by(AuditEntry.timestamp.desc())
                )
                .scalars()
                .all()
            )

        # Load attached policies
        policy_links = list(
            session.execute(select(CasePolicyLink).where(CasePolicyLink.case_id == request.case_id))
            .scalars()
            .all()
        )
        policy_ids = [link.policy_id for link in policy_links]
        policies = []
        if policy_ids:
            policies = list(
                session.execute(
                    select(PolicyReference).where(PolicyReference.policy_id.in_(policy_ids))
                )
                .scalars()
                .all()
            )

        return CaseDetailOutput(
            case=self._to_output(case, milestones),
            tasks=[self._task_to_output(t) for t in tasks] if tasks else None,
            audit_trail=(
                [self._audit_to_output(e) for e in audit_entries] if audit_entries else None
            ),
            policy_refs=[self._policy_to_output(p) for p in policies],
        )

    def search(self, session: Session, request: SearchCasesInput) -> SearchCasesOutput:
        """Search cases with filters and pagination.

        Args:
            session: Database session
            request: Search request with filters

        Returns:
            Paginated list of matching cases
        """
        # Build base query
        base_query = select(Case)

        # Apply filters
        if request.status:
            base_query = base_query.where(Case.status == request.status)
        if request.owner_persona:
            base_query = base_query.where(Case.owner_persona == request.owner_persona)
        if request.country:
            base_query = base_query.where(Case.country == request.country.upper())
        if request.role:
            base_query = base_query.where(Case.role.ilike(f"%{request.role}%"))
        if request.due_date_before:
            base_query = base_query.where(Case.due_date <= request.due_date_before)
        if request.due_date_after:
            base_query = base_query.where(Case.due_date >= request.due_date_after)

        # Get total count
        count_stmt = select(func.count()).select_from(base_query.subquery())
        total_count = session.execute(count_stmt).scalar_one()

        # Apply pagination with deterministic ordering
        offset = (request.page_number - 1) * request.page_size
        stmt = (
            base_query.order_by(Case.created_at.desc(), Case.case_id)
            .offset(offset)
            .limit(request.page_size)
        )

        cases = list(session.execute(stmt).scalars().all())

        # For each case, load milestones
        case_outputs = []
        for case in cases:
            milestones = list(
                session.execute(select(Milestone).where(Milestone.case_id == case.case_id))
                .scalars()
                .all()
            )
            case_outputs.append(self._to_output(case, milestones))

        return SearchCasesOutput(
            cases=case_outputs,
            total_count=total_count,
            page_size=request.page_size,
            page_number=request.page_number,
        )

    def update_status(
        self,
        session: Session,
        request: UpdateCaseStatusInput,
        valid_transitions: dict[str, list[str]],
    ) -> CaseOutput:
        """Update case status with audit logging.

        Args:
            session: Database session
            request: Status update request
            valid_transitions: Dict mapping current status to list of allowed next statuses

        Returns:
            Updated case details

        Raises:
            ValueError: If case not found or invalid transition
        """
        case = session.execute(
            select(Case).where(Case.case_id == request.case_id).with_for_update()
        ).scalar_one_or_none()

        if not case:
            raise ValueError(f"Case {request.case_id} not found")

        old_status = case.status

        # Validate transition under lock to prevent race conditions
        allowed_transitions = valid_transitions.get(old_status, [])
        if request.new_status not in allowed_transitions:
            raise ValueError(f"Invalid status transition from {old_status} to {request.new_status}")

        case.status = request.new_status
        session.flush()

        # Create audit entry
        from db.repositories.audit_repository import AuditRepository

        audit_repo = AuditRepository()
        audit_repo.log_action(
            session=session,
            case_id=request.case_id,
            action_type="status_updated",
            actor_persona=request.actor_persona,
            rationale=request.rationale,
            details={"old_status": old_status, "new_status": request.new_status},
        )

        milestones = list(
            session.execute(select(Milestone).where(Milestone.case_id == request.case_id))
            .scalars()
            .all()
        )

        return self._to_output(case, milestones)

    def assign_owner(self, session: Session, request: AssignOwnerInput) -> CaseOutput:
        """Assign case to new owner with audit logging.

        Args:
            session: Database session
            request: Owner assignment request

        Returns:
            Updated case details

        Raises:
            ValueError: If case not found
        """
        case = session.execute(
            select(Case).where(Case.case_id == request.case_id).with_for_update()
        ).scalar_one_or_none()

        if not case:
            raise ValueError(f"Case {request.case_id} not found")

        old_owner = case.owner_persona
        case.owner_persona = request.new_owner_persona
        session.flush()

        # Create audit entry
        from db.repositories.audit_repository import AuditRepository

        audit_repo = AuditRepository()
        audit_repo.log_action(
            session=session,
            case_id=request.case_id,
            action_type="owner_assigned",
            actor_persona=request.actor_persona,
            rationale=request.rationale,
            details={"old_owner": old_owner, "new_owner": request.new_owner_persona},
        )

        milestones = list(
            session.execute(select(Milestone).where(Milestone.case_id == request.case_id))
            .scalars()
            .all()
        )

        return self._to_output(case, milestones)

    # =========================================================================
    # MILESTONE OPERATIONS
    # =========================================================================

    def list_milestones(
        self, session: Session, request: ListMilestonesInput
    ) -> MilestoneListOutput:
        """List all milestones for a case.

        Args:
            session: Database session
            request: List milestones request

        Returns:
            Milestone list with counts
        """
        milestones = list(
            session.execute(select(Milestone).where(Milestone.case_id == request.case_id))
            .scalars()
            .all()
        )

        completed_count = sum(1 for m in milestones if m.status == "completed")
        pending_count = sum(1 for m in milestones if m.status == "pending")

        return MilestoneListOutput(
            milestones=[self._milestone_to_output(m) for m in milestones],
            total_count=len(milestones),
            completed_count=completed_count,
            pending_count=pending_count,
        )

    def update_milestone(
        self,
        session: Session,
        request: UpdateMilestoneInput,
        valid_transitions: dict[str, list[str]] | None = None,
    ) -> MilestoneOutput:
        """Update a milestone's status with audit logging.

        Args:
            session: Database session
            request: Milestone update request
            valid_transitions: Dict mapping current status to allowed next statuses.
                             If provided, validates under lock to prevent race conditions.

        Returns:
            Updated milestone details

        Raises:
            ValueError: If milestone not found or invalid transition
        """
        milestone = session.execute(
            select(Milestone)
            .where(
                Milestone.case_id == request.case_id,
                Milestone.milestone_type == request.milestone_type,
            )
            .with_for_update()
        ).scalar_one_or_none()

        if not milestone:
            raise ValueError(
                f"Milestone {request.milestone_type} not found for case {request.case_id}"
            )

        old_status = milestone.status

        # Validate transition under lock to prevent race conditions
        if valid_transitions is not None:
            allowed = valid_transitions.get(old_status, [])
            if request.new_status not in allowed:
                if not allowed:
                    raise ValueError(
                        f"Invalid status transition from '{old_status}' to '{request.new_status}'. "
                        f"Allowed transitions: none (terminal state)"
                    )
                raise ValueError(
                    f"Invalid status transition from '{old_status}' to '{request.new_status}'. "
                    f"Allowed transitions: {allowed}"
                )

        milestone.status = request.new_status
        if request.evidence_link:
            milestone.evidence_link = request.evidence_link
        if request.notes:
            milestone.notes = request.notes
        if request.new_status == "completed":
            from datetime import date

            milestone.completion_date = date.today().isoformat()
            milestone.completed_by = request.actor_persona
        session.flush()

        # Create audit entry
        from db.repositories.audit_repository import AuditRepository

        audit_repo = AuditRepository()
        audit_repo.log_action(
            session=session,
            case_id=request.case_id,
            action_type="milestone_updated",
            actor_persona=request.actor_persona,
            rationale=request.notes,
            evidence_links=[request.evidence_link] if request.evidence_link else None,
            details={
                "milestone_type": request.milestone_type,
                "old_status": old_status,
                "new_status": request.new_status,
            },
        )

        return self._milestone_to_output(milestone)

    # =========================================================================
    # TASK OPERATIONS
    # =========================================================================

    def create_task(self, session: Session, request: CreateTaskInput) -> TaskOutput:
        """Create a new task for a case.

        Args:
            session: Database session
            request: Task creation request

        Returns:
            Created task details

        Raises:
            ValueError: If case not found or milestone type invalid
        """
        # Verify case exists
        case = session.execute(
            select(Case).where(Case.case_id == request.case_id)
        ).scalar_one_or_none()

        if not case:
            raise ValueError(f"Case {request.case_id} not found")

        # Find milestone if specified
        milestone_id = None
        if request.milestone_type:
            milestone = session.execute(
                select(Milestone).where(
                    Milestone.case_id == request.case_id,
                    Milestone.milestone_type == request.milestone_type,
                )
            ).scalar_one_or_none()

            if not milestone:
                raise ValueError(f"Milestone {request.milestone_type} not found for case")
            milestone_id = milestone.milestone_id

        task = Task(
            task_id=f"TASK-{uuid4().hex[:8].upper()}",
            case_id=request.case_id,
            milestone_id=milestone_id,
            title=request.title,
            owner_persona=request.owner_persona,
            due_date=request.due_date,
            notes=request.notes,
        )
        session.add(task)
        session.flush()

        return self._task_to_output(task)

    def update_task(self, session: Session, request: UpdateTaskInput) -> TaskOutput:
        """Update a task.

        Args:
            session: Database session
            request: Task update request

        Returns:
            Updated task details

        Raises:
            ValueError: If task not found
        """
        task = session.execute(
            select(Task).where(Task.task_id == request.task_id).with_for_update()
        ).scalar_one_or_none()

        if not task:
            raise ValueError(f"Task {request.task_id} not found")

        old_status = task.status
        old_owner = task.owner_persona

        if request.new_status:
            task.status = request.new_status
        if request.new_owner_persona:
            task.owner_persona = request.new_owner_persona
        if request.notes:
            task.notes = request.notes
        session.flush()

        # Create audit entry for task update
        from db.repositories.audit_repository import AuditRepository

        audit_repo = AuditRepository()
        audit_repo.log_action(
            session=session,
            case_id=task.case_id,
            action_type="task_updated",
            actor_persona=request.actor_persona,
            rationale=request.notes,
            details={
                "task_id": request.task_id,
                "old_status": old_status,
                "new_status": task.status,
                "old_owner": old_owner,
                "new_owner": task.owner_persona,
            },
        )

        return self._task_to_output(task)

    # =========================================================================
    # POLICY OPERATIONS
    # =========================================================================

    def attach_policies(
        self,
        session: Session,
        case_id: str,
        policy_ids: list[str],
        decision_context: str,
        actor_persona: str,
    ) -> list[CasePolicyLinkOutput]:
        """Attach policies to a case.

        Args:
            session: Database session
            case_id: Case ID
            policy_ids: List of policy IDs to attach
            decision_context: Why these policies are relevant
            actor_persona: Persona attaching policies

        Returns:
            List of created links

        Raises:
            ValueError: If case or policy not found
        """
        # Verify case exists
        case = session.execute(select(Case).where(Case.case_id == case_id)).scalar_one_or_none()

        if not case:
            raise ValueError(f"Case {case_id} not found")

        # Verify all policies exist
        policies = list(
            session.execute(
                select(PolicyReference).where(PolicyReference.policy_id.in_(policy_ids))
            )
            .scalars()
            .all()
        )

        found_ids = {p.policy_id for p in policies}
        missing = set(policy_ids) - found_ids
        if missing:
            raise ValueError(f"Policies not found: {missing}")

        links = []
        for policy_id in policy_ids:
            link = CasePolicyLink(
                link_id=f"LINK-{uuid4().hex[:8].upper()}",
                case_id=case_id,
                policy_id=policy_id,
                attached_by=actor_persona,
                decision_context=decision_context,
            )
            session.add(link)
            links.append(link)
        session.flush()

        return [
            CasePolicyLinkOutput(
                link_id=link.link_id,
                case_id=link.case_id,
                policy_id=link.policy_id,
                attached_at=link.attached_at.isoformat(),
                attached_by=link.attached_by,
                decision_context=link.decision_context,
            )
            for link in links
        ]

    # =========================================================================
    # SNAPSHOT OPERATIONS
    # =========================================================================

    def get_case_snapshot(
        self, session: Session, request: CaseSnapshotInput
    ) -> CaseSnapshotOutput | None:
        """Get a complete point-in-time snapshot of a case.

        This includes full case details with milestones, tasks, audit trail,
        attached policy references, HCM worker state, and HCM write history.

        When as_of_date is provided, the HCM state is reconstructed by replaying
        write logs up to that date, and the write history is filtered to only
        include logs up to that date.

        Args:
            session: Database session
            request: Snapshot request with case_id and optional as_of_date

        Returns:
            Complete case snapshot or None if case not found
        """
        # Get full case details with tasks and audit
        case_request = GetCaseInput(
            case_id=request.case_id,
            include_tasks=True,
            include_audit=True,
        )
        case_detail = self.get_by_id(session, case_request)

        if not case_detail:
            return None

        # Get all attached policy references with full details
        policy_links = list(
            session.execute(select(CasePolicyLink).where(CasePolicyLink.case_id == request.case_id))
            .scalars()
            .all()
        )
        policy_ids = [link.policy_id for link in policy_links]

        policy_references = []
        if policy_ids:
            policies = list(
                session.execute(
                    select(PolicyReference).where(PolicyReference.policy_id.in_(policy_ids))
                )
                .scalars()
                .all()
            )
            policy_references = [self._policy_to_output(p) for p in policies]

        # Parse as_of_date if provided
        as_of_datetime = None
        if request.as_of_date:
            # Convert date string to end-of-day datetime for comparison
            # Note: HCMWriteLog.timestamp is stored as naive datetime, so we create
            # a naive datetime here to avoid TypeError when comparing
            as_of_date_parsed = date.fromisoformat(request.as_of_date)
            as_of_datetime = datetime(
                as_of_date_parsed.year,
                as_of_date_parsed.month,
                as_of_date_parsed.day,
                23,
                59,
                59,
                999999,
            )

        # Get HCM write logs (ordered chronologically for replay)
        hcm_write_logs = list(
            session.execute(
                select(HCMWriteLog)
                .where(HCMWriteLog.case_id == request.case_id)
                .order_by(HCMWriteLog.timestamp.asc())  # Chronological for replay
            )
            .scalars()
            .all()
        )

        # Filter logs by as_of_date if provided
        if as_of_datetime:
            hcm_write_logs = [log for log in hcm_write_logs if log.timestamp <= as_of_datetime]

        # Reconstruct HCM state from write logs if as_of_date is provided,
        # otherwise get current state from database
        hcm_state = None
        if request.as_of_date:
            hcm_state = self._reconstruct_hcm_state_from_logs(
                case_id=request.case_id,
                write_logs=hcm_write_logs,
            )
        else:
            # Get current HCM worker state from database
            hcm_worker_state = session.execute(
                select(HCMWorkerState).where(HCMWorkerState.case_id == request.case_id)
            ).scalar_one_or_none()

            if hcm_worker_state:
                hcm_state = HCMWorkerStateOutput(
                    worker_id=hcm_worker_state.worker_id,
                    case_id=hcm_worker_state.case_id,
                    onboarding_status=hcm_worker_state.onboarding_status,
                    onboarding_readiness=bool(hcm_worker_state.onboarding_readiness),
                    proposed_start_date=hcm_worker_state.proposed_start_date,
                    confirmed_start_date=hcm_worker_state.confirmed_start_date,
                    hire_finalized=bool(hcm_worker_state.hire_finalized),
                    effective_date=hcm_worker_state.effective_date,
                    created_at=hcm_worker_state.created_at.isoformat(),
                    updated_at=hcm_worker_state.updated_at.isoformat(),
                )

        # Convert logs to output format (reverse to show most recent first)
        hcm_write_history = [
            HCMWriteLogOutput(
                log_id=log.log_id,
                case_id=log.case_id,
                worker_id=log.worker_id,
                write_type=log.write_type,
                old_value=json.loads(log.old_value) if log.old_value else None,
                new_value=json.loads(log.new_value) if log.new_value else {},
                actor_persona=log.actor_persona,
                policy_refs=json.loads(log.policy_refs) if log.policy_refs else [],
                milestone_evidence=json.loads(log.milestone_evidence)
                if log.milestone_evidence
                else [],
                rationale=log.rationale,
                timestamp=log.timestamp.isoformat(),
            )
            for log in reversed(hcm_write_logs)  # Most recent first
        ]

        return CaseSnapshotOutput(
            case=case_detail,
            policy_references=policy_references,
            hcm_state=hcm_state,
            hcm_write_history=hcm_write_history,
            snapshot_timestamp=datetime.now(UTC).isoformat(),
        )

    def _reconstruct_hcm_state_from_logs(
        self,
        case_id: str,
        write_logs: list[HCMWriteLog],
    ) -> HCMWorkerStateOutput | None:
        """Reconstruct HCM worker state by replaying write logs.

        This method replays all write logs in chronological order to reconstruct
        what the HCM state would have looked like at the given point in time.

        Args:
            case_id: Case ID
            write_logs: List of write logs ordered chronologically (oldest first),
                        already filtered by as_of_date

        Returns:
            Reconstructed HCM state, or None if no logs exist
        """
        if not write_logs:
            return None

        # Initialize state from first log
        first_log = write_logs[0]
        state = {
            "worker_id": first_log.worker_id,
            "case_id": case_id,
            "onboarding_status": None,
            "onboarding_readiness": False,
            "proposed_start_date": None,
            "confirmed_start_date": None,
            "hire_finalized": False,
            "effective_date": None,
            "created_at": first_log.timestamp.isoformat(),
            "updated_at": first_log.timestamp.isoformat(),
        }

        # Replay each log to build up state
        for log in write_logs:
            new_value = json.loads(log.new_value) if log.new_value else {}
            state["updated_at"] = log.timestamp.isoformat()

            if log.write_type == "confirm_start_date":
                state["confirmed_start_date"] = new_value.get("confirmed_start_date")
                # First confirm_start_date sets status to in_progress
                if state["onboarding_status"] is None:
                    state["onboarding_status"] = "in_progress"

            elif log.write_type == "update_readiness":
                readiness = new_value.get("onboarding_readiness", False)
                state["onboarding_readiness"] = readiness
                if readiness:
                    state["onboarding_status"] = "ready"
                else:
                    state["onboarding_status"] = "in_progress"

            elif log.write_type == "finalize_hire":
                state["hire_finalized"] = True
                state["onboarding_status"] = "finalized"
                state["effective_date"] = new_value.get("effective_date")

        return HCMWorkerStateOutput(
            worker_id=state["worker_id"],
            case_id=state["case_id"],
            onboarding_status=state["onboarding_status"],
            onboarding_readiness=state["onboarding_readiness"],
            proposed_start_date=state["proposed_start_date"],
            confirmed_start_date=state["confirmed_start_date"],
            hire_finalized=state["hire_finalized"],
            effective_date=state["effective_date"],
            created_at=state["created_at"],
            updated_at=state["updated_at"],
        )

    # =========================================================================
    # HELPER METHODS
    # =========================================================================

    def _to_output(self, case: Case, milestones: list[Milestone]) -> CaseOutput:
        """Convert ORM model to Pydantic output model."""
        return CaseOutput(
            case_id=case.case_id,
            candidate_id=case.candidate_id,
            requisition_id=case.requisition_id,
            role=case.role,
            country=case.country,
            employment_type=case.employment_type,
            owner_persona=case.owner_persona,
            status=case.status,
            proposed_start_date=case.proposed_start_date,
            confirmed_start_date=case.confirmed_start_date,
            due_date=case.due_date,
            milestones=[self._milestone_to_output(m) for m in milestones],
            created_at=case.created_at.isoformat(),
            updated_at=case.updated_at.isoformat(),
        )

    def _milestone_to_output(self, milestone: Milestone) -> MilestoneOutput:
        """Convert ORM milestone to Pydantic output model."""
        return MilestoneOutput(
            milestone_id=milestone.milestone_id,
            case_id=milestone.case_id,
            milestone_type=milestone.milestone_type,
            status=milestone.status,
            evidence_link=milestone.evidence_link,
            completion_date=milestone.completion_date,
            completed_by=milestone.completed_by,
            notes=milestone.notes,
            created_at=milestone.created_at.isoformat(),
            updated_at=milestone.updated_at.isoformat(),
        )

    def _task_to_output(self, task: Task) -> TaskOutput:
        """Convert ORM task to Pydantic output model."""
        return TaskOutput(
            task_id=task.task_id,
            case_id=task.case_id,
            milestone_id=task.milestone_id,
            title=task.title,
            owner_persona=task.owner_persona,
            due_date=task.due_date,
            status=task.status,
            notes=task.notes,
            created_at=task.created_at.isoformat(),
            updated_at=task.updated_at.isoformat(),
        )

    def _audit_to_output(self, entry: AuditEntry) -> AuditEntryOutput:
        """Convert ORM audit entry to Pydantic output model."""
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

    def _policy_to_output(self, policy: PolicyReference) -> PolicyRefOutput:
        """Convert ORM policy to Pydantic output model."""
        return PolicyRefOutput(
            policy_id=policy.policy_id,
            country=policy.country,
            role=policy.role,
            employment_type=policy.employment_type,
            policy_type=policy.policy_type,
            lead_time_days=policy.lead_time_days,
            content=json.loads(policy.content),
            effective_date=policy.effective_date,
            version=policy.version,
            created_at=policy.created_at.isoformat(),
        )
