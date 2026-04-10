"""Organization management tools for Workday HCM MCP server."""

from db.models import SupervisoryOrg, Worker
from db.repositories.org_repository import OrgRepository
from db.session import get_session
from loguru import logger
from mcp_auth import require_roles, require_scopes
from models import (
    CreateSupervisoryOrgInput,
    GetOrgHierarchyInput,
    GetSupervisoryOrgInput,
    ListSupervisoryOrgsInput,
    OrgHierarchyOutput,
    SupervisoryOrgListOutput,
    SupervisoryOrgOutput,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from utils.decorators import make_async_background

# Error code constants
E_ORG_001 = "E_ORG_001"  # Organization not found
E_ORG_002 = "E_ORG_002"  # Organization already exists (duplicate) / Circular reference
E_WRK_001 = "E_WRK_001"  # Worker not found (for manager validation)


@make_async_background
@require_roles("pre_onboarding_coordinator", "hr_admin")
def workday_create_org(request: CreateSupervisoryOrgInput) -> SupervisoryOrgOutput:
    """Create a new supervisory organization in Workday HCM."""
    logger.info(f"Creating organization: {request.org_id}")

    repository = OrgRepository()

    with get_session() as session:
        # Check duplicate org_id up front to produce deterministic error
        existing = session.execute(
            select(SupervisoryOrg).where(SupervisoryOrg.org_id == request.org_id)
        ).scalar_one_or_none()
        if existing:
            raise ValueError(f"{E_ORG_002}: Organization already exists: {request.org_id}")

        # Validate parent_org_id if provided
        if request.parent_org_id:
            parent = session.execute(
                select(SupervisoryOrg).where(SupervisoryOrg.org_id == request.parent_org_id)
            ).scalar_one_or_none()
            if not parent:
                raise ValueError(
                    f"{E_ORG_001}: Parent organization '{request.parent_org_id}' not found"
                )

        # Validate manager_worker_id if provided
        if request.manager_worker_id:
            manager = session.execute(
                select(Worker).where(Worker.worker_id == request.manager_worker_id)
            ).scalar_one_or_none()
            if not manager:
                raise ValueError(
                    f"{E_WRK_001}: Manager worker '{request.manager_worker_id}' not found"
                )

        # Create organization via repository
        # Guard against concurrent creation race
        try:
            result = repository.create(session, request)
            logger.info(f"Successfully created organization: {result.org_id} ({result.org_name})")
            return result
        except IntegrityError as exc:
            # Reset failed transaction state before re-querying
            session.rollback()

            race_existing = session.execute(
                select(SupervisoryOrg).where(SupervisoryOrg.org_id == request.org_id)
            ).scalar_one_or_none()

            if race_existing is not None:
                # Deterministic duplicate behavior even under concurrency
                raise ValueError(
                    f"{E_ORG_002}: Organization already exists: {request.org_id}"
                ) from exc

            # Non-duplicate integrity failures (e.g., FK constraints)
            raise


@make_async_background
@require_scopes("read")
def workday_get_org(request: GetSupervisoryOrgInput) -> SupervisoryOrgOutput:
    """Retrieve detailed information about a supervisory organization."""
    logger.info(f"Retrieving organization: {request.org_id}")

    repository = OrgRepository()

    with get_session() as session:
        org = repository.get_org(session, request)

        if not org:
            raise ValueError(f"{E_ORG_001}: Organization not found")

        logger.info(f"Successfully retrieved organization: {org.org_id} ({org.org_name})")
        return org


@make_async_background
@require_scopes("read")
def workday_list_orgs(request: ListSupervisoryOrgsInput) -> SupervisoryOrgListOutput:
    """List supervisory organizations with filtering and pagination."""
    repository = OrgRepository()

    with get_session() as session:
        return repository.list_orgs(session, request)


@make_async_background
@require_scopes("read")
def workday_get_org_hierarchy(request: GetOrgHierarchyInput) -> OrgHierarchyOutput:
    """Retrieve organization hierarchy as nested tree structure."""
    logger.info(f"Retrieving organization hierarchy: root_org_id={request.root_org_id}")

    repository = OrgRepository()

    with get_session() as session:
        result = repository.get_org_hierarchy(session, request)
        logger.info(f"Successfully retrieved hierarchy: {len(result.hierarchy)} root node(s)")
        return result
