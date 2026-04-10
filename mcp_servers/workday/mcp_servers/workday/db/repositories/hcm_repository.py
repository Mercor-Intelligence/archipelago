"""HCMRepository for managing gated HCM state write-backs.

This repository handles the gated write-back pattern for HCM state changes.
All writes are validated against policy requirements and logged immutably.
"""

import json
from datetime import UTC, datetime
from uuid import uuid4

from models import (
    ConfirmStartDateInput,
    ConfirmStartDateOutput,
    GatingCheckResult,
    HCMContextOutput,
    HCMWorkerStateOutput,
    HCMWriteLogOutput,
    PositionContextOutput,
    UpdateReadinessInput,
    UpdateReadinessOutput,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import (
    Case,
    CaseException,
    HCMWorkerState,
    HCMWriteLog,
    Milestone,
    PayrollCutoff,
    PolicyReference,
)


class HCMRepository:
    """Repository for HCM state management with gated write-backs.

    All write operations are subject to gating checks based on:
    - Persona authorization
    - Policy reference validation
    - Milestone completion status
    - Lead time requirements
    - Payroll cutoff constraints
    """

    def _check_authorization(self, actor_persona: str, allowed_personas: list[str]) -> None:
        """Verify actor has required persona for gated operations.

        Args:
            actor_persona: Persona attempting the operation
            allowed_personas: List of personas authorized for this operation

        Raises:
            ValueError: E_AUTH_001 if persona not authorized
        """
        if actor_persona not in allowed_personas:
            raise ValueError(
                f"E_AUTH_001: Persona '{actor_persona}' not authorized for this operation. "
                f"Allowed: {allowed_personas}"
            )

    def _validate_policy_refs(self, session: Session, policy_refs: list[str]) -> None:
        """Verify policy references are provided and exist in the database.

        Per BUILD_PLAN_v2.md § 3.5:
        - At least one policy_ref must be attached
        - Validate all provided policy_refs exist

        Args:
            session: Database session
            policy_refs: List of policy IDs

        Raises:
            ValueError: E_POLICY_002 if no policy references provided
            ValueError: E_POLICY_001 if any policy reference not found
        """
        if not policy_refs or len(policy_refs) == 0:
            raise ValueError(
                "E_POLICY_002: At least one policy reference is required for "
                "gated write-back operations"
            )

        # Validate all policy refs exist
        existing_policies = (
            session.execute(
                select(PolicyReference.policy_id).where(PolicyReference.policy_id.in_(policy_refs))
            )
            .scalars()
            .all()
        )
        existing_set = set(existing_policies)
        missing = [p for p in policy_refs if p not in existing_set]
        if missing:
            raise ValueError(f"E_POLICY_001: Policy reference(s) not found: {missing}")

    def _has_approved_exception(self, session: Session, case_id: str, milestone_type: str) -> bool:
        """Check if milestone has an approved exception.

        Per spec: milestones can pass gating if completed, waived, OR have
        an approved exception on file.

        Args:
            session: Database session
            case_id: Case ID
            milestone_type: Type of milestone to check

        Returns:
            True if at least one approved exception exists for this milestone
        """
        exception = (
            session.execute(
                select(CaseException).where(
                    CaseException.case_id == case_id,
                    CaseException.milestone_type == milestone_type,
                    CaseException.approval_status == "approved",
                )
            )
            .scalars()
            .first()
        )
        return exception is not None

    def get_hcm_context(self, session: Session, case_id: str) -> HCMContextOutput | None:
        """Get HCM context for a case.

        Args:
            session: Database session
            case_id: Case ID

        Returns:
            HCM context if state exists, None otherwise
        """
        state = session.execute(
            select(HCMWorkerState).where(HCMWorkerState.case_id == case_id)
        ).scalar_one_or_none()

        if not state:
            return None

        return HCMContextOutput(
            case_id=state.case_id,
            worker_id=state.worker_id,
            onboarding_status=state.onboarding_status,
            onboarding_readiness=bool(state.onboarding_readiness),
            proposed_start_date=state.proposed_start_date,
            confirmed_start_date=state.confirmed_start_date,
            hire_finalized=bool(state.hire_finalized),
            last_updated=state.updated_at.isoformat(),
        )

    def get_position_context(self, session: Session, case_id: str) -> PositionContextOutput | None:
        """Get position context with derived policy requirements.

        Args:
            session: Database session
            case_id: Case ID

        Returns:
            Position context with policy requirements, None if case not found
        """
        case = session.execute(select(Case).where(Case.case_id == case_id)).scalar_one_or_none()

        if not case:
            return None

        # Get required milestones (all 4 are required by default)
        from models import VALID_MILESTONE_TYPES

        required_milestones = list(VALID_MILESTONE_TYPES)

        # Look up lead time policy for country/role (most recent effective date)
        lead_time_policy = (
            session.execute(
                select(PolicyReference)
                .where(
                    PolicyReference.country == case.country,
                    PolicyReference.policy_type == "lead_times",
                )
                .order_by(PolicyReference.effective_date.desc())
            )
            .scalars()
            .first()
        )

        minimum_lead_time = None
        if lead_time_policy and lead_time_policy.lead_time_days:
            minimum_lead_time = lead_time_policy.lead_time_days

        # Look up payroll cutoff for country (most recent effective date)
        payroll_cutoff = (
            session.execute(
                select(PayrollCutoff)
                .where(PayrollCutoff.country == case.country)
                .order_by(PayrollCutoff.effective_date.desc())
            )
            .scalars()
            .first()
        )

        cutoff_day = None
        if payroll_cutoff:
            cutoff_day = payroll_cutoff.cutoff_day_of_month

        return PositionContextOutput(
            case_id=case_id,
            role=case.role,
            country=case.country,
            employment_type=case.employment_type,
            required_milestones=required_milestones,
            minimum_lead_time_days=minimum_lead_time,
            payroll_cutoff_day=cutoff_day,
        )

    def confirm_start_date(
        self, session: Session, request: ConfirmStartDateInput
    ) -> ConfirmStartDateOutput:
        """Confirm a start date with gating checks.

        This is a gated write-back operation. The start date is only
        confirmed if all gating checks pass:
        1. Actor persona is authorized (pre_onboarding_coordinator or hr_admin)
        2. At least one policy reference is provided
        3. All required milestones are completed or waived
        4. Lead time requirement is met
        5. Payroll cutoff is respected

        Args:
            session: Database session
            request: Start date confirmation request

        Returns:
            Confirmation result with gating check details

        Raises:
            ValueError: E_AUTH_001 if persona not authorized
            ValueError: E_POLICY_002 if no policy references provided
            ValueError: E_CASE_001 if case not found
            ValueError: If gating checks fail
        """
        # STEP 1: Check persona authorization
        self._check_authorization(
            request.actor_persona, allowed_personas=["pre_onboarding_coordinator", "hr_admin"]
        )

        # STEP 2: Validate policy references exist
        self._validate_policy_refs(session, request.policy_refs)

        # STEP 3: Lock case and proceed with existing logic
        case = session.execute(
            select(Case).where(Case.case_id == request.case_id).with_for_update()
        ).scalar_one_or_none()

        if not case:
            raise ValueError(f"E_CASE_001: Case {request.case_id} not found")

        # Run gating checks
        gating_checks = self._run_gating_checks(
            session=session,
            case=case,
            confirmed_start_date=request.confirmed_start_date,
        )

        all_passed = all(check.passed for check in gating_checks)

        if not all_passed:
            failed_checks = [c for c in gating_checks if not c.passed]
            # Raise specific error code based on first failed check (fail-closed)
            for check in failed_checks:
                if check.check_name == "milestones_complete":
                    raise ValueError(f"E_GATE_001: {check.details}")
                elif check.check_name == "lead_time_valid":
                    raise ValueError(f"E_GATE_003: {check.details}")
                elif check.check_name == "payroll_cutoff_valid":
                    raise ValueError(f"E_GATE_004: {check.details}")
            # Fallback for unknown check types
            raise ValueError(f"Gating checks failed: {[c.check_name for c in failed_checks]}")

        # Get or create HCM state
        state = session.execute(
            select(HCMWorkerState)
            .where(HCMWorkerState.case_id == request.case_id)
            .with_for_update()
        ).scalar_one_or_none()

        old_value = None
        if state:
            old_value = {"confirmed_start_date": state.confirmed_start_date}
            state.confirmed_start_date = request.confirmed_start_date
        else:
            worker_id = f"WRK-{uuid4().hex[:8].upper()}"
            state = HCMWorkerState(
                worker_id=worker_id,
                case_id=request.case_id,
                onboarding_status="in_progress",
                confirmed_start_date=request.confirmed_start_date,
            )
            session.add(state)

        # Update case
        case.confirmed_start_date = request.confirmed_start_date
        session.flush()

        # Create HCM write log
        write_log = HCMWriteLog(
            log_id=f"LOG-{uuid4().hex[:8].upper()}",
            case_id=request.case_id,
            worker_id=state.worker_id,
            write_type="confirm_start_date",
            old_value=json.dumps(old_value) if old_value else None,
            new_value=json.dumps({"confirmed_start_date": request.confirmed_start_date}),
            actor_persona=request.actor_persona,
            policy_refs=json.dumps(request.policy_refs),
            milestone_evidence=json.dumps(request.evidence_links),
            rationale=request.rationale,
        )
        session.add(write_log)
        session.flush()

        # Log audit entry
        from db.repositories.audit_repository import AuditRepository

        audit_repo = AuditRepository()
        audit_repo.log_hcm_write(
            session=session,
            case_id=request.case_id,
            write_type="confirm_start_date",
            actor_persona=request.actor_persona,
            policy_refs=request.policy_refs,
            evidence_links=request.evidence_links,
            rationale=request.rationale,
            old_value=old_value,
            new_value={"confirmed_start_date": request.confirmed_start_date},
        )

        return ConfirmStartDateOutput(
            success=True,
            case_id=request.case_id,
            confirmed_start_date=request.confirmed_start_date,
            gating_checks=gating_checks,
            hcm_write_id=write_log.log_id,
            timestamp=datetime.now(UTC).isoformat(),
        )

    def update_readiness(
        self, session: Session, request: UpdateReadinessInput
    ) -> UpdateReadinessOutput:
        """Update onboarding readiness flag.

        When setting readiness to True, enforces gating checks:
        - Actor persona must be authorized (pre_onboarding_coordinator or hr_admin)
        - At least one policy reference required
        - All required milestones must be completed, waived, or have approved exception

        When setting readiness to False, no gating checks are performed (allows any persona).

        Args:
            session: Database session
            request: Readiness update request

        Returns:
            Update result

        Raises:
            ValueError: E_AUTH_001 if setting readiness=True and persona not authorized
            ValueError: E_POLICY_002 if setting readiness=True and no policy references provided
            ValueError: E_CASE_001 if case not found
            ValueError: E_GATE_001 if setting readiness=True but milestones incomplete
        """
        # STEP 1: Get case
        case = session.execute(
            select(Case).where(Case.case_id == request.case_id)
        ).scalar_one_or_none()

        if not case:
            raise ValueError(f"E_CASE_001: Case {request.case_id} not found")

        # STEP 2: If setting readiness=true, run gating checks (auth, policy, milestones)
        if request.onboarding_readiness:
            # Check persona authorization
            self._check_authorization(
                request.actor_persona, allowed_personas=["pre_onboarding_coordinator", "hr_admin"]
            )

            # Validate policy references exist
            self._validate_policy_refs(session, request.policy_refs)

            # Check milestone completion (no lead time/payroll checks needed)
            from models import VALID_MILESTONE_TYPES

            milestones = list(
                session.execute(select(Milestone).where(Milestone.case_id == case.case_id))
                .scalars()
                .all()
            )
            existing_types = {m.milestone_type for m in milestones}
            missing_types = set(VALID_MILESTONE_TYPES) - existing_types

            # Check incomplete milestones, but allow if they have approved exceptions
            incomplete = []
            for m in milestones:
                if m.status not in ("completed", "waived"):
                    # Check if there's an approved exception for this milestone
                    if not self._has_approved_exception(session, case.case_id, m.milestone_type):
                        incomplete.append(m.milestone_type)

            all_complete = len(missing_types) == 0 and len(milestones) > 0 and len(incomplete) == 0

            if not all_complete:
                if missing_types:
                    raise ValueError(
                        f"E_GATE_001: Cannot set readiness - missing milestone types: "
                        f"{sorted(missing_types)}"
                    )
                elif incomplete:
                    raise ValueError(
                        f"E_GATE_001: Cannot set readiness - incomplete milestones: {incomplete}"
                    )

        # STEP 3: Get or create HCM state

        state = session.execute(
            select(HCMWorkerState)
            .where(HCMWorkerState.case_id == request.case_id)
            .with_for_update()
        ).scalar_one_or_none()

        if not state:
            raise ValueError(
                f"HCM state not found for case {request.case_id} - confirm start date first"
            )

        old_value = {"onboarding_readiness": bool(state.onboarding_readiness)}
        state.onboarding_readiness = 1 if request.onboarding_readiness else 0

        if request.onboarding_readiness:
            state.onboarding_status = "ready"
        else:
            # Revert status to in_progress when readiness is unset
            state.onboarding_status = "in_progress"
        session.flush()

        # Create HCM write log
        write_log = HCMWriteLog(
            log_id=f"LOG-{uuid4().hex[:8].upper()}",
            case_id=request.case_id,
            worker_id=state.worker_id,
            write_type="update_readiness",
            old_value=json.dumps(old_value),
            new_value=json.dumps({"onboarding_readiness": request.onboarding_readiness}),
            actor_persona=request.actor_persona,
            policy_refs=json.dumps(request.policy_refs),
            milestone_evidence=json.dumps(request.evidence_links),
            rationale=request.rationale,
        )
        session.add(write_log)
        session.flush()

        # Log audit entry
        from db.repositories.audit_repository import AuditRepository

        audit_repo = AuditRepository()
        audit_repo.log_hcm_write(
            session=session,
            case_id=request.case_id,
            write_type="update_readiness",
            actor_persona=request.actor_persona,
            policy_refs=request.policy_refs,
            evidence_links=request.evidence_links,
            rationale=request.rationale,
            old_value=old_value,
            new_value={"onboarding_readiness": request.onboarding_readiness},
        )

        return UpdateReadinessOutput(
            success=True,
            case_id=request.case_id,
            onboarding_readiness=request.onboarding_readiness,
            hcm_write_id=write_log.log_id,
            timestamp=datetime.now(UTC).isoformat(),
        )

    def get_write_history(self, session: Session, case_id: str) -> list[HCMWriteLogOutput]:
        """Get HCM write history for a case.

        Args:
            session: Database session
            case_id: Case ID

        Returns:
            List of write log entries
        """
        logs = list(
            session.execute(
                select(HCMWriteLog)
                .where(HCMWriteLog.case_id == case_id)
                .order_by(HCMWriteLog.timestamp.desc())
            )
            .scalars()
            .all()
        )

        return [self._log_to_output(log) for log in logs]

    def get_state(self, session: Session, case_id: str) -> HCMWorkerStateOutput | None:
        """Get HCM worker state for a case.

        Args:
            session: Database session
            case_id: Case ID

        Returns:
            Worker state if exists, None otherwise
        """
        state = session.execute(
            select(HCMWorkerState).where(HCMWorkerState.case_id == case_id)
        ).scalar_one_or_none()

        if not state:
            return None

        return self._state_to_output(state)

    def _run_gating_checks(
        self, session: Session, case: Case, confirmed_start_date: str
    ) -> list[GatingCheckResult]:
        """Run all gating checks for a start date confirmation.

        Args:
            session: Database session
            case: Case ORM object
            confirmed_start_date: Proposed start date

        Returns:
            List of gating check results
        """
        checks = []

        # Check 1: Milestones complete (all 4 required types, complete/waived/excepted)
        from models import VALID_MILESTONE_TYPES

        milestones = list(
            session.execute(select(Milestone).where(Milestone.case_id == case.case_id))
            .scalars()
            .all()
        )
        existing_types = {m.milestone_type for m in milestones}
        missing_types = set(VALID_MILESTONE_TYPES) - existing_types

        # Check incomplete milestones, but allow if they have approved exceptions
        incomplete = []
        for m in milestones:
            if m.status not in ("completed", "waived"):
                # Check if there's an approved exception for this milestone
                if not self._has_approved_exception(session, case.case_id, m.milestone_type):
                    incomplete.append(m.milestone_type)

        # All required milestone types must exist AND all must be completed/waived/excepted
        all_complete = len(missing_types) == 0 and len(milestones) > 0 and len(incomplete) == 0

        if missing_types:
            details = f"Missing milestone types: {sorted(missing_types)}"
        elif incomplete:
            details = f"Incomplete milestones: {incomplete}"
        else:
            details = "All milestones completed, waived, or have approved exceptions"

        checks.append(
            GatingCheckResult(
                check_name="milestones_complete",
                passed=all_complete,
                details=details,
            )
        )

        # Check 2: Lead time requirement (most recent effective date)
        lead_time_policy = (
            session.execute(
                select(PolicyReference)
                .where(
                    PolicyReference.country == case.country,
                    PolicyReference.policy_type == "lead_times",
                )
                .order_by(PolicyReference.effective_date.desc())
            )
            .scalars()
            .first()
        )

        if lead_time_policy and lead_time_policy.lead_time_days:
            from datetime import date

            start_date = date.fromisoformat(confirmed_start_date)
            today = date.today()
            days_until_start = (start_date - today).days
            required_days = lead_time_policy.lead_time_days
            lead_time_ok = days_until_start >= required_days

            checks.append(
                GatingCheckResult(
                    check_name="lead_time_valid",
                    passed=lead_time_ok,
                    details=(
                        f"Lead time met: {days_until_start} >= {required_days} days"
                        if lead_time_ok
                        else f"Insufficient lead time: {days_until_start} < {required_days} days"
                    ),
                )
            )
        else:
            checks.append(
                GatingCheckResult(
                    check_name="lead_time_valid",
                    passed=True,
                    details="No lead time policy found for country",
                )
            )

        # Check 3: Payroll cutoff (most recent effective date)
        payroll_cutoff = (
            session.execute(
                select(PayrollCutoff)
                .where(PayrollCutoff.country == case.country)
                .order_by(PayrollCutoff.effective_date.desc())
            )
            .scalars()
            .first()
        )

        if payroll_cutoff:
            from calendar import monthrange
            from datetime import date, timedelta

            start_date = date.fromisoformat(confirmed_start_date)
            cutoff_day = payroll_cutoff.cutoff_day_of_month
            processing_days = (
                payroll_cutoff.processing_days if payroll_cutoff.processing_days is not None else 5
            )
            today = date.today()

            # Determine cutoff date for the start date's month
            # (clamp cutoff_day to the last day of the month if needed)
            _, last_day = monthrange(start_date.year, start_date.month)
            effective_cutoff_day = min(cutoff_day, last_day)
            cutoff_date_for_start_month = start_date.replace(day=effective_cutoff_day)

            # Payroll can be processed if today + processing_days is on or before
            # the cutoff for the start date's month (regardless of whether start
            # date is in current or future month)
            deadline = today + timedelta(days=processing_days)
            payroll_ok = deadline <= cutoff_date_for_start_month

            checks.append(
                GatingCheckResult(
                    check_name="payroll_cutoff_valid",
                    passed=payroll_ok,
                    details=(
                        f"Start date respects payroll cutoff (day {cutoff_day})"
                        if payroll_ok
                        else f"Start date may miss payroll cutoff (day {cutoff_day})"
                    ),
                )
            )
        else:
            checks.append(
                GatingCheckResult(
                    check_name="payroll_cutoff_valid",
                    passed=True,
                    details="No payroll cutoff policy found for country",
                )
            )

        return checks

    def _state_to_output(self, state: HCMWorkerState) -> HCMWorkerStateOutput:
        """Convert ORM model to Pydantic output model."""
        return HCMWorkerStateOutput(
            worker_id=state.worker_id,
            case_id=state.case_id,
            onboarding_status=state.onboarding_status,
            onboarding_readiness=bool(state.onboarding_readiness),
            proposed_start_date=state.proposed_start_date,
            confirmed_start_date=state.confirmed_start_date,
            hire_finalized=bool(state.hire_finalized),
            effective_date=state.effective_date,
            created_at=state.created_at.isoformat(),
            updated_at=state.updated_at.isoformat(),
        )

    def _log_to_output(self, log: HCMWriteLog) -> HCMWriteLogOutput:
        """Convert ORM model to Pydantic output model."""
        return HCMWriteLogOutput(
            log_id=log.log_id,
            case_id=log.case_id,
            worker_id=log.worker_id,
            write_type=log.write_type,
            old_value=json.loads(log.old_value) if log.old_value else None,
            new_value=json.loads(log.new_value),
            actor_persona=log.actor_persona,
            policy_refs=json.loads(log.policy_refs),
            milestone_evidence=json.loads(log.milestone_evidence),
            rationale=log.rationale,
            timestamp=log.timestamp.isoformat(),
        )
