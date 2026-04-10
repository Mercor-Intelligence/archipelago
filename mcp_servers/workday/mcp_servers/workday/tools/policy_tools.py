"""Policy management tools for Workday pre-onboarding coordination."""

from db.models import Case, PolicyReference
from db.repositories.audit_repository import AuditRepository
from db.repositories.case_repository import CaseRepository
from db.repositories.policy_repository import PolicyRepository
from db.session import get_session
from mcp_auth import require_roles, require_scopes
from models import (
    ApplicablePoliciesOutput,
    AttachPolicyInput,
    CasePolicyLinkOutput,
    CreatePayrollCutoffInput,
    CreatePolicyInput,
    GetApplicablePoliciesInput,
    PayrollCutoffOutput,
    PolicyRefOutput,
)
from sqlalchemy import select
from utils.decorators import make_async_background

# Error code constants (per BUILD_PLAN.md § 2.7 and acceptance criteria)
E_POLICY_001 = "E_POLICY_001"  # Policy not found
E_CASE_001 = "E_CASE_001"  # Case not found


@make_async_background
@require_scopes("read")
def workday_policies_get_applicable(
    request: GetApplicablePoliciesInput,
) -> ApplicablePoliciesOutput:
    """Retrieve applicable policies based on country, role, and employment type."""
    repository = PolicyRepository()

    with get_session() as session:
        return repository.get_applicable_policies(session, request)


@make_async_background
@require_roles("pre_onboarding_coordinator", "hr_admin")
def workday_policies_attach_to_case(
    request: AttachPolicyInput,
) -> list[CasePolicyLinkOutput]:
    """Attach policies to a case for audit trail and decision documentation."""
    case_repository = CaseRepository()
    audit_repository = AuditRepository()

    with get_session() as session:
        # Validate case exists
        case = session.execute(
            select(Case).where(Case.case_id == request.case_id)
        ).scalar_one_or_none()

        if not case:
            raise ValueError(f"{E_CASE_001}: Case '{request.case_id}' not found")

        # Validate all policies exist
        policies = list(
            session.execute(
                select(PolicyReference).where(PolicyReference.policy_id.in_(request.policy_ids))
            )
            .scalars()
            .all()
        )

        found_ids = {p.policy_id for p in policies}
        missing = set(request.policy_ids) - found_ids
        if missing:
            raise ValueError(f"{E_POLICY_001}: Policies not found: {missing}")

        # Attach policies via repository
        links = case_repository.attach_policies(
            session=session,
            case_id=request.case_id,
            policy_ids=request.policy_ids,
            decision_context=request.decision_context,
            actor_persona=request.actor_persona,
        )

        # Create audit entry for policy attachment
        audit_repository.log_action(
            session=session,
            case_id=request.case_id,
            action_type="policies_attached",
            actor_persona=request.actor_persona,
            rationale=request.decision_context,
            policy_refs=request.policy_ids,
            details={
                "attached_count": len(request.policy_ids),
                "policy_ids": request.policy_ids,
            },
        )

        return links


@make_async_background
@require_roles("pre_onboarding_coordinator", "hr_admin")
def workday_policies_create(
    request: CreatePolicyInput,
) -> PolicyRefOutput:
    """Create a new policy reference for country/role/employment type combinations."""
    repository = PolicyRepository()

    with get_session() as session:
        return repository.create_policy(
            session=session,
            policy_id=request.policy_id,
            country=request.country,
            policy_type=request.policy_type,
            content=request.content,
            effective_date=request.effective_date,
            version=request.version,
            role=request.role,
            employment_type=request.employment_type,
            lead_time_days=request.lead_time_days,
        )


@make_async_background
@require_roles("pre_onboarding_coordinator", "hr_admin")
def workday_policies_create_payroll_cutoff(
    request: CreatePayrollCutoffInput,
) -> PayrollCutoffOutput:
    """Create a payroll cutoff rule for a country."""
    repository = PolicyRepository()

    with get_session() as session:
        return repository.create_payroll_cutoff(
            session=session,
            cutoff_id=request.cutoff_id,
            country=request.country,
            cutoff_day_of_month=request.cutoff_day_of_month,
            processing_days=request.processing_days,
            effective_date=request.effective_date,
        )
