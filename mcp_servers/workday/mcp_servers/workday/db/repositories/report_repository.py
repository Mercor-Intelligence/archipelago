"""ReportRepository for generating RaaS-style reports.

This repository handles all reporting queries, including:
- Workforce roster reports (workday_report_workforce_roster)
- Headcount reconciliation reports (workday_report_headcount)
- Movement reports (workday_report_movements)
- Position reports (workday_report_positions)
- Organization hierarchy reports (workday_report_org_hierarchy)
"""

from datetime import datetime

from models import (
    HeadcountReportInput,
    HeadcountReportOutput,
    HeadcountReportRow,
    MovementReportInput,
    MovementReportOutput,
    MovementReportRow,
    OrgHierarchyReportInput,
    OrgHierarchyReportOutput,
    OrgHierarchyReportRow,
    PositionReportInput,
    PositionReportOutput,
    PositionReportRow,
    WorkforceRosterInput,
    WorkforceRosterOutput,
    WorkforceRosterRow,
)
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased

from db.models import CostCenter, JobProfile, Location, Movement, Position, SupervisoryOrg, Worker

# Error code constants (per BUILD_PLAN.md § 2.7)
E_VAL_001 = "E_VAL_001"  # Invalid date format
E_VAL_002 = "E_VAL_002"  # Date range validation error


class ReportRepository:
    """Repository for RaaS-style reporting queries.

    Supports multiple report types:
    - Workforce roster (get_workforce_roster)
    - Headcount reconciliation (get_headcount_summary)
    - Movement report (get_movement_report)
    - Position report (get_position_report)
    - Organization hierarchy report (get_org_hierarchy_report)
    """

    def get_workforce_roster(
        self, session: Session, request: WorkforceRosterInput
    ) -> WorkforceRosterOutput:
        """Generate workforce roster report with full worker details.

        This is the primary report for FP&A and Controller use cases. Returns
        a flattened roster of all workers with full details (org, cost center,
        job profile, FTE, status).

        Business Logic (per BUILD_PLAN.md § 3.5):
        1. JOIN workers with job_profiles, supervisory_orgs, cost_centers, locations
        2. Apply filters (org, cost_center, status)
        3. If as_of_date: filter by effective_date <= as_of_date AND
           (termination_date IS NULL OR termination_date > as_of_date)
        4. Order by worker_id
        5. Paginate results

        Args:
            session: Database session
            request: Roster report request with filters and pagination

        Returns:
            WorkforceRosterOutput with paginated roster and total count

        Raises:
            ValueError: If validation fails

        Note:
            Does not commit the transaction (read-only operation).
        """
        # Build base query with JOINs
        base_query = (
            select(
                Worker.worker_id,
                Worker.job_profile_id,
                JobProfile.title.label("job_title"),
                JobProfile.job_family,
                Worker.org_id,
                SupervisoryOrg.org_name,
                Worker.cost_center_id,
                CostCenter.cost_center_name,
                Worker.location_id,
                Location.location_name,
                Worker.employment_status,
                Worker.fte,
                Worker.hire_date,
                Worker.termination_date,
                Worker.effective_date,
            )
            .join(JobProfile, Worker.job_profile_id == JobProfile.job_profile_id)
            .join(SupervisoryOrg, Worker.org_id == SupervisoryOrg.org_id)
            .join(CostCenter, Worker.cost_center_id == CostCenter.cost_center_id)
            .outerjoin(Location, Worker.location_id == Location.location_id)
        )

        # Apply filters
        filters = []

        # Filter by organization
        if request.org_id:
            filters.append(Worker.org_id == request.org_id)

        # Filter by cost center
        if request.cost_center_id:
            filters.append(Worker.cost_center_id == request.cost_center_id)

        # Filter by employment status
        if request.employment_status:
            filters.append(Worker.employment_status == request.employment_status)

        # Temporal query: filter by as_of_date
        if request.as_of_date:
            # Parse as_of_date to compare with hire_date and termination_date
            as_of_dt = datetime.strptime(request.as_of_date, "%Y-%m-%d").date()

            # Worker must be effective on as_of_date
            filters.append(func.date(Worker.effective_date) <= as_of_dt)

            # Worker must not be terminated before as_of_date
            filters.append(
                (Worker.termination_date.is_(None))
                | (func.date(Worker.termination_date) > as_of_dt)
            )

        # Apply all filters
        if filters:
            base_query = base_query.where(*filters)

        # Order by worker_id (per BUILD_PLAN.md)
        base_query = base_query.order_by(Worker.worker_id)

        # Get total count (before pagination)
        count_query = select(func.count()).select_from(base_query.subquery())
        total_count = session.execute(count_query).scalar_one()

        # Apply pagination
        offset = (request.page_number - 1) * request.page_size
        paginated_query = base_query.limit(request.page_size).offset(offset)

        # Execute query
        results = session.execute(paginated_query).all()

        # Convert to WorkforceRosterRow objects
        roster = [
            WorkforceRosterRow(
                worker_id=row.worker_id,
                job_profile_id=row.job_profile_id,
                job_title=row.job_title,
                job_family=row.job_family,
                org_id=row.org_id,
                org_name=row.org_name,
                cost_center_id=row.cost_center_id,
                cost_center_name=row.cost_center_name,
                location_id=row.location_id,
                location_name=row.location_name,
                employment_status=row.employment_status,
                fte=row.fte,
                hire_date=str(row.hire_date),
                termination_date=str(row.termination_date) if row.termination_date else None,
                effective_date=str(row.effective_date),
            )
            for row in results
        ]

        return WorkforceRosterOutput(
            roster=roster,
            total_count=total_count,
            page_size=request.page_size,
            page_number=request.page_number,
            as_of_date=request.as_of_date,
        )

    def get_headcount_summary(
        self, session: Session, request: HeadcountReportInput
    ) -> HeadcountReportOutput:
        """Generate headcount reconciliation report by org or cost center.

        Args:
            session: Database session
            request: Report request parameters

        Returns:
            Headcount summary with beginning HC, hires, terms, transfers, and ending HC

        Raises:
            ValueError: If date validation fails or reconciliation doesn't balance
        """
        # Validate date range
        self._validate_date_range(request.start_date, request.end_date)

        # Determine grouping dimension
        if request.group_by == "org_id":
            groups = self._get_org_groups(session, request.org_id)
        else:  # cost_center_id
            groups = self._get_cost_center_groups(session, request.org_id)

        # Build report rows for each group
        report_rows = []
        for group_id, group_name in groups:
            row = self._calculate_headcount_row(
                session,
                group_id,
                group_name,
                request.start_date,
                request.end_date,
                request.group_by,
            )
            report_rows.append(row)

        # Sort by group_name
        report_rows.sort(key=lambda x: x.group_name)

        return HeadcountReportOutput(
            report=report_rows,
            total_count=len(report_rows),
            start_date=request.start_date,
            end_date=request.end_date,
            group_by=request.group_by,
        )

    def _validate_date_range(self, start_date: str, end_date: str) -> None:
        """Validate date range and format.

        Args:
            start_date: Period start date (YYYY-MM-DD)
            end_date: Period end date (YYYY-MM-DD)

        Raises:
            ValueError: If dates are invalid
        """
        # Validate date format
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"{E_VAL_001}: Invalid date format. Use YYYY-MM-DD")

        # Validate date range
        if start_dt > end_dt:
            raise ValueError(f"{E_VAL_002}: start_date must be before or equal to end_date")

    def _get_org_groups(self, session: Session, filter_org_id: str | None) -> list[tuple[str, str]]:
        """Get list of organizations for reporting.

        Args:
            session: Database session
            filter_org_id: Optional org_id filter

        Returns:
            List of (org_id, org_name) tuples
        """
        query = select(SupervisoryOrg.org_id, SupervisoryOrg.org_name)

        if filter_org_id:
            query = query.where(SupervisoryOrg.org_id == filter_org_id)

        result = session.execute(query).all()
        return [(row[0], row[1]) for row in result]

    def _get_cost_center_groups(
        self, session: Session, filter_org_id: str | None
    ) -> list[tuple[str, str]]:
        """Get list of cost centers for reporting.

        Args:
            session: Database session
            filter_org_id: Optional org_id filter (for cost centers in that org)

        Returns:
            List of (cost_center_id, cost_center_name) tuples
        """
        query = select(CostCenter.cost_center_id, CostCenter.cost_center_name)

        if filter_org_id:
            query = query.where(CostCenter.org_id == filter_org_id)

        result = session.execute(query).all()
        return [(row[0], row[1]) for row in result]

    def _calculate_headcount_row(
        self,
        session: Session,
        group_id: str,
        group_name: str,
        start_date: str,
        end_date: str,
        group_by: str,
    ) -> HeadcountReportRow:
        """Calculate headcount metrics for a single group.

        Args:
            session: Database session
            group_id: Organization or cost center ID
            group_name: Organization or cost center name
            start_date: Period start date (YYYY-MM-DD)
            end_date: Period end date (YYYY-MM-DD)
            group_by: Grouping dimension (org_id or cost_center_id)

        Returns:
            Headcount report row with all calculated metrics
        """
        # Calculate beginning headcount
        # Active workers at start_date: hired before/on start_date, not terminated before start_date
        beginning_hc = self._count_beginning_headcount(session, group_id, start_date, group_by)

        # Count hires during period from movements table
        hires = self._count_hires(session, group_id, start_date, end_date, group_by)

        # Count terminations during period from movements table
        terminations = self._count_terminations(session, group_id, start_date, end_date, group_by)

        # Count transfers in during period
        transfers_in = self._count_transfers_in(session, group_id, start_date, end_date, group_by)

        # Count transfers out during period
        transfers_out = self._count_transfers_out(session, group_id, start_date, end_date, group_by)

        # Calculate net movement and ending headcount
        net_movement = hires - terminations + transfers_in - transfers_out
        ending_hc_formula = beginning_hc + net_movement

        # Calculate actual ending headcount independently to validate reconciliation
        ending_hc_actual = self._count_ending_headcount(session, group_id, end_date, group_by)

        # CRITICAL: Validate reconciliation formula
        if ending_hc_formula != ending_hc_actual:
            raise ValueError(
                f"E_RECON_001: Reconciliation failed for {group_id}. "
                f"Formula: beginning_hc({beginning_hc}) + net_movement({net_movement}) "
                f"= {ending_hc_formula}, but actual ending_hc = {ending_hc_actual}. "
                f"This indicates missing or duplicated movement events."
            )

        return HeadcountReportRow(
            group_id=group_id,
            group_name=group_name,
            beginning_hc=beginning_hc,
            hires=hires,
            terminations=terminations,
            transfers_in=transfers_in,
            transfers_out=transfers_out,
            net_movement=net_movement,
            ending_hc=ending_hc_actual,  # Use actual count, not formula
        )

    def _count_beginning_headcount(
        self, session: Session, group_id: str, start_date: str, group_by: str
    ) -> int:
        """Count active workers at start_date based on historical movements.

        Uses window functions to efficiently determine worker group membership
        in a single aggregated query, avoiding N+1 query pattern.

        Args:
            session: Database session
            group_id: Organization or cost center ID
            start_date: Start date (YYYY-MM-DD)
            group_by: Grouping dimension

        Returns:
            Count of active workers who were in this group at start_date
        """
        from sqlalchemy import func

        # Determine which field to use based on grouping dimension
        group_field = Movement.to_org_id if group_by == "org_id" else Movement.to_cost_center_id

        # Use window function to get most recent movement for each worker
        # Window function ranks movements by date DESC, created_at DESC, event_id DESC
        # This ensures deterministic results even when multiple movements share the same date
        ranked_movements = (
            select(
                Movement.worker_id,
                group_field.label("group_value"),
                func.row_number()
                .over(
                    partition_by=Movement.worker_id,
                    order_by=[
                        Movement.event_date.desc(),
                        Movement.created_at.desc(),
                        Movement.event_id.desc(),
                    ],
                )
                .label("rn"),
            )
            .where(
                and_(
                    Movement.event_date <= start_date,
                    group_field.isnot(None),
                )
            )
            .subquery()
        )

        # Get workers who were employed at start_date and their most recent movement
        # NOTE: We use termination_date for temporal queries, not employment_status
        # because employment_status reflects current state, not historical state
        result = session.execute(
            select(func.count(Worker.worker_id))
            .select_from(Worker)
            .join(ranked_movements, Worker.worker_id == ranked_movements.c.worker_id)
            .where(
                and_(
                    Worker.hire_date <= start_date,
                    or_(Worker.termination_date.is_(None), Worker.termination_date > start_date),
                    ranked_movements.c.rn == 1,  # Most recent movement
                    ranked_movements.c.group_value == group_id,  # In target group
                )
            )
        )

        return result.scalar() or 0

    def _count_ending_headcount(
        self, session: Session, group_id: str, end_date: str, group_by: str
    ) -> int:
        """Count active workers at end_date independently (for reconciliation validation).

        Uses window functions to efficiently determine worker group membership
        in a single aggregated query, avoiding N+1 query pattern.

        This serves as a validation check against the formula-based ending_hc to
        detect missing or duplicated movement events.

        Args:
            session: Database session
            group_id: Organization or cost center ID
            end_date: End date (YYYY-MM-DD)
            group_by: Grouping dimension

        Returns:
            Count of active workers in this group at end_date
        """
        from sqlalchemy import func

        # Determine which field to use based on grouping dimension
        group_field = Movement.to_org_id if group_by == "org_id" else Movement.to_cost_center_id

        # Use window function to get most recent movement for each worker
        # Window function ranks movements by date DESC, created_at DESC, event_id DESC
        # This ensures deterministic results even when multiple movements share the same date
        ranked_movements = (
            select(
                Movement.worker_id,
                group_field.label("group_value"),
                func.row_number()
                .over(
                    partition_by=Movement.worker_id,
                    order_by=[
                        Movement.event_date.desc(),
                        Movement.created_at.desc(),
                        Movement.event_id.desc(),
                    ],
                )
                .label("rn"),
            )
            .where(
                and_(
                    Movement.event_date <= end_date,
                    group_field.isnot(None),
                )
            )
            .subquery()
        )

        # Get workers who were employed at end_date and their most recent movement
        # NOTE: We use termination_date for temporal queries, not employment_status
        # because employment_status reflects current state, not historical state
        result = session.execute(
            select(func.count(Worker.worker_id))
            .select_from(Worker)
            .join(ranked_movements, Worker.worker_id == ranked_movements.c.worker_id)
            .where(
                and_(
                    Worker.hire_date <= end_date,
                    or_(Worker.termination_date.is_(None), Worker.termination_date > end_date),
                    ranked_movements.c.rn == 1,  # Most recent movement
                    ranked_movements.c.group_value == group_id,  # In target group
                )
            )
        )

        return result.scalar() or 0

    def _count_hires(
        self, session: Session, group_id: str, start_date: str, end_date: str, group_by: str
    ) -> int:
        """Count hires during period.

        Args:
            session: Database session
            group_id: Organization or cost center ID
            start_date: Period start date (YYYY-MM-DD)
            end_date: Period end date (YYYY-MM-DD)
            group_by: Grouping dimension

        Returns:
            Count of hire events in the period
        """
        query = select(func.count(Movement.event_id)).where(
            and_(
                Movement.event_type == "hire",
                Movement.event_date > start_date,
                Movement.event_date <= end_date,
            )
        )

        # Add group filter based on to_org_id or to_cost_center_id
        if group_by == "org_id":
            query = query.where(Movement.to_org_id == group_id)
        else:  # cost_center_id
            query = query.where(Movement.to_cost_center_id == group_id)

        result = session.execute(query).scalar_one()
        return result or 0

    def _count_terminations(
        self, session: Session, group_id: str, start_date: str, end_date: str, group_by: str
    ) -> int:
        """Count terminations during period.

        Args:
            session: Database session
            group_id: Organization or cost center ID
            start_date: Period start date (YYYY-MM-DD)
            end_date: Period end date (YYYY-MM-DD)
            group_by: Grouping dimension

        Returns:
            Count of termination events in the period
        """
        query = select(func.count(Movement.event_id)).where(
            and_(
                Movement.event_type == "termination",
                Movement.event_date > start_date,
                Movement.event_date <= end_date,
            )
        )

        # Add group filter based on from_org_id or from_cost_center_id
        if group_by == "org_id":
            query = query.where(Movement.from_org_id == group_id)
        else:  # cost_center_id
            query = query.where(Movement.from_cost_center_id == group_id)

        result = session.execute(query).scalar_one()
        return result or 0

    def _count_transfers_in(
        self, session: Session, group_id: str, start_date: str, end_date: str, group_by: str
    ) -> int:
        """Count transfers into the group during period.

        Excludes intra-group transfers (e.g., cost center change within same org)
        to avoid double-counting internal reorganizations.

        Args:
            session: Database session
            group_id: Organization or cost center ID
            start_date: Period start date (YYYY-MM-DD)
            end_date: Period end date (YYYY-MM-DD)
            group_by: Grouping dimension

        Returns:
            Count of transfer-in events in the period
        """
        query = select(func.count(Movement.event_id)).where(
            and_(
                Movement.event_type == "transfer",
                Movement.event_date > start_date,
                Movement.event_date <= end_date,
            )
        )

        # Add group filter - transfers INTO this group
        # Exclude intra-group transfers where from and to are the same
        # Handle NULL: NULL != value returns NULL (not TRUE), use or_(is_(None), !=)
        if group_by == "org_id":
            query = query.where(
                and_(
                    Movement.to_org_id == group_id,
                    or_(
                        Movement.from_org_id.is_(None),
                        Movement.from_org_id != group_id,
                    ),
                )
            )
        else:  # cost_center_id
            query = query.where(
                and_(
                    Movement.to_cost_center_id == group_id,
                    or_(
                        Movement.from_cost_center_id.is_(None),
                        Movement.from_cost_center_id != group_id,
                    ),
                )
            )

        result = session.execute(query).scalar_one()
        return result or 0

    def _count_transfers_out(
        self, session: Session, group_id: str, start_date: str, end_date: str, group_by: str
    ) -> int:
        """Count transfers out of the group during period.

        Excludes intra-group transfers (e.g., cost center change within same org)
        to avoid double-counting internal reorganizations.

        Args:
            session: Database session
            group_id: Organization or cost center ID
            start_date: Period start date (YYYY-MM-DD)
            end_date: Period end date (YYYY-MM-DD)
            group_by: Grouping dimension

        Returns:
            Count of transfer-out events in the period
        """
        query = select(func.count(Movement.event_id)).where(
            and_(
                Movement.event_type == "transfer",
                Movement.event_date > start_date,
                Movement.event_date <= end_date,
            )
        )

        # Add group filter - transfers OUT OF this group
        # Exclude intra-group transfers where from and to are the same
        # Handle NULL: NULL != value returns NULL (not TRUE), use or_(is_(None), !=)
        if group_by == "org_id":
            query = query.where(
                and_(
                    Movement.from_org_id == group_id,
                    or_(
                        Movement.to_org_id.is_(None),
                        Movement.to_org_id != group_id,
                    ),
                )
            )
        else:  # cost_center_id
            query = query.where(
                and_(
                    Movement.from_cost_center_id == group_id,
                    or_(
                        Movement.to_cost_center_id.is_(None),
                        Movement.to_cost_center_id != group_id,
                    ),
                )
            )

        result = session.execute(query).scalar_one()
        return result or 0

    def get_movement_report(
        self, session: Session, request: MovementReportInput
    ) -> MovementReportOutput:
        """Generate movement report with full event details.

        This report provides a detailed log of all workforce lifecycle events
        (hires, terminations, transfers) within a specified date range, with
        optional filtering by event type and organization.

        Business Logic (per BUILD_PLAN.md § 3.5):
        1. Query movements table for date range.
        2. JOIN with workers, job_profiles, supervisory_orgs (from/to) for enriched data.
        3. Apply filters (event_type, org_id).
        4. Order by event_date DESC (most recent first).
        5. Paginate results.

        Args:
            session: Database session
            request: Movement report request with filters and pagination

        Returns:
            MovementReportOutput with paginated movements and total count

        Raises:
            ValueError: If validation fails

        Note:
            Does not commit the transaction (read-only operation).
        """
        # Validate date range
        self._validate_date_range(request.start_date, request.end_date)

        # Create aliases for from/to joins
        from_org = aliased(SupervisoryOrg)
        to_org = aliased(SupervisoryOrg)
        from_cc = aliased(CostCenter)
        to_cc = aliased(CostCenter)
        from_jp = aliased(JobProfile)
        to_jp = aliased(JobProfile)

        # Build base query with JOINs
        base_query = (
            select(
                Movement.event_id,
                Movement.worker_id,
                Movement.event_type,
                Movement.event_date,
                Movement.from_org_id,
                from_org.org_name.label("from_org_name"),
                Movement.to_org_id,
                to_org.org_name.label("to_org_name"),
                Movement.from_cost_center_id,
                from_cc.cost_center_name.label("from_cost_center_name"),
                Movement.to_cost_center_id,
                to_cc.cost_center_name.label("to_cost_center_name"),
                Movement.from_job_profile_id,
                from_jp.title.label("from_job_title"),
                Movement.to_job_profile_id,
                to_jp.title.label("to_job_title"),
                Movement.from_position_id,
                Movement.to_position_id,
                Movement.created_at,
            )
            .join(Worker, Movement.worker_id == Worker.worker_id)
            .outerjoin(from_org, Movement.from_org_id == from_org.org_id)
            .outerjoin(to_org, Movement.to_org_id == to_org.org_id)
            .outerjoin(from_cc, Movement.from_cost_center_id == from_cc.cost_center_id)
            .outerjoin(to_cc, Movement.to_cost_center_id == to_cc.cost_center_id)
            .outerjoin(from_jp, Movement.from_job_profile_id == from_jp.job_profile_id)
            .outerjoin(to_jp, Movement.to_job_profile_id == to_jp.job_profile_id)
        )

        # Apply filters
        filters = []

        # Filter by date range
        filters.append(Movement.event_date >= request.start_date)
        filters.append(Movement.event_date <= request.end_date)

        # Filter by event type
        if request.event_type:
            filters.append(Movement.event_type == request.event_type)

        # Filter by organization (either from_org_id or to_org_id)
        if request.org_id:
            filters.append(
                or_(
                    Movement.from_org_id == request.org_id,
                    Movement.to_org_id == request.org_id,
                )
            )

        if filters:
            base_query = base_query.where(*filters)

        # Order by event_date DESC (most recent first), then event_id for deterministic ordering
        # This ensures stable pagination when multiple movements occur on the same date
        base_query = base_query.order_by(Movement.event_date.desc(), Movement.event_id)

        # Get total count (before pagination)
        count_query = select(func.count()).select_from(base_query.subquery())
        total_count = session.execute(count_query).scalar_one()

        # Apply pagination
        offset = (request.page_number - 1) * request.page_size
        paginated_query = base_query.limit(request.page_size).offset(offset)

        # Execute query
        results = session.execute(paginated_query).all()

        # Convert to MovementReportRow objects
        movements = [
            MovementReportRow(
                event_id=row.event_id,
                worker_id=row.worker_id,
                event_type=row.event_type,
                event_date=row.event_date,
                from_org_id=row.from_org_id,
                from_org_name=row.from_org_name,
                to_org_id=row.to_org_id,
                to_org_name=row.to_org_name,
                from_cost_center_id=row.from_cost_center_id,
                from_cost_center_name=row.from_cost_center_name,
                to_cost_center_id=row.to_cost_center_id,
                to_cost_center_name=row.to_cost_center_name,
                from_job_profile_id=row.from_job_profile_id,
                from_job_title=row.from_job_title,
                to_job_profile_id=row.to_job_profile_id,
                to_job_title=row.to_job_title,
                from_position_id=row.from_position_id,
                to_position_id=row.to_position_id,
                created_at=row.created_at.isoformat(),
            )
            for row in results
        ]

        return MovementReportOutput(
            movements=movements,
            total_count=total_count,
            page_size=request.page_size,
            page_number=request.page_number,
            start_date=request.start_date,
            end_date=request.end_date,
        )

    def get_position_report(
        self, session: Session, request: PositionReportInput
    ) -> PositionReportOutput:
        """Generate position vacancy report with full position details.

        This report provides vacancy analysis showing open, filled, and closed
        positions with enriched data from job profiles and organizations.

        Business Logic:
        1. JOIN positions with job_profiles, supervisory_orgs.
        2. Calculate days_open for open positions (DATEDIFF(CURRENT_DATE, created_at)).
        3. Apply filters (org_id, status, job_profile_id).
        4. Aggregate counts: open_positions, filled_positions, closed_positions.
        5. Order by position_id.
        6. Paginate results.

        Args:
            session: Database session
            request: Position report request with filters and pagination

        Returns:
            PositionReportOutput with paginated positions and aggregate counts

        Raises:
            ValueError: If validation fails

        Note:
            Does not commit the transaction (read-only operation).
        """
        # Build base query with JOINs
        base_query = (
            select(
                Position.position_id,
                Position.job_profile_id,
                JobProfile.title.label("job_title"),
                JobProfile.job_family,
                Position.org_id,
                SupervisoryOrg.org_name,
                Position.fte,
                Position.status,
                Position.worker_id,
                Position.created_at,
            )
            .join(JobProfile, Position.job_profile_id == JobProfile.job_profile_id)
            .join(SupervisoryOrg, Position.org_id == SupervisoryOrg.org_id)
        )

        # Apply filters
        filters = []

        if request.org_id:
            filters.append(Position.org_id == request.org_id)

        if request.status:
            filters.append(Position.status == request.status)

        if request.job_profile_id:
            filters.append(Position.job_profile_id == request.job_profile_id)

        if filters:
            base_query = base_query.where(*filters)

        # Order by position_id for deterministic ordering
        base_query = base_query.order_by(Position.position_id)

        # Get total count (before pagination)
        count_query = select(func.count()).select_from(base_query.subquery())
        total_count = session.execute(count_query).scalar_one()

        # Get aggregate counts for all matching positions (before pagination)
        # Query status counts directly from the filtered base query
        subquery = base_query.subquery()
        aggregate_query = select(subquery.c.status, func.count()).group_by(subquery.c.status)
        status_counts = dict(session.execute(aggregate_query).all())
        open_positions = status_counts.get("open", 0)
        filled_positions = status_counts.get("filled", 0)
        closed_positions = status_counts.get("closed", 0)

        # Apply pagination
        offset = (request.page_number - 1) * request.page_size
        paginated_query = base_query.limit(request.page_size).offset(offset)

        # Execute query
        results = session.execute(paginated_query).all()

        # Convert to PositionReportRow objects
        from datetime import UTC, datetime

        positions = []
        for row in results:
            # Calculate days_open for open positions
            # Use UTC for both dates to ensure consistency with database timestamps
            days_open = None
            if row.status == "open":
                days_open = (datetime.now(UTC).date() - row.created_at.date()).days

            positions.append(
                PositionReportRow(
                    position_id=row.position_id,
                    job_profile_id=row.job_profile_id,
                    job_title=row.job_title,
                    job_family=row.job_family,
                    org_id=row.org_id,
                    org_name=row.org_name,
                    fte=row.fte,
                    status=row.status,
                    worker_id=row.worker_id,
                    days_open=days_open,
                    created_at=row.created_at.isoformat(),
                )
            )

        return PositionReportOutput(
            positions=positions,
            total_count=total_count,
            open_positions=open_positions,
            filled_positions=filled_positions,
            closed_positions=closed_positions,
            page_size=request.page_size,
            page_number=request.page_number,
        )

    def get_org_hierarchy_report(
        self, session: Session, request: OrgHierarchyReportInput
    ) -> OrgHierarchyReportOutput:
        """Generate organization hierarchy report with flattened structure.

        Returns the organizational hierarchy as a flattened report table with
        org_level (depth), parent_org_name, and headcount for each organization.

        Business Logic (per BUILD_PLAN.md § 3.5):
        1. Query all orgs (or filter by root_org_id subtree)
        2. Calculate org_level recursively (0 for root, +1 for each level)
        3. For each org, COUNT active workers (employment_status="Active")
        4. JOIN with parent org to get parent_org_name
        5. Order by org_level, org_name
        6. Detect circular references

        Args:
            session: Database session
            request: Report request with optional root_org_id filter

        Returns:
            OrgHierarchyReportOutput with flattened hierarchy and headcount

        Raises:
            ValueError: If root_org_id not found or circular reference detected

        Note:
            Does not commit the transaction (read-only operation).
        """
        # Get all organizations (or filter by root_org_id)
        query = select(SupervisoryOrg)

        if request.root_org_id:
            # Validate root org exists
            root_org = session.execute(
                select(SupervisoryOrg).where(SupervisoryOrg.org_id == request.root_org_id)
            ).scalar_one_or_none()

            if root_org is None:
                raise ValueError(f"E_ORG_001: Root organization not found: {request.root_org_id}")

        # Get all orgs (we'll filter by subtree later if root_org_id is provided)
        all_orgs = session.execute(query).scalars().all()

        # Build org lookup dictionary
        org_dict = {org.org_id: org for org in all_orgs}

        # Calculate org_level for each org using iterative approach
        # Start with root orgs (parent_org_id is None) at level 0
        org_levels: dict[str, int] = {}

        def calculate_level(org_id: str, path: list[str] = None) -> int:
            """Calculate org_level recursively, detecting circular references."""
            if path is None:
                path = []

            # Detect circular reference
            if org_id in path:
                cycle = " -> ".join(path + [org_id])
                raise ValueError(f"Circular org hierarchy detected: {cycle}")

            # If already calculated, return cached value
            if org_id in org_levels:
                return org_levels[org_id]

            # If org not found, return 0 (shouldn't happen, but defensive)
            if org_id not in org_dict:
                return 0

            org = org_dict[org_id]

            # Root org (no parent)
            if org.parent_org_id is None:
                org_levels[org_id] = 0
                return 0

            # Recursive case: level = parent_level + 1
            parent_level = calculate_level(org.parent_org_id, path + [org_id])
            org_levels[org_id] = parent_level + 1
            return org_levels[org_id]

        # Calculate levels for all orgs
        for org_id in org_dict.keys():
            if org_id not in org_levels:
                calculate_level(org_id)

        # Filter by root_org_id subtree if specified
        if request.root_org_id:
            # Get all descendants of root_org_id (including root itself)
            def get_descendants(org_id: str, descendants: set[str] = None) -> set[str]:
                """Get all descendant org IDs recursively."""
                if descendants is None:
                    descendants = set()

                descendants.add(org_id)

                # Find all orgs with this org as parent
                for child_id, child_org in org_dict.items():
                    if child_org.parent_org_id == org_id:
                        get_descendants(child_id, descendants)

                return descendants

            subtree_org_ids = get_descendants(request.root_org_id)
            org_dict = {oid: org_dict[oid] for oid in subtree_org_ids if oid in org_dict}

            # Recalculate org_levels relative to root_org_id (root becomes level 0)
            root_level = org_levels[request.root_org_id]
            for org_id in org_dict.keys():
                if org_id in org_levels:
                    # Adjust level relative to root
                    org_levels[org_id] = org_levels[org_id] - root_level

        # Get headcount for each org (COUNT active workers)
        headcounts: dict[str, int] = {}
        for org_id in org_dict.keys():
            count = session.execute(
                select(func.count(Worker.worker_id)).where(
                    and_(
                        Worker.org_id == org_id,
                        Worker.employment_status == "Active",
                    )
                )
            ).scalar()
            headcounts[org_id] = count or 0

        # Build parent org name lookup
        # Use original all_orgs dict to look up parent names
        # (in case parent is outside filtered subtree)
        all_orgs_dict = {org.org_id: org for org in all_orgs}
        parent_org_names: dict[str, str | None] = {}
        for org_id, org in org_dict.items():
            if org.parent_org_id:
                # Look up parent in original dict (may be outside filtered subtree)
                parent_org = all_orgs_dict.get(org.parent_org_id)
                parent_org_names[org_id] = parent_org.org_name if parent_org else None
            else:
                parent_org_names[org_id] = None

        # Build report rows
        hierarchy_rows = []
        for org_id, org in org_dict.items():
            hierarchy_rows.append(
                OrgHierarchyReportRow(
                    org_id=org.org_id,
                    org_name=org.org_name,
                    org_type=org.org_type,
                    parent_org_id=org.parent_org_id,
                    parent_org_name=parent_org_names.get(org_id),
                    org_level=org_levels[org_id],
                    manager_worker_id=org.manager_worker_id,
                    headcount=headcounts[org_id],
                )
            )

        # Order by org_level, org_name (per BUILD_PLAN.md)
        hierarchy_rows.sort(key=lambda x: (x.org_level, x.org_name))

        return OrgHierarchyReportOutput(
            hierarchy=hierarchy_rows,
            total_count=len(hierarchy_rows),
        )
