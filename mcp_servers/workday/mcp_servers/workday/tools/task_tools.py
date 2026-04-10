"""Task MCP tools for Workday HCM V2.

Implements:
- workday_tasks_create: Create a manual task for a case
- workday_tasks_update: Update task status
"""

from db.models import Case, Milestone, Task
from db.repositories.case_repository import CaseRepository
from db.session import get_session
from mcp_auth import require_roles
from models import CreateTaskInput, TaskOutput, UpdateTaskInput
from sqlalchemy import select
from utils.decorators import make_async_background

from tools.constants import E_CASE_001, E_MILE_001, E_TASK_001


@make_async_background
@require_roles("pre_onboarding_coordinator", "hr_admin")
def workday_tasks_create(request: CreateTaskInput) -> TaskOutput:
    """Create a manual task for a pre-onboarding case with optional milestone link."""
    repository = CaseRepository()

    with get_session() as session:
        # 1. Validate case exists
        case = session.execute(
            select(Case).where(Case.case_id == request.case_id)
        ).scalar_one_or_none()

        if not case:
            raise ValueError(f"{E_CASE_001}: Case '{request.case_id}' not found")

        # 2. If milestone_type provided, validate it exists
        if request.milestone_type:
            milestone = session.execute(
                select(Milestone).where(
                    Milestone.case_id == request.case_id,
                    Milestone.milestone_type == request.milestone_type,
                )
            ).scalar_one_or_none()

            if not milestone:
                raise ValueError(
                    f"{E_MILE_001}: Milestone '{request.milestone_type}' "
                    f"not found for case '{request.case_id}'"
                )

        # 3. Create task via repository (auto-generates task_id)
        try:
            return repository.create_task(session, request)
        except ValueError as e:
            error_msg = str(e)
            if "Case" in error_msg and "not found" in error_msg:
                raise ValueError(f"{E_CASE_001}: Case '{request.case_id}' not found")
            elif "Milestone" in error_msg and "not found" in error_msg:
                raise ValueError(
                    f"{E_MILE_001}: Milestone '{request.milestone_type}' "
                    f"not found for case '{request.case_id}'"
                )
            raise


@make_async_background
@require_roles("pre_onboarding_coordinator", "hr_admin")
def workday_tasks_update(request: UpdateTaskInput) -> TaskOutput:
    """Update a task's status or owner with audit logging."""
    repository = CaseRepository()

    with get_session() as session:
        # 1. Validate task exists
        task = session.execute(
            select(Task).where(Task.task_id == request.task_id)
        ).scalar_one_or_none()

        if not task:
            raise ValueError(f"{E_TASK_001}: Task '{request.task_id}' not found")

        # 2. Update task via repository (handles audit logging)
        try:
            return repository.update_task(session, request)
        except ValueError as e:
            error_msg = str(e)
            if "not found" in error_msg:
                raise ValueError(f"{E_TASK_001}: Task '{request.task_id}' not found")
            raise
