"""Job board tool wiring for the Greenhouse MCP server."""

from auth.permissions import Permission as Perm
from db.models import Degree, Discipline, Job, School
from db.models.jobboard import JobPost
from db.session import get_session
from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError, ToolError
from mcp_auth import public_tool, require_scopes
from schemas import (
    CreateJobPostInput,
    CreateJobPostOutput,
    JobBoardApplyInput,
    JobBoardApplyOutput,
    ListJobBoardJobsInput,
    ListJobBoardJobsOutput,
)
from services.clean_provider import CleanProvider, DuplicateApplicationError, JobNotFoundError
from sqlalchemy import select


@public_tool
async def greenhouse_jobboard_list_jobs(params: ListJobBoardJobsInput) -> ListJobBoardJobsOutput:
    """List all jobs posted on the public job board."""
    provider = CleanProvider()
    result = await provider.list_jobboard_jobs(content=params.content)
    return ListJobBoardJobsOutput(jobs=result.get("jobs", []), meta=result.get("meta", {}))


@public_tool
async def greenhouse_jobboard_apply(params: JobBoardApplyInput) -> JobBoardApplyOutput:
    """Submit a job application through the public job board."""
    provider = CleanProvider()

    def _convert_date(date_dict: dict | None) -> str | None:
        """Convert date dict to ISO 8601 format.

        Args:
            date_dict: Dict with 'month' and 'year' keys.
                Values can be strings or integers, e.g., {'month': 1, 'year': 2020}
                or {'month': '1', 'year': '2020'}

        Returns:
            ISO 8601 date string (YYYY-MM-DD) or None
        """
        if not date_dict or "year" not in date_dict:
            return None

        # Convert year to string and handle both int and str types
        year = str(date_dict["year"])

        # Get month (defaults to 1 if not provided) and convert to string
        month_value = date_dict.get("month", 1)
        month = str(month_value) if month_value else "1"

        # Pad month to 2 digits
        month = month.zfill(2)

        # Use first day of month
        return f"{year}-{month}-01"

    # Convert educations and employments from Pydantic models to dicts
    educations = None
    if params.educations:
        async with get_session() as session:
            educations_list = []
            for e in params.educations:
                # Look up school name from ID
                school_name = None
                if e.school_name_id:
                    school_query = select(School).where(School.id == e.school_name_id)
                    school_result = await session.execute(school_query)
                    school = school_result.scalar_one_or_none()
                    if school:
                        school_name = school.text

                # Look up degree from ID
                degree = None
                if e.degree_id:
                    degree_query = select(Degree).where(Degree.id == e.degree_id)
                    degree_result = await session.execute(degree_query)
                    degree_obj = degree_result.scalar_one_or_none()
                    if degree_obj:
                        degree = degree_obj.text

                # Look up discipline from ID
                discipline = None
                if e.discipline_id:
                    discipline_query = select(Discipline).where(Discipline.id == e.discipline_id)
                    discipline_result = await session.execute(discipline_query)
                    discipline_obj = discipline_result.scalar_one_or_none()
                    if discipline_obj:
                        discipline = discipline_obj.text

                educations_list.append(
                    {
                        "school_name": school_name,
                        "degree": degree,
                        "discipline": discipline,
                        "start_date": _convert_date(e.start_date),
                        "end_date": _convert_date(e.end_date),
                    }
                )
            educations = educations_list

    employments = None
    if params.employments:
        employments = [
            {
                "company_name": e.company_name,
                "title": e.title,
                "start_date": _convert_date(e.start_date),
                "end_date": _convert_date(e.end_date),
                "current": e.current,
            }
            for e in params.employments
        ]

    try:
        result = await provider.jobboard_apply(
            job_post_id=params.job_post_id,
            first_name=params.first_name,
            last_name=params.last_name,
            email=params.email,
            phone=params.phone,
            location=params.location,
            latitude=params.latitude,
            longitude=params.longitude,
            resume_text=params.resume_text,
            resume_url=params.resume_url,
            cover_letter_text=params.cover_letter_text,
            educations=educations,
            employments=employments,
            answers=params.answers,
            mapped_url_token=params.mapped_url_token,
        )
        return JobBoardApplyOutput(
            success=result["success"],
            status=result["status"],
            application_id=result["application_id"],
            candidate_id=result["candidate_id"],
        )
    except JobNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    except DuplicateApplicationError as exc:
        raise ToolError(str(exc)) from exc


@require_scopes(Perm.JOB_CREATE.value)
async def greenhouse_jobboard_create_post(params: CreateJobPostInput) -> CreateJobPostOutput:
    """Create a job posting on the public job board.

    Publishes an internal job to the public job board. The posting must be
    marked as live (default) to appear in job board listings.
    """
    async with get_session() as session:
        # Verify the job exists
        job = await session.get(Job, params.job_id)
        if job is None:
            raise NotFoundError(f"Job with id {params.job_id} does not exist")

        job_post = JobPost(
            job_id=params.job_id,
            title=params.title,
            location_name=params.location_name,
            content=params.content,
            live=params.live,
            internal=params.internal,
        )
        session.add(job_post)
        await session.flush()

        return CreateJobPostOutput(
            id=job_post.id,
            job_id=job_post.job_id,
            title=job_post.title,
            location_name=job_post.location_name,
            content=job_post.content,
            live=job_post.live,
            internal=job_post.internal,
        )


def register_jobboard_tools(mcp: FastMCP) -> None:
    """Register the job board tools with the MCP server."""
    mcp.tool()(greenhouse_jobboard_list_jobs)
    mcp.tool()(greenhouse_jobboard_apply)
    mcp.tool()(greenhouse_jobboard_create_post)
