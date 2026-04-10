"""Cost center management tools for Workday HCM MCP server."""

from db.models import CostCenter, SupervisoryOrg
from db.repositories.cost_center_repository import CostCenterRepository
from db.session import get_session
from loguru import logger
from mcp_auth import require_roles
from models import CostCenterOutput, CreateCostCenterInput
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from utils.decorators import make_async_background

# Error code constants
E_ORG_001 = "E_ORG_001"  # Organization not found (for org_id validation)
E_CC_001 = "E_CC_001"  # Cost center not found
E_CC_002 = "E_CC_002"  # Cost center already exists (duplicate)


@make_async_background
@require_roles("pre_onboarding_coordinator", "hr_admin")
def workday_create_cost_center(request: CreateCostCenterInput) -> CostCenterOutput:
    """Create a new cost center in Workday HCM."""
    logger.info(f"Creating cost center: {request.cost_center_id}")

    repository = CostCenterRepository()

    with get_session() as session:
        # Check duplicate cost_center_id up front to produce deterministic error
        existing = session.execute(
            select(CostCenter).where(CostCenter.cost_center_id == request.cost_center_id)
        ).scalar_one_or_none()
        if existing:
            raise ValueError(f"{E_CC_002}: Cost center already exists: {request.cost_center_id}")

        # Validate org_id exists (required FK)
        org = session.execute(
            select(SupervisoryOrg).where(SupervisoryOrg.org_id == request.org_id)
        ).scalar_one_or_none()
        if not org:
            raise ValueError(f"{E_ORG_001}: Organization '{request.org_id}' not found")

        # Create cost center via repository
        # Guard against concurrent creation race
        try:
            result = repository.create(session, request)
            logger.info(
                f"Successfully created cost center: {result.cost_center_id} "
                f"({result.cost_center_name})"
            )
            return result
        except IntegrityError as exc:
            # Reset failed transaction state before re-querying
            session.rollback()

            race_existing = session.execute(
                select(CostCenter).where(CostCenter.cost_center_id == request.cost_center_id)
            ).scalar_one_or_none()

            if race_existing is not None:
                # Deterministic duplicate behavior even under concurrency
                raise ValueError(
                    f"{E_CC_002}: Cost center already exists: {request.cost_center_id}"
                ) from exc

            # Non-duplicate integrity failures (e.g., FK constraints)
            raise
