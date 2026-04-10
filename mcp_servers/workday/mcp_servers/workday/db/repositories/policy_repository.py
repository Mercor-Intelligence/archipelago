"""PolicyRepository for managing policy references.

This repository handles policy lookup and filtering for the V2
pre-onboarding coordination system.
"""

import json

from models import (
    ApplicablePoliciesOutput,
    GetApplicablePoliciesInput,
    PayrollCutoffOutput,
    PolicyRefOutput,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import PayrollCutoff, PolicyReference


class PolicyRepository:
    """Repository for policy reference operations."""

    def get_applicable_policies(
        self, session: Session, request: GetApplicablePoliciesInput
    ) -> ApplicablePoliciesOutput:
        """Get applicable policies based on filters.

        Args:
            session: Database session
            request: Policy filter request

        Returns:
            List of applicable policies
        """
        # Build query
        query = select(PolicyReference).where(PolicyReference.country == request.country)

        # Apply optional filters
        if request.role:
            query = query.where(
                (PolicyReference.role == request.role) | (PolicyReference.role.is_(None))
            )
        if request.employment_type:
            query = query.where(
                (PolicyReference.employment_type == request.employment_type)
                | (PolicyReference.employment_type.is_(None))
            )
        if request.policy_type:
            query = query.where(PolicyReference.policy_type == request.policy_type)
        if request.as_of_date:
            query = query.where(PolicyReference.effective_date <= request.as_of_date)

        # Order by effective date descending to get most recent
        query = query.order_by(PolicyReference.effective_date.desc())

        policies = list(session.execute(query).scalars().all())

        return ApplicablePoliciesOutput(
            policies=[self._to_output(p) for p in policies],
            total_count=len(policies),
            query_context={
                "country": request.country,
                "role": request.role,
                "employment_type": request.employment_type,
                "policy_type": request.policy_type,
                "as_of_date": request.as_of_date,
            },
        )

    def get_by_id(self, session: Session, policy_id: str) -> PolicyRefOutput | None:
        """Get policy by ID.

        Args:
            session: Database session
            policy_id: Policy ID

        Returns:
            Policy details if found, None otherwise
        """
        policy = session.execute(
            select(PolicyReference).where(PolicyReference.policy_id == policy_id)
        ).scalar_one_or_none()

        if not policy:
            return None

        return self._to_output(policy)

    def get_lead_time_policy(
        self, session: Session, country: str, role: str | None = None
    ) -> PolicyRefOutput | None:
        """Get lead time policy for a country/role.

        Args:
            session: Database session
            country: Country code
            role: Optional role filter

        Returns:
            Lead time policy if found, None otherwise
        """
        query = select(PolicyReference).where(
            PolicyReference.country == country,
            PolicyReference.policy_type == "lead_times",
        )

        if role:
            query = query.where((PolicyReference.role == role) | (PolicyReference.role.is_(None)))

        # Get most recent effective policy
        query = query.order_by(PolicyReference.effective_date.desc())

        policy = session.execute(query).scalars().first()

        if not policy:
            return None

        return self._to_output(policy)

    def get_payroll_cutoff(self, session: Session, country: str) -> PayrollCutoffOutput | None:
        """Get payroll cutoff for a country.

        Args:
            session: Database session
            country: Country code

        Returns:
            Payroll cutoff if found, None otherwise
        """
        cutoff = (
            session.execute(
                select(PayrollCutoff)
                .where(PayrollCutoff.country == country)
                .order_by(PayrollCutoff.effective_date.desc())
            )
            .scalars()
            .first()
        )

        if not cutoff:
            return None

        return PayrollCutoffOutput(
            cutoff_id=cutoff.cutoff_id,
            country=cutoff.country,
            cutoff_day_of_month=cutoff.cutoff_day_of_month,
            processing_days=cutoff.processing_days,
            effective_date=cutoff.effective_date,
            created_at=cutoff.created_at.isoformat(),
        )

    def create_policy(
        self,
        session: Session,
        policy_id: str,
        country: str,
        policy_type: str,
        content: dict,
        effective_date: str,
        version: str,
        role: str | None = None,
        employment_type: str | None = None,
        lead_time_days: int | None = None,
    ) -> PolicyRefOutput:
        """Create a new policy reference.

        Args:
            session: Database session
            policy_id: Unique policy identifier
            country: Country code
            policy_type: Policy type
            content: Policy content (JSON serializable)
            effective_date: Effective date
            version: Policy version
            role: Optional role filter
            employment_type: Optional employment type filter
            lead_time_days: Lead time in days (for lead_times type)

        Returns:
            Created policy details
        """
        policy = PolicyReference(
            policy_id=policy_id,
            country=country,
            role=role,
            employment_type=employment_type,
            policy_type=policy_type,
            lead_time_days=lead_time_days,
            content=json.dumps(content),
            effective_date=effective_date,
            version=version,
        )
        session.add(policy)
        session.flush()

        return self._to_output(policy)

    def create_payroll_cutoff(
        self,
        session: Session,
        cutoff_id: str,
        country: str,
        cutoff_day_of_month: int,
        processing_days: int,
        effective_date: str,
    ) -> PayrollCutoffOutput:
        """Create a new payroll cutoff rule.

        Args:
            session: Database session
            cutoff_id: Unique cutoff identifier
            country: Country code
            cutoff_day_of_month: Day of month for cutoff
            processing_days: Number of processing days
            effective_date: Effective date

        Returns:
            Created cutoff details
        """
        cutoff = PayrollCutoff(
            cutoff_id=cutoff_id,
            country=country,
            cutoff_day_of_month=cutoff_day_of_month,
            processing_days=processing_days,
            effective_date=effective_date,
        )
        session.add(cutoff)
        session.flush()

        return PayrollCutoffOutput(
            cutoff_id=cutoff.cutoff_id,
            country=cutoff.country,
            cutoff_day_of_month=cutoff.cutoff_day_of_month,
            processing_days=cutoff.processing_days,
            effective_date=cutoff.effective_date,
            created_at=cutoff.created_at.isoformat(),
        )

    def _to_output(self, policy: PolicyReference) -> PolicyRefOutput:
        """Convert ORM model to Pydantic output model."""
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
