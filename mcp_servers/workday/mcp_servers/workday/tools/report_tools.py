"""Reporting tools for Workday HCM."""

from db.repositories.report_repository import ReportRepository
from db.session import get_session
from mcp_auth import require_scopes
from models import (
    HeadcountReportInput,
    HeadcountReportOutput,
    MovementReportInput,
    MovementReportOutput,
    OrgHierarchyReportInput,
    OrgHierarchyReportOutput,
    PositionReportInput,
    PositionReportOutput,
    WorkforceRosterInput,
    WorkforceRosterOutput,
)
from utils.decorators import make_async_background


@make_async_background
@require_scopes("read")
def workday_report_workforce_roster(
    request: WorkforceRosterInput,
) -> WorkforceRosterOutput:
    """Generate workforce roster report with full worker details."""
    # Execute report query using sync session
    with get_session() as session:
        repo = ReportRepository()
        result = repo.get_workforce_roster(session, request)
        # Read-only operation - no commit needed
        return result


@make_async_background
@require_scopes("read")
def workday_report_headcount(
    request: HeadcountReportInput,
) -> HeadcountReportOutput:
    """Generate headcount reconciliation report by organization or cost center."""
    # Execute report query using sync session
    with get_session() as session:
        repo = ReportRepository()
        return repo.get_headcount_summary(session, request)


@make_async_background
@require_scopes("read")
def workday_report_movements(
    request: MovementReportInput,
) -> MovementReportOutput:
    """Generate movement report with workforce lifecycle events."""
    # Execute report query using sync session
    with get_session() as session:
        repo = ReportRepository()
        return repo.get_movement_report(session, request)


@make_async_background
@require_scopes("read")
def workday_report_positions(
    request: PositionReportInput,
) -> PositionReportOutput:
    """Generate position vacancy report with position details."""
    # Execute report query using sync session
    with get_session() as session:
        repo = ReportRepository()
        return repo.get_position_report(session, request)


@make_async_background
@require_scopes("read")
def workday_report_org_hierarchy(
    request: OrgHierarchyReportInput,
) -> OrgHierarchyReportOutput:
    """Generate organization hierarchy report with flattened structure."""
    # Execute report query using sync session
    with get_session() as session:
        repo = ReportRepository()
        return repo.get_org_hierarchy_report(session, request)
