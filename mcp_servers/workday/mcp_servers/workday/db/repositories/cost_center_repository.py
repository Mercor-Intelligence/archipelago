"""CostCenterRepository for managing cost center CRUD operations.

This repository handles all database operations for cost centers.
"""

from models import CostCenterOutput, CreateCostCenterInput
from sqlalchemy.orm import Session

from db.models import CostCenter


class CostCenterRepository:
    """Repository for cost center database operations."""

    def create(self, session: Session, request: CreateCostCenterInput) -> CostCenterOutput:
        """Create a new cost center.

        Args:
            session: Database session
            request: Cost center creation request

        Returns:
            Created cost center details

        Note:
            Does not commit the transaction. Caller is responsible for committing.
        """
        cost_center = CostCenter(
            cost_center_id=request.cost_center_id,
            cost_center_name=request.cost_center_name,
            org_id=request.org_id,
        )
        session.add(cost_center)
        session.flush()

        return self._to_output(cost_center)

    def _to_output(self, cost_center: CostCenter) -> CostCenterOutput:
        """Convert CostCenter ORM model to Pydantic output model."""
        return CostCenterOutput(
            cost_center_id=cost_center.cost_center_id,
            cost_center_name=cost_center.cost_center_name,
            org_id=cost_center.org_id,
            created_at=cost_center.created_at.isoformat(),
        )
