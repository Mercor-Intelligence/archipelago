"""OrgRepository for managing organization CRUD operations.

This repository handles all database operations for supervisory organizations.
"""

from models import (
    CreateSupervisoryOrgInput,
    GetOrgHierarchyInput,
    GetSupervisoryOrgInput,
    ListSupervisoryOrgsInput,
    OrgHierarchyNode,
    OrgHierarchyOutput,
    SupervisoryOrgListOutput,
    SupervisoryOrgOutput,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import SupervisoryOrg


class OrgRepository:
    """Repository for organization database operations."""

    def create(self, session: Session, request: CreateSupervisoryOrgInput) -> SupervisoryOrgOutput:
        """Create a new supervisory organization.

        Args:
            session: Database session
            request: Organization creation request

        Returns:
            Created organization details

        Note:
            Does not commit the transaction. Caller is responsible for committing.
        """
        org = SupervisoryOrg(
            org_id=request.org_id,
            org_name=request.org_name,
            org_type=request.org_type,
            parent_org_id=request.parent_org_id,
            manager_worker_id=request.manager_worker_id,
        )
        session.add(org)
        session.flush()

        return self._to_output(org)

    def get_org(
        self, session: Session, request: GetSupervisoryOrgInput
    ) -> SupervisoryOrgOutput | None:
        """Get organization by ID.

        Args:
            session: Database session
            request: Get organization request

        Returns:
            Organization details if found, None otherwise
        """
        stmt = select(SupervisoryOrg).where(SupervisoryOrg.org_id == request.org_id)
        result = session.execute(stmt)
        org = result.scalar_one_or_none()

        if not org:
            return None

        return self._to_output(org)

    def list_orgs(
        self, session: Session, request: ListSupervisoryOrgsInput
    ) -> SupervisoryOrgListOutput:
        """List supervisory organizations with pagination and filters.

        Args:
            session: Database session
            request: List orgs request with:
                - page_size: Results per page (default: 100)
                - page_number: Page number (default: 1)
                - org_type: Optional filter by type (Supervisory|Cost_Center|Location)
                - parent_org_id: Optional filter by parent organization

        Returns:
            Paginated list of organizations
        """
        # Build base query
        base_query = select(SupervisoryOrg)

        # Apply filters
        if request.org_type:
            base_query = base_query.where(SupervisoryOrg.org_type == request.org_type)
        if request.root_only:
            # Filter for root organizations (no parent)
            base_query = base_query.where(SupervisoryOrg.parent_org_id.is_(None))
        elif request.parent_org_id:
            base_query = base_query.where(SupervisoryOrg.parent_org_id == request.parent_org_id)

        # Get total count
        count_stmt = select(func.count()).select_from(base_query.subquery())
        total_count = session.execute(count_stmt).scalar_one()

        # Apply pagination
        # Use secondary sort key (org_id) for deterministic ordering with ties
        offset = (request.page_number - 1) * request.page_size
        stmt = (
            base_query.order_by(SupervisoryOrg.created_at.desc(), SupervisoryOrg.org_id)
            .offset(offset)
            .limit(request.page_size)
        )

        # Execute query
        result = session.execute(stmt)
        orgs = list(result.scalars().all())

        return SupervisoryOrgListOutput(
            orgs=[self._to_output(o) for o in orgs],
            total_count=total_count,
            page_size=request.page_size,
            page_number=request.page_number,
        )

    def get_org_hierarchy(
        self, session: Session, request: GetOrgHierarchyInput
    ) -> OrgHierarchyOutput:
        """Get organization hierarchy as nested tree structure.

        Args:
            session: Database session
            request: Get hierarchy request with optional root_org_id

        Returns:
            OrgHierarchyOutput with nested tree structure

        Raises:
            ValueError: If root_org_id provided but not found (E_ORG_001)
            ValueError: If circular reference detected (E_ORG_002)
        """
        # Query all orgs
        stmt = select(SupervisoryOrg)
        result = session.execute(stmt)
        all_orgs = list(result.scalars().all())

        # Build org lookup map
        org_map = {org.org_id: org for org in all_orgs}

        # If root_org_id provided, validate it exists
        root_org = None
        if request.root_org_id:
            root_org = org_map.get(request.root_org_id)
            if not root_org:
                raise ValueError("E_ORG_001: Root organization not found")

        # Build parent-child relationships map
        children_map: dict[str | None, list[SupervisoryOrg]] = {}
        for org in all_orgs:
            parent_id = org.parent_org_id
            if parent_id not in children_map:
                children_map[parent_id] = []
            children_map[parent_id].append(org)

        # Build tree starting from root(s)
        visited = set()
        hierarchy: list[OrgHierarchyNode] = []

        def build_node(org: SupervisoryOrg, path: set[str]) -> OrgHierarchyNode:
            """Build a hierarchy node recursively with circular reference detection."""
            if org.org_id in path:
                raise ValueError("E_ORG_002: Circular org hierarchy detected")

            visited.add(org.org_id)
            new_path = path | {org.org_id}

            # Get children for this org
            children_orgs = children_map.get(org.org_id, [])
            children_nodes = [build_node(child, new_path) for child in children_orgs]

            return OrgHierarchyNode(
                org_id=org.org_id,
                org_name=org.org_name,
                org_type=org.org_type,
                manager_worker_id=org.manager_worker_id,
                children=children_nodes,
            )

        # If root_org_id specified, build tree from that root
        if root_org:
            hierarchy = [build_node(root_org, set())]
        else:
            # Build trees from all root orgs (orgs with no parent)
            root_orgs = children_map.get(None, [])
            for root_org in root_orgs:
                if root_org.org_id not in visited:
                    hierarchy.append(build_node(root_org, set()))

        return OrgHierarchyOutput(hierarchy=hierarchy)

    def _to_output(self, org: SupervisoryOrg) -> SupervisoryOrgOutput:
        """Convert SupervisoryOrg ORM model to Pydantic output model."""
        return SupervisoryOrgOutput(
            org_id=org.org_id,
            org_name=org.org_name,
            org_type=org.org_type,
            parent_org_id=org.parent_org_id,
            manager_worker_id=org.manager_worker_id,
            created_at=org.created_at.isoformat(),
            updated_at=org.updated_at.isoformat(),
        )
