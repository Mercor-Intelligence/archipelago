"""Provider helpers for Greenhouse user and organization data."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from db.models import (
    Application,
    Candidate,
    CandidateEmailAddress,
    CandidateTag,
    Department,
    HiringTeam,
    InterviewStep,
    InterviewStepDefaultInterviewer,
    Job,
    JobDepartment,
    JobOffice,
    JobOpening,
    JobPost,
    JobStage,
    Note,
    Office,
    RejectionReason,
    Scorecard,
    ScorecardAttribute,
    Source,
    Tag,
    User,
    UserDepartment,
    UserEmail,
    UserOffice,
)
from db.session import get_session
from schemas import (
    ApplicationCreditedToOutput,
    ApplicationJobOutput,
    ApplicationOutput,
    ApplicationRejectionReasonOutput,
    ApplicationSourceOutput,
    ApplicationStageOutput,
    CandidateAddressOutput,
    CandidateApplicationOutput,
    CandidateCurrentStageOutput,
    CandidateEducationOutput,
    CandidateEmailAddressOutput,
    CandidateEmploymentOutput,
    CandidateJobOutput,
    CandidateOutput,
    CandidatePhoneNumberOutput,
    CandidateSocialMediaAddressOutput,
    CandidateUserOutput,
    CandidateWebsiteAddressOutput,
    DefaultInterviewerUserOutput,
    DepartmentOutput,
    InterviewKitOutput,
    InterviewKitQuestionOutput,
    InterviewOutput,
    JobStageOutput,
    OfficeLocationOutput,
    OfficeOutput,
    RejectionReasonTypeOutput,
    UserOutput,
)
from schemas.scorecards import (
    RATING_BUCKETS,
    ScorecardAttributeOutput,
    ScorecardInterviewStepOutput,
    ScorecardOutput,
    ScorecardQuestionOutput,
    ScorecardRatingsOutput,
    ScorecardUserOutput,
)
from services.activity_service import log_candidate_updated, log_note_added, log_tag_added
from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

# Escape character used for SQL LIKE/ILIKE patterns
LIKE_ESCAPE_CHAR = "\\"


def escape_like_pattern(value: str) -> str:
    """Escape SQL LIKE wildcard characters (% and _) in user input.

    This prevents user input containing % or _ from being interpreted
    as SQL wildcards when used in ILIKE/LIKE patterns.

    Use LIKE_ESCAPE_CHAR when calling ilike()/like() to enable escaping.
    """
    return (
        value.replace(LIKE_ESCAPE_CHAR, LIKE_ESCAPE_CHAR + LIKE_ESCAPE_CHAR)
        .replace("%", LIKE_ESCAPE_CHAR + "%")
        .replace("_", LIKE_ESCAPE_CHAR + "_")
    )


class UserNotFoundError(Exception):
    """Raised when the requested user does not exist."""


class JobNotFoundError(Exception):
    """Raised when the requested job does not exist."""


class CandidateNotFoundError(Exception):
    """Raised when the requested candidate does not exist."""


class DuplicateEmailError(Exception):
    """Raised when duplicate email addresses are provided."""


class ApplicationNotFoundError(Exception):
    """Raised when the requested application does not exist."""


class ApplicationAlreadyHiredError(Exception):
    """Raised when trying to hire an already hired application."""


class ApplicationRejectedError(Exception):
    """Raised when trying to hire a rejected application."""


class ApplicationIsProspectError(Exception):
    """Raised when trying to hire a prospect (application without a job)."""


class InvalidJobOpeningError(Exception):
    """Raised when the job opening doesn't belong to the application's job."""


class InvalidDepartmentError(Exception):
    """Raised when a department ID doesn't exist."""


class InvalidOfficeError(Exception):
    """Raised when an office ID doesn't exist."""


class InvalidHiringManagerError(Exception):
    """Raised when a hiring manager user ID doesn't exist."""


class InvalidRecruiterError(Exception):
    """Raised when a recruiter user ID doesn't exist."""


class InvalidStageTransitionError(Exception):
    """Raised when an application stage transition is invalid."""


class StageMismatchError(Exception):
    """Raised when from_stage_id doesn't match the current stage."""


class DuplicateApplicationError(Exception):
    """Raised when a candidate already has an active application for a job."""


class ApplicationAlreadyRejectedError(Exception):
    """Raised when an application is already rejected."""


class InvalidRejectionReasonError(Exception):
    """Raised when an invalid rejection_reason_id is provided."""


class JobNotOpenError(Exception):
    """Raised when trying to create an application for a job that is not open."""


class InvalidStageError(Exception):
    """Raised when a stage ID doesn't belong to the job."""


class SourceNotFoundError(Exception):
    """Raised when a source ID doesn't exist."""


class InvalidInterviewStepError(Exception):
    """Raised when an interview step doesn't belong to the job."""


class DepartmentNotFoundError(Exception):
    """Raised when a department ID doesn't exist."""


class OfficeNotFoundError(Exception):
    """Raised when an office ID doesn't exist."""


class DuplicateUserEmailError(Exception):
    """Raised when a user email already exists."""


class CleanProvider:
    """Encapsulates Greenhouse data access logic for tools."""

    async def list_users(
        self,
        *,
        page: int = 1,
        per_page: int = 100,
        email: str | None = None,
        employee_id: str | None = None,
        created_before: str | None = None,
        created_after: str | None = None,
        updated_before: str | None = None,
        updated_after: str | None = None,
    ) -> list[UserOutput]:
        """Return a page of users matching the provided filters."""
        filters = self._build_filters(
            email=email,
            employee_id=employee_id,
            created_before=created_before,
            created_after=created_after,
            updated_before=updated_before,
            updated_after=updated_after,
        )
        async with get_session() as session:
            query = (
                select(User)
                .options(
                    selectinload(User.emails),
                    selectinload(User.departments)
                    .selectinload(UserDepartment.department)
                    .selectinload(Department.children),
                    selectinload(User.offices)
                    .selectinload(UserOffice.office)
                    .selectinload(Office.children),
                )
                .order_by(User.id)
            )
            if filters:
                query = query.filter(*filters)
            offset = (page - 1) * per_page
            query = query.offset(offset).limit(per_page)
            result = await session.execute(query)
            users = result.scalars().unique().all()
            user_ids = [user.id for user in users if user.id is not None]
            linked_candidate_ids = await self._load_linked_candidate_ids(session, user_ids)
            return [self._serialize_user(user, linked_candidate_ids) for user in users]

    async def count_users(
        self,
        *,
        email: str | None = None,
        employee_id: str | None = None,
        created_before: str | None = None,
        created_after: str | None = None,
        updated_before: str | None = None,
        updated_after: str | None = None,
    ) -> int:
        """Return the total number of users matching the filters."""
        filters = self._build_filters(
            email=email,
            employee_id=employee_id,
            created_before=created_before,
            created_after=created_after,
            updated_before=updated_before,
            updated_after=updated_after,
        )
        async with get_session() as session:
            query = select(func.count()).select_from(User)
            if filters:
                query = query.filter(*filters)
            total = await session.scalar(query)
            return total or 0

    async def get_user(self, user_id: int) -> UserOutput:
        """Return a single user by ID or raise UserNotFoundError."""
        async with get_session() as session:
            query = (
                select(User)
                .options(
                    selectinload(User.emails),
                    selectinload(User.departments)
                    .selectinload(UserDepartment.department)
                    .selectinload(Department.children),
                    selectinload(User.offices)
                    .selectinload(UserOffice.office)
                    .selectinload(Office.children),
                )
                .where(User.id == user_id)
            )
            result = await session.execute(query)
            user = result.scalar_one_or_none()
            if user is None:
                raise UserNotFoundError(f"User with id {user_id} does not exist")
            linked_candidate_ids = await self._load_linked_candidate_ids(session, [user.id])
            return self._serialize_user(user, linked_candidate_ids)

    def _build_filters(
        self,
        *,
        email: str | None,
        employee_id: str | None,
        created_before: str | None,
        created_after: str | None,
        updated_before: str | None,
        updated_after: str | None,
    ) -> list[Any]:
        """Build SQLAlchemy filter clauses from Greenhouse-style filters."""
        filters: list[Any] = []
        if email:
            email_exists = exists(
                select(UserEmail.id)
                .where(UserEmail.user_id == User.id)
                .where(UserEmail.email == email)
            )
            filters.append(or_(User.primary_email_address == email, email_exists))
        if employee_id is not None:
            filters.append(User.employee_id == employee_id)
        if created_before is not None:
            filters.append(User.created_at <= created_before)
        if created_after is not None:
            filters.append(User.created_at >= created_after)
        if updated_before is not None:
            filters.append(User.updated_at <= updated_before)
        if updated_after is not None:
            filters.append(User.updated_at >= updated_after)
        return filters

    async def _load_linked_candidate_ids(
        self, session, user_ids: list[int]
    ) -> dict[int, list[int]]:
        """Map user IDs to the candidate IDs that reference them."""
        if not user_ids:
            return {}
        query = select(
            Candidate.id,
            Candidate.recruiter_id,
            Candidate.coordinator_id,
        ).where(or_(Candidate.recruiter_id.in_(user_ids), Candidate.coordinator_id.in_(user_ids)))
        result = await session.execute(query)
        mapping: dict[int, set[int]] = defaultdict(set)
        for candidate_id, recruiter_id, coordinator_id in result:
            if recruiter_id in user_ids:
                mapping[recruiter_id].add(candidate_id)
            if coordinator_id in user_ids:
                mapping[coordinator_id].add(candidate_id)
        return {users_id: sorted(ids) for users_id, ids in mapping.items()}

    def _serialize_user(self, user: User, linked_candidate_ids: dict[int, list[int]]) -> UserOutput:
        """Serialize a User ORM instance into a Harvest-style model."""
        emails = [entry.email for entry in user.emails if entry.email]
        if user.primary_email_address and user.primary_email_address not in emails:
            emails.insert(0, user.primary_email_address)

        departments = []
        for association in user.departments:
            department = association.department
            if department is None:
                continue
            departments.append(
                DepartmentOutput(
                    id=department.id,
                    name=department.name,
                    parent_id=department.parent_id,
                    child_ids=[child.id for child in department.children if child.id is not None],
                    external_id=department.external_id,
                )
            )

        offices = []
        for association in user.offices:
            office = association.office
            if office is None:
                continue
            offices.append(
                OfficeOutput(
                    id=office.id,
                    name=office.name,
                    location=OfficeLocationOutput(name=office.location_name),
                    primary_contact_user_id=office.primary_contact_user_id,
                    parent_id=office.parent_id,
                    child_ids=[child.id for child in office.children if child.id is not None],
                    external_id=office.external_id,
                )
            )

        name_parts = filter(None, [user.first_name, user.last_name])
        computed_name = user.name or " ".join(name_parts)

        return UserOutput(
            id=user.id,
            name=computed_name,
            first_name=user.first_name,
            last_name=user.last_name,
            primary_email_address=user.primary_email_address,
            emails=emails,
            employee_id=user.employee_id,
            disabled=user.disabled,
            site_admin=user.site_admin,
            created_at=user.created_at,
            updated_at=user.updated_at,
            linked_candidate_ids=linked_candidate_ids.get(user.id, []),
            departments=departments,
            offices=offices,
        )

    async def create_user(
        self,
        *,
        first_name: str,
        last_name: str,
        email: str,
        employee_id: str | None = None,
        department_ids: list[int] | None = None,
        office_ids: list[int] | None = None,
    ) -> UserOutput:
        """Create a new user in the system.

        Args:
            first_name: The user's first name (required)
            last_name: The user's last name (required)
            email: The user's email address (required, must be unique)
            employee_id: External employee ID (optional)
            department_ids: List of department IDs to associate (optional)
            office_ids: List of office IDs to associate (optional)

        Returns:
            UserOutput with created user data

        Raises:
            DuplicateUserEmailError: If email already exists
            DepartmentNotFoundError: If department_id doesn't exist
            OfficeNotFoundError: If office_id doesn't exist
        """
        async with get_session() as session:
            # Check if email already exists (case-insensitive)
            existing_email_query = select(User).where(
                func.lower(User.primary_email_address) == email.lower()
            )
            existing_result = await session.execute(existing_email_query)
            if existing_result.scalar_one_or_none():
                raise DuplicateUserEmailError(f"User with email '{email}' already exists")

            # Validate and deduplicate department_ids if provided
            validated_dept_ids: list[int] = []
            if department_ids:
                unique_dept_ids = list(dict.fromkeys(department_ids))  # Deduplicate
                for dept_id in unique_dept_ids:
                    dept_query = select(Department).where(Department.id == dept_id)
                    dept_result = await session.execute(dept_query)
                    if not dept_result.scalar_one_or_none():
                        raise DepartmentNotFoundError(
                            f"Department with id {dept_id} does not exist"
                        )
                    validated_dept_ids.append(dept_id)

            # Validate and deduplicate office_ids if provided
            validated_office_ids: list[int] = []
            if office_ids:
                unique_office_ids = list(dict.fromkeys(office_ids))  # Deduplicate
                for office_id in unique_office_ids:
                    office_query = select(Office).where(Office.id == office_id)
                    office_result = await session.execute(office_query)
                    if not office_result.scalar_one_or_none():
                        raise OfficeNotFoundError(f"Office with id {office_id} does not exist")
                    validated_office_ids.append(office_id)

            # Create the user - store email in lowercase for case-insensitive uniqueness
            normalized_email = email.lower()
            user = User(
                first_name=first_name,
                last_name=last_name,
                primary_email_address=normalized_email,
                employee_id=employee_id,
                disabled=False,
                site_admin=False,
            )
            session.add(user)
            await session.flush()  # Get the user ID

            # Add department associations
            for dept_id in validated_dept_ids:
                user_dept = UserDepartment(user_id=user.id, department_id=dept_id)
                session.add(user_dept)

            # Add office associations
            for office_id in validated_office_ids:
                user_office = UserOffice(user_id=user.id, office_id=office_id)
                session.add(user_office)

            try:
                await session.commit()
            except IntegrityError:
                # Could be race condition (duplicate email) or foreign key violation
                # (department/office deleted). Re-validate to determine which.
                await session.rollback()

                # Re-check if email now exists (race condition case)
                email_recheck = await session.execute(
                    select(User).where(func.lower(User.primary_email_address) == normalized_email)
                )
                if email_recheck.scalar_one_or_none():
                    raise DuplicateUserEmailError(f"User with email '{email}' already exists")

                # Re-check if departments still exist
                for dept_id in validated_dept_ids:
                    dept_check = await session.execute(
                        select(Department).where(Department.id == dept_id)
                    )
                    if not dept_check.scalar_one_or_none():
                        raise DepartmentNotFoundError(
                            f"Department with id {dept_id} was deleted during operation"
                        )

                # Re-check if offices still exist
                for office_id in validated_office_ids:
                    office_check = await session.execute(
                        select(Office).where(Office.id == office_id)
                    )
                    if not office_check.scalar_one_or_none():
                        raise OfficeNotFoundError(
                            f"Office with id {office_id} was deleted during operation"
                        )

                # Unknown integrity error
                raise

            # Re-fetch user with all relationships for serialization
            return await self.get_user(user.id)

    # -------------------------------------------------------------------------
    # Jobs Methods
    # -------------------------------------------------------------------------

    async def list_jobs(
        self,
        *,
        page: int = 1,
        per_page: int = 100,
        status: str | None = None,
        department_id: int | None = None,
        office_id: int | None = None,
        requisition_id: str | None = None,
        created_before: str | None = None,
        created_after: str | None = None,
        updated_before: str | None = None,
        updated_after: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return a page of jobs matching the provided filters."""
        filters = self._build_job_filters(
            status=status,
            department_id=department_id,
            office_id=office_id,
            requisition_id=requisition_id,
            created_before=created_before,
            created_after=created_after,
            updated_before=updated_before,
            updated_after=updated_after,
        )
        async with get_session() as session:
            query = (
                select(Job)
                .options(
                    selectinload(Job.departments)
                    .selectinload(JobDepartment.department)
                    .selectinload(Department.children),
                    selectinload(Job.offices)
                    .selectinload(JobOffice.office)
                    .selectinload(Office.children),
                    selectinload(Job.hiring_team).selectinload(HiringTeam.user),
                )
                .order_by(Job.id)
            )
            if filters:
                query = query.filter(*filters)
            offset = (page - 1) * per_page
            query = query.offset(offset).limit(per_page)
            result = await session.execute(query)
            jobs = result.scalars().unique().all()
            return [self._serialize_job(job) for job in jobs]

    async def count_jobs(
        self,
        *,
        status: str | None = None,
        department_id: int | None = None,
        office_id: int | None = None,
        requisition_id: str | None = None,
        created_before: str | None = None,
        created_after: str | None = None,
        updated_before: str | None = None,
        updated_after: str | None = None,
    ) -> int:
        """Return the total number of jobs matching the filters."""
        filters = self._build_job_filters(
            status=status,
            department_id=department_id,
            office_id=office_id,
            requisition_id=requisition_id,
            created_before=created_before,
            created_after=created_after,
            updated_before=updated_before,
            updated_after=updated_after,
        )
        async with get_session() as session:
            query = select(func.count()).select_from(Job)
            if filters:
                query = query.filter(*filters)
            total = await session.scalar(query)
            return total or 0

    async def create_job(
        self,
        *,
        name: str,
        template_job_id: int | None = None,
        requisition_id: str | None = None,
        notes: str | None = None,
        anywhere: bool = False,
        department_id: int | None = None,
        office_ids: list[int] | None = None,
        opening_ids: list[str] | None = None,
        number_of_openings: int = 1,
        status: str = "draft",
    ) -> dict[str, Any]:
        """Create a new job with default pipeline stages.

        Args:
            name: Job title (required)
            template_job_id: Job ID to copy from (not yet implemented)
            requisition_id: External requisition ID
            notes: Internal notes about the job
            anywhere: Whether job can be performed anywhere (not yet implemented)
            department_id: Department ID for the job
            office_ids: List of office IDs to link
            opening_ids: External opening IDs for tracking
            number_of_openings: Number of openings for this job
            status: Job status (open, closed, or draft)

        Returns:
            Full job object matching get_job format

        Raises:
            InvalidDepartmentError: If department_id doesn't exist
            InvalidOfficeError: If office_id doesn't exist
        """
        from datetime import UTC, datetime

        # Deduplicate office_ids to prevent IntegrityError on composite keys
        unique_office_ids = list(dict.fromkeys(office_ids)) if office_ids else None

        async with get_session() as session:
            # Validate department ID exists
            if department_id is not None:
                dept_query = select(Department).where(Department.id == department_id)
                dept_result = await session.execute(dept_query)
                if not dept_result.scalar_one_or_none():
                    raise InvalidDepartmentError(
                        f"Department with id {department_id} does not exist"
                    )

            # Validate office IDs exist
            if unique_office_ids:
                for office_id in unique_office_ids:
                    office_query = select(Office).where(Office.id == office_id)
                    office_result = await session.execute(office_query)
                    if not office_result.scalar_one_or_none():
                        raise InvalidOfficeError(f"Office with id {office_id} does not exist")

            # Create the job
            now = datetime.now(UTC).isoformat()
            job = Job(
                name=name,
                requisition_id=requisition_id,
                notes=notes,
                confidential=False,
                status=status,
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            await session.flush()

            # Create default pipeline stages
            default_stages = [
                ("Application Review", 0),
                ("Phone Screen", 1),
                ("Technical Interview", 2),
                ("Onsite", 3),
                ("Offer", 4),
            ]
            for stage_name, priority in default_stages:
                stage = JobStage(
                    job_id=job.id,
                    name=stage_name,
                    priority=priority,
                    active=True,
                    created_at=now,
                    updated_at=now,
                )
                session.add(stage)

            # Link department (single)
            if department_id is not None:
                job_dept = JobDepartment(job_id=job.id, department_id=department_id)
                session.add(job_dept)

            # Link offices
            if unique_office_ids:
                for office_id in unique_office_ids:
                    job_office = JobOffice(job_id=job.id, office_id=office_id)
                    session.add(job_office)

            # Create job openings
            for i in range(number_of_openings):
                opening_id = opening_ids[i] if opening_ids and i < len(opening_ids) else None
                opening = JobOpening(
                    job_id=job.id,
                    opening_id=opening_id,
                    status="open",
                    opened_at=now,
                    created_at=now,
                )
                session.add(opening)

            await session.commit()

        # Return full job using get_job to match response format
        return await self.get_job(job.id)

    async def update_job(
        self,
        job_id: int,
        *,
        name: str | None = None,
        requisition_id: str | None = None,
        notes: str | None = None,
        status: str | None = None,
        department_id: int | None = None,
        office_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """Update an existing job with PATCH semantics.

        Args:
            job_id: Job ID to update (required)
            name: Updated job title
            requisition_id: Updated external requisition ID
            notes: Updated internal notes
            status: Updated job status (open, closed, or draft)
            department_id: Updated department ID (replaces existing)
            office_ids: Updated office IDs (replaces existing)

        Returns:
            Full job object matching get_job format

        Raises:
            JobNotFoundError: If job_id doesn't exist
            InvalidDepartmentError: If department_id doesn't exist
            InvalidOfficeError: If office_id doesn't exist
        """
        from datetime import UTC, datetime

        # Deduplicate office_ids to prevent IntegrityError on composite keys
        # Use 'is not None' to distinguish between None (not provided) and [] (clear all)
        unique_office_ids = list(dict.fromkeys(office_ids)) if office_ids is not None else None

        async with get_session() as session:
            # Load the job
            job = await session.get(Job, job_id)
            if job is None:
                raise JobNotFoundError(f"Job with id {job_id} does not exist")

            # Validate department ID if provided
            if department_id is not None:
                dept_query = select(Department).where(Department.id == department_id)
                dept_result = await session.execute(dept_query)
                if not dept_result.scalar_one_or_none():
                    raise InvalidDepartmentError(
                        f"Department with id {department_id} does not exist"
                    )

            # Validate office IDs if provided
            if unique_office_ids:
                for office_id in unique_office_ids:
                    office_query = select(Office).where(Office.id == office_id)
                    office_result = await session.execute(office_query)
                    if not office_result.scalar_one_or_none():
                        raise InvalidOfficeError(f"Office with id {office_id} does not exist")

            # Update scalar fields with PATCH semantics (only if provided)
            if name is not None:
                job.name = name
            if requisition_id is not None:
                job.requisition_id = requisition_id
            if notes is not None:
                job.notes = notes
            if status is not None:
                job.status = status
                # Update timestamps based on status transitions
                if status == "open" and job.opened_at is None:
                    job.opened_at = datetime.now(UTC).isoformat()
                elif status == "closed" and job.closed_at is None:
                    job.closed_at = datetime.now(UTC).isoformat()

            # Update department (replaces existing)
            if department_id is not None:
                # Delete existing department links
                await session.execute(delete(JobDepartment).where(JobDepartment.job_id == job_id))
                # Add new department link
                job_dept = JobDepartment(job_id=job_id, department_id=department_id)
                session.add(job_dept)

            # Update offices (replaces existing)
            if unique_office_ids is not None:
                # Delete existing office links
                await session.execute(delete(JobOffice).where(JobOffice.job_id == job_id))
                # Add new office links
                for oid in unique_office_ids:
                    job_office = JobOffice(job_id=job_id, office_id=oid)
                    session.add(job_office)

            job.updated_at = datetime.now(UTC).isoformat()
            await session.commit()

        # Return full job using get_job to match response format
        return await self.get_job(job_id)

    async def get_job(self, job_id: int) -> dict[str, Any]:
        """Return a single job by ID or raise JobNotFoundError."""
        async with get_session() as session:
            query = (
                select(Job)
                .options(
                    selectinload(Job.departments)
                    .selectinload(JobDepartment.department)
                    .selectinload(Department.children),
                    selectinload(Job.offices)
                    .selectinload(JobOffice.office)
                    .selectinload(Office.children),
                    selectinload(Job.hiring_team).selectinload(HiringTeam.user),
                    selectinload(Job.openings),
                )
                .where(Job.id == job_id)
            )
            result = await session.execute(query)
            job = result.scalar_one_or_none()
            if job is None:
                raise JobNotFoundError(f"Job with id {job_id} does not exist")
            return self._serialize_job_detail(job)

    def _build_job_filters(
        self,
        *,
        status: str | None,
        department_id: int | None,
        office_id: int | None,
        requisition_id: str | None,
        created_before: str | None,
        created_after: str | None,
        updated_before: str | None,
        updated_after: str | None,
    ) -> list[Any]:
        """Build SQLAlchemy filter clauses for job listing."""
        filters: list[Any] = []
        if status is not None:
            filters.append(Job.status == status)
        if department_id is not None:
            filters.append(
                exists(
                    select(JobDepartment.job_id)
                    .where(JobDepartment.job_id == Job.id)
                    .where(JobDepartment.department_id == department_id)
                )
            )
        if office_id is not None:
            filters.append(
                exists(
                    select(JobOffice.job_id)
                    .where(JobOffice.job_id == Job.id)
                    .where(JobOffice.office_id == office_id)
                )
            )
        if requisition_id is not None:
            filters.append(Job.requisition_id == requisition_id)
        if created_before is not None:
            filters.append(Job.created_at <= created_before)
        if created_after is not None:
            filters.append(Job.created_at >= created_after)
        if updated_before is not None:
            filters.append(Job.updated_at <= updated_before)
        if updated_after is not None:
            filters.append(Job.updated_at >= updated_after)
        return filters

    def _serialize_job(self, job: Job) -> dict[str, Any]:
        """Serialize a Job ORM instance into a Harvest-style dict."""
        return {
            "id": job.id,
            "name": job.name,
            "requisition_id": job.requisition_id,
            "notes": job.notes,
            "confidential": job.confidential,
            "status": job.status,
            "opened_at": job.opened_at,
            "closed_at": job.closed_at,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "is_template": job.is_template,
            "copied_from_id": job.copied_from_id,
            "departments": [
                entry
                for assoc in job.departments
                if (entry := self._serialize_department(assoc)) is not None
            ],
            "offices": [
                entry
                for assoc in job.offices
                if (entry := self._serialize_office(assoc)) is not None
            ],
            "hiring_team": self._build_hiring_team(job),
        }

    def _serialize_job_detail(self, job: Job) -> dict[str, Any]:
        data = self._serialize_job(job)
        data["openings"] = [self._serialize_job_opening(o) for o in job.openings]
        data["custom_fields"] = {}
        data["keyed_custom_fields"] = {}
        return data

    def _serialize_department(self, association: JobDepartment) -> dict[str, Any] | None:
        department = association.department
        if department is None:
            return None
        return {
            "id": department.id,
            "name": department.name,
            "parent_id": department.parent_id,
            "child_ids": [child.id for child in department.children],
            "external_id": department.external_id,
        }

    def _serialize_office(self, association: JobOffice) -> dict[str, Any] | None:
        office = association.office
        if office is None:
            return None
        return {
            "id": office.id,
            "name": office.name,
            "location": {"name": office.location_name},
            "primary_contact_user_id": office.primary_contact_user_id,
            "parent_id": office.parent_id,
            "child_ids": [child.id for child in office.children],
            "external_id": office.external_id,
        }

    def _build_hiring_team(self, job: Job) -> dict[str, list[dict[str, Any]]]:
        hiring_team = {
            "hiring_managers": [],
            "recruiters": [],
            "coordinators": [],
        }
        for member in job.hiring_team:
            if member.user is None:
                continue
            serialized = self._serialize_hiring_team_member(member)
            role_key = {
                "hiring_manager": "hiring_managers",
                "recruiter": "recruiters",
                "coordinator": "coordinators",
            }.get(member.role)
            if role_key:
                hiring_team[role_key].append(serialized)
        return hiring_team

    def _serialize_hiring_team_member(self, member: HiringTeam) -> dict[str, Any]:
        user = member.user
        return {
            "id": user.id,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "name": user.name,
            "employee_id": user.employee_id,
            "responsible": member.responsible,
        }

    def _serialize_job_opening(self, opening: JobOpening) -> dict[str, Any]:
        return {
            "id": opening.id,
            "opening_id": opening.opening_id,
            "status": opening.status,
            "opened_at": opening.opened_at,
            "closed_at": opening.closed_at,
            "application_id": opening.application_id,
            "close_reason_id": opening.close_reason_id,
            "close_reason_name": opening.close_reason_name,
            "close_reason": opening.close_reason_name,
            "created_at": opening.created_at,
        }

    # -------------------------------------------------------------------------
    # Job Stages Methods
    # -------------------------------------------------------------------------

    async def get_job_stages(
        self,
        job_id: int,
        *,
        created_before: str | None = None,
        created_after: str | None = None,
        updated_before: str | None = None,
        updated_after: str | None = None,
    ) -> list[JobStageOutput]:
        """Return all pipeline stages for a job, ordered by priority.

        Args:
            job_id: The ID of the job to get stages for.
            created_before: Filter stages created before this ISO 8601 timestamp.
            created_after: Filter stages created after this ISO 8601 timestamp.
            updated_before: Filter stages updated before this ISO 8601 timestamp.
            updated_after: Filter stages updated after this ISO 8601 timestamp.

        Returns:
            List of JobStageOutput models matching Greenhouse Harvest API format.

        Raises:
            JobNotFoundError: If the job with the given ID does not exist.
        """
        async with get_session() as session:
            # First, verify the job exists
            job_exists = await session.scalar(select(exists().where(Job.id == job_id)))
            if not job_exists:
                raise JobNotFoundError(f"Job with id {job_id} does not exist")

            # Build filters for stages
            filters = self._build_stage_filters(
                job_id=job_id,
                created_before=created_before,
                created_after=created_after,
                updated_before=updated_before,
                updated_after=updated_after,
            )

            # Query stages with all related data eagerly loaded
            query = (
                select(JobStage)
                .options(
                    selectinload(JobStage.interview_steps).selectinload(
                        InterviewStep.kit_questions
                    ),
                    selectinload(JobStage.interview_steps)
                    .selectinload(InterviewStep.default_interviewers)
                    .selectinload(InterviewStepDefaultInterviewer.user),
                )
                .where(*filters)
                .order_by(JobStage.priority)
            )

            result = await session.execute(query)
            stages = result.scalars().unique().all()

            return [self._serialize_stage(stage) for stage in stages]

    def _build_stage_filters(
        self,
        *,
        job_id: int,
        created_before: str | None,
        created_after: str | None,
        updated_before: str | None,
        updated_after: str | None,
    ) -> list[Any]:
        """Build SQLAlchemy filter clauses for job stages."""
        filters: list[Any] = [JobStage.job_id == job_id]
        if created_before is not None:
            filters.append(JobStage.created_at <= created_before)
        if created_after is not None:
            filters.append(JobStage.created_at >= created_after)
        if updated_before is not None:
            filters.append(JobStage.updated_at <= updated_before)
        if updated_after is not None:
            filters.append(JobStage.updated_at >= updated_after)
        return filters

    def _serialize_stage(self, stage: JobStage) -> JobStageOutput:
        """Serialize a JobStage ORM instance into a JobStageOutput model."""
        interviews = []
        for step in stage.interview_steps:
            # Serialize interview kit questions
            questions = [
                InterviewKitQuestionOutput(id=q.id, question=q.question) for q in step.kit_questions
            ]

            # Serialize default interviewers with full user info
            default_interviewer_users = []
            for di in step.default_interviewers:
                user = di.user
                if user:
                    default_interviewer_users.append(
                        DefaultInterviewerUserOutput(
                            id=user.id,
                            first_name=user.first_name,
                            last_name=user.last_name,
                            name=user.name,
                            employee_id=user.employee_id,
                        )
                    )

            # Build interview_kit object
            interview_kit = InterviewKitOutput(
                id=step.interview_kit_id,
                content=step.interview_kit_content,
                questions=questions,
            )

            interviews.append(
                InterviewOutput(
                    id=step.id,
                    name=step.name,
                    schedulable=step.schedulable,
                    estimated_minutes=step.estimated_minutes,
                    default_interviewer_users=default_interviewer_users,
                    interview_kit=interview_kit,
                )
            )

        return JobStageOutput(
            id=stage.id,
            name=stage.name,
            created_at=stage.created_at,
            updated_at=stage.updated_at,
            active=stage.active,
            job_id=stage.job_id,
            priority=stage.priority,
            interviews=interviews,
        )

    # -------------------------------------------------------------------------
    # Candidate Methods
    # -------------------------------------------------------------------------

    async def get_candidate(self, candidate_id: int) -> CandidateOutput:
        """Return a single candidate by ID with all related data.

        Args:
            candidate_id: The ID of the candidate to retrieve.

        Returns:
            CandidateOutput model matching Greenhouse Harvest API format.

        Raises:
            CandidateNotFoundError: If the candidate with the given ID does not exist.
        """
        async with get_session() as session:
            query = (
                select(Candidate)
                .options(
                    selectinload(Candidate.recruiter),
                    selectinload(Candidate.coordinator),
                    selectinload(Candidate.phone_numbers),
                    selectinload(Candidate.email_addresses),
                    selectinload(Candidate.addresses),
                    selectinload(Candidate.website_addresses),
                    selectinload(Candidate.social_media_addresses),
                    selectinload(Candidate.educations),
                    selectinload(Candidate.employments),
                    selectinload(Candidate.tags).selectinload(CandidateTag.tag),
                )
                .where(Candidate.id == candidate_id)
            )
            result = await session.execute(query)
            candidate = result.scalar_one_or_none()

            if candidate is None:
                raise CandidateNotFoundError(f"Candidate with id {candidate_id} does not exist")

            # Load applications separately with their relationships
            apps_query = (
                select(Application)
                .options(
                    selectinload(Application.current_stage),
                    selectinload(Application.job),
                )
                .where(Application.candidate_id == candidate_id)
            )
            apps_result = await session.execute(apps_query)
            applications = apps_result.scalars().unique().all()

            return self._serialize_candidate(candidate, applications)

    async def search_candidates(
        self,
        *,
        page: int = 1,
        per_page: int = 100,
        name: str | None = None,
        email: str | None = None,
        job_id: int | None = None,
        tag: str | None = None,
        created_before: str | None = None,
        created_after: str | None = None,
        updated_before: str | None = None,
        updated_after: str | None = None,
        candidate_ids: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search and filter candidates with various criteria.

        Args:
            page: Page number (1-indexed).
            per_page: Results per page (max 500).
            name: Search by first/last name (case-insensitive, partial match).
            email: Search by email address (partial match).
            job_id: Filter by job ID (candidates with applications to this job).
            tag: Filter by tag name (exact match).
            created_before: Filter by creation date (ISO 8601).
            created_after: Filter by creation date (ISO 8601).
            updated_before: Filter by update date (ISO 8601).
            updated_after: Filter by update date (ISO 8601).

        Returns:
            List of simplified candidate dictionaries.
        """
        async with get_session() as session:
            query = (
                select(Candidate)
                .options(
                    selectinload(Candidate.email_addresses),
                    selectinload(Candidate.tags).selectinload(CandidateTag.tag),
                )
                .order_by(Candidate.id)
            )

            # Build filter clauses
            filters = self._build_candidate_search_filters(
                name=name,
                email=email,
                job_id=job_id,
                tag=tag,
                created_before=created_before,
                created_after=created_after,
                updated_before=updated_before,
                updated_after=updated_after,
                candidate_ids=candidate_ids,
            )
            if filters:
                query = query.where(*filters)

            # Apply pagination
            offset = (page - 1) * per_page
            query = query.offset(offset).limit(per_page)

            result = await session.execute(query)
            candidates = result.scalars().unique().all()

            # Load application IDs for each candidate
            candidate_ids = [c.id for c in candidates]
            app_ids_map = await self._load_application_ids(session, candidate_ids)

            return [
                self._serialize_candidate_search_result(c, app_ids_map.get(c.id, []))
                for c in candidates
            ]

    async def count_candidates(
        self,
        *,
        name: str | None = None,
        email: str | None = None,
        job_id: int | None = None,
        tag: str | None = None,
        created_before: str | None = None,
        created_after: str | None = None,
        updated_before: str | None = None,
        updated_after: str | None = None,
        candidate_ids: str | None = None,
    ) -> int:
        """Return the total number of candidates matching the filters."""
        filters = self._build_candidate_search_filters(
            name=name,
            email=email,
            job_id=job_id,
            tag=tag,
            created_before=created_before,
            created_after=created_after,
            updated_before=updated_before,
            updated_after=updated_after,
            candidate_ids=candidate_ids,
        )
        async with get_session() as session:
            query = select(func.count(Candidate.id.distinct())).select_from(Candidate)
            if filters:
                query = query.where(*filters)
            total = await session.scalar(query)
            return total or 0

    def _build_candidate_search_filters(
        self,
        *,
        name: str | None,
        email: str | None,
        job_id: int | None,
        tag: str | None,
        created_before: str | None,
        created_after: str | None,
        updated_before: str | None,
        updated_after: str | None,
        candidate_ids: str | None,
    ) -> list[Any]:
        """Build SQLAlchemy filter clauses for candidate search."""
        filters: list[Any] = []

        # Name search: case-insensitive partial match on first_name OR last_name
        if name is not None:
            escaped_name = escape_like_pattern(name)
            name_pattern = f"%{escaped_name}%"
            filters.append(
                or_(
                    Candidate.first_name.ilike(name_pattern, escape=LIKE_ESCAPE_CHAR),
                    Candidate.last_name.ilike(name_pattern, escape=LIKE_ESCAPE_CHAR),
                )
            )

        # Email search: partial match in candidate_email_addresses table
        if email is not None:
            escaped_email = escape_like_pattern(email)
            email_pattern = f"%{escaped_email}%"
            filters.append(
                exists(
                    select(CandidateEmailAddress.id)
                    .where(CandidateEmailAddress.candidate_id == Candidate.id)
                    .where(
                        CandidateEmailAddress.value.ilike(email_pattern, escape=LIKE_ESCAPE_CHAR)
                    )
                )
            )

        # Job filter: candidates with any application to the job
        if job_id is not None:
            filters.append(
                exists(
                    select(Application.id)
                    .where(Application.candidate_id == Candidate.id)
                    .where(Application.job_id == job_id)
                )
            )

        # Tag filter: exact match on tag name
        if tag is not None:
            filters.append(
                exists(
                    select(CandidateTag.candidate_id)
                    .join(Tag, CandidateTag.tag_id == Tag.id)
                    .where(CandidateTag.candidate_id == Candidate.id)
                    .where(Tag.name == tag)
                )
            )

        # Date filters
        if created_before is not None:
            filters.append(Candidate.created_at <= created_before)
        if created_after is not None:
            filters.append(Candidate.created_at >= created_after)
        if updated_before is not None:
            filters.append(Candidate.updated_at <= updated_before)
        if updated_after is not None:
            filters.append(Candidate.updated_at >= updated_after)

        # Candidate IDs filter: comma-separated list of IDs
        if candidate_ids is not None:
            ids = []
            for id_str in candidate_ids.split(","):
                id_str = id_str.strip()
                if id_str:
                    try:
                        ids.append(int(id_str))
                    except ValueError:
                        # Skip non-numeric IDs
                        pass
            if ids:
                filters.append(Candidate.id.in_(ids))

        return filters

    async def _load_application_ids(
        self, session, candidate_ids: list[int]
    ) -> dict[int, list[int]]:
        """Load application IDs for a list of candidates."""
        if not candidate_ids:
            return {}
        query = select(Application.candidate_id, Application.id).where(
            Application.candidate_id.in_(candidate_ids)
        )
        result = await session.execute(query)
        mapping: dict[int, list[int]] = defaultdict(list)
        for candidate_id, app_id in result:
            mapping[candidate_id].append(app_id)
        return dict(mapping)

    def _serialize_candidate_search_result(
        self, candidate: Candidate, application_ids: list[int]
    ) -> dict[str, Any]:
        """Serialize a candidate for search results (simplified format)."""
        email_addresses = [{"value": e.value, "type": e.type} for e in candidate.email_addresses]
        tags = [ct.tag.name for ct in candidate.tags if ct.tag is not None]

        return {
            "id": candidate.id,
            "first_name": candidate.first_name,
            "last_name": candidate.last_name,
            "company": candidate.company,
            "title": candidate.title,
            "created_at": candidate.created_at,
            "updated_at": candidate.updated_at,
            "last_activity": candidate.last_activity,
            "is_private": candidate.is_private,
            "application_ids": application_ids,
            "email_addresses": email_addresses,
            "tags": tags,
        }

    def _serialize_candidate(
        self, candidate: Candidate, applications: list[Application]
    ) -> CandidateOutput:
        """Serialize a Candidate ORM instance into a CandidateOutput model."""
        # Serialize phone numbers
        phone_numbers = [
            CandidatePhoneNumberOutput(value=p.value, type=p.type) for p in candidate.phone_numbers
        ]

        # Serialize email addresses
        email_addresses = [
            CandidateEmailAddressOutput(value=e.value, type=e.type)
            for e in candidate.email_addresses
        ]

        # Serialize addresses
        addresses = [
            CandidateAddressOutput(value=a.value, type=a.type) for a in candidate.addresses
        ]

        # Serialize website addresses
        website_addresses = [
            CandidateWebsiteAddressOutput(value=w.value, type=w.type)
            for w in candidate.website_addresses
        ]

        # Serialize social media addresses
        social_media_addresses = [
            CandidateSocialMediaAddressOutput(value=s.value)
            for s in candidate.social_media_addresses
        ]

        # Serialize educations
        educations = [
            CandidateEducationOutput(
                id=edu.id,
                school_name=edu.school_name,
                degree=edu.degree,
                discipline=edu.discipline,
                start_date=edu.start_date,
                end_date=edu.end_date,
            )
            for edu in candidate.educations
        ]

        # Serialize employments
        employments = [
            CandidateEmploymentOutput(
                id=emp.id,
                company_name=emp.company_name,
                title=emp.title,
                start_date=emp.start_date,
                end_date=emp.end_date,
            )
            for emp in candidate.employments
        ]

        # Serialize tags (extract tag names)
        tags = [ct.tag.name for ct in candidate.tags if ct.tag is not None]

        # Serialize recruiter
        recruiter = None
        if candidate.recruiter:
            recruiter = CandidateUserOutput(
                id=candidate.recruiter.id,
                first_name=candidate.recruiter.first_name,
                last_name=candidate.recruiter.last_name,
                name=candidate.recruiter.name,
                employee_id=candidate.recruiter.employee_id,
            )

        # Serialize coordinator
        coordinator = None
        if candidate.coordinator:
            coordinator = CandidateUserOutput(
                id=candidate.coordinator.id,
                first_name=candidate.coordinator.first_name,
                last_name=candidate.coordinator.last_name,
                name=candidate.coordinator.name,
                employee_id=candidate.coordinator.employee_id,
            )

        # Serialize applications
        serialized_applications = []
        application_ids = []
        for app in applications:
            application_ids.append(app.id)

            # Current stage
            current_stage = None
            if app.current_stage:
                current_stage = CandidateCurrentStageOutput(
                    id=app.current_stage.id,
                    name=app.current_stage.name,
                )

            # Jobs array (single job per application in this model)
            jobs = []
            if app.job:
                jobs.append(CandidateJobOutput(id=app.job.id, name=app.job.name))

            serialized_applications.append(
                CandidateApplicationOutput(
                    id=app.id,
                    candidate_id=app.candidate_id,
                    prospect=app.prospect,
                    applied_at=app.applied_at,
                    status=app.status,
                    current_stage=current_stage,
                    jobs=jobs,
                )
            )

        return CandidateOutput(
            id=candidate.id,
            first_name=candidate.first_name,
            last_name=candidate.last_name,
            company=candidate.company,
            title=candidate.title,
            created_at=candidate.created_at,
            updated_at=candidate.updated_at,
            last_activity=candidate.last_activity,
            is_private=candidate.is_private,
            photo_url=candidate.photo_url,
            application_ids=application_ids,
            phone_numbers=phone_numbers,
            addresses=addresses,
            email_addresses=email_addresses,
            website_addresses=website_addresses,
            social_media_addresses=social_media_addresses,
            recruiter=recruiter,
            coordinator=coordinator,
            can_email=candidate.can_email,
            tags=tags,
            applications=serialized_applications,
            educations=educations,
            employments=employments,
        )

    async def create_candidate(
        self,
        *,
        first_name: str,
        last_name: str,
        email_addresses: list[dict],
        company: str | None = None,
        title: str | None = None,
        is_private: bool = False,
        phone_numbers: list[dict] | None = None,
        addresses: list[dict] | None = None,
        website_addresses: list[dict] | None = None,
        social_media_addresses: list[dict] | None = None,
        tags: list[str] | None = None,
        educations: list[dict] | None = None,
        employments: list[dict] | None = None,
        recruiter_id: int | None = None,
        coordinator_id: int | None = None,
        user_id: int | None = None,
    ) -> CandidateOutput:
        """Create a new candidate with all related data.

        Args:
            first_name: Candidate's first name (required).
            last_name: Candidate's last name (required).
            email_addresses: List of email address dicts with 'value' and 'type' (required).
            company: Current company name.
            title: Current job title.
            is_private: Whether the candidate is private.
            phone_numbers: List of phone number dicts with 'value' and 'type'.
            addresses: List of address dicts with 'value' and 'type'.
            website_addresses: List of website dicts with 'value' and 'type'.
            social_media_addresses: List of social media dicts with 'value'.
            tags: List of tag names to apply.
            educations: List of education history dicts.
            employments: List of employment history dicts.
            recruiter_id: User ID of assigned recruiter.
            coordinator_id: User ID of assigned coordinator.
            user_id: User ID of creator for audit trail.

        Returns:
            CandidateOutput model matching Greenhouse Harvest API format.

        Raises:
            UserNotFoundError: If recruiter_id or coordinator_id doesn't exist.
            DuplicateEmailError: If email_addresses contains duplicates.
        """
        from datetime import UTC, datetime

        from db.models import (
            Activity,
            CandidateAddress,
            CandidateEducation,
            CandidateEmailAddress,
            CandidateEmployment,
            CandidatePhoneNumber,
            CandidateSocialMediaAddress,
            CandidateTag,
            CandidateWebsiteAddress,
            Tag,
        )

        async with get_session() as session:
            # Validate recruiter_id exists if provided
            if recruiter_id is not None:
                recruiter_query = select(User).where(User.id == recruiter_id)
                recruiter_result = await session.execute(recruiter_query)
                if recruiter_result.scalar_one_or_none() is None:
                    raise UserNotFoundError(f"Recruiter with id {recruiter_id} does not exist")

            # Validate coordinator_id exists if provided
            if coordinator_id is not None:
                coordinator_query = select(User).where(User.id == coordinator_id)
                coordinator_result = await session.execute(coordinator_query)
                if coordinator_result.scalar_one_or_none() is None:
                    raise UserNotFoundError(f"Coordinator with id {coordinator_id} does not exist")

            # Validate user_id exists if provided (for audit trail)
            if user_id is not None:
                user_query = select(User).where(User.id == user_id)
                user_result = await session.execute(user_query)
                if user_result.scalar_one_or_none() is None:
                    raise UserNotFoundError(f"User with id {user_id} does not exist")

            # Validate no duplicate email addresses within request
            email_values = [e["value"].lower() for e in email_addresses]
            if len(email_values) != len(set(email_values)):
                raise DuplicateEmailError("Duplicate email addresses are not allowed")

            # Check if any email already exists in the database (case-insensitive)
            from sqlalchemy import func

            existing_email_query = select(CandidateEmailAddress).where(
                func.lower(CandidateEmailAddress.value).in_(email_values)
            )
            existing_email_result = await session.execute(existing_email_query)
            existing_email = existing_email_result.scalars().first()
            if existing_email is not None:
                raise DuplicateEmailError(
                    f"Email address '{existing_email.value}' already exists for another candidate"
                )

            # Generate timestamp
            now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")

            # Create candidate
            candidate = Candidate(
                first_name=first_name,
                last_name=last_name,
                company=company,
                title=title,
                is_private=is_private,
                can_email=True,
                recruiter_id=recruiter_id,
                coordinator_id=coordinator_id,
                created_at=now,
                updated_at=now,
                last_activity=now,
            )
            session.add(candidate)
            await session.flush()

            # Add email addresses (required)
            for email in email_addresses:
                email_record = CandidateEmailAddress(
                    candidate_id=candidate.id,
                    value=email["value"],
                    type=email.get("type", "personal"),
                )
                session.add(email_record)

            # Add phone numbers
            if phone_numbers:
                for phone in phone_numbers:
                    phone_record = CandidatePhoneNumber(
                        candidate_id=candidate.id,
                        value=phone["value"],
                        type=phone.get("type", "mobile"),
                    )
                    session.add(phone_record)

            # Add addresses
            if addresses:
                for addr in addresses:
                    addr_record = CandidateAddress(
                        candidate_id=candidate.id,
                        value=addr["value"],
                        type=addr.get("type", "home"),
                    )
                    session.add(addr_record)

            # Add website addresses
            if website_addresses:
                for website in website_addresses:
                    website_record = CandidateWebsiteAddress(
                        candidate_id=candidate.id,
                        value=website["value"],
                        type=website.get("type", "personal"),
                    )
                    session.add(website_record)

            # Add social media addresses
            if social_media_addresses:
                for social in social_media_addresses:
                    social_record = CandidateSocialMediaAddress(
                        candidate_id=candidate.id,
                        value=social["value"],
                    )
                    session.add(social_record)

            # Add tags (create if they don't exist, reuse if they do)
            # Deduplicate tags to avoid primary key constraint violations
            if tags:
                unique_tags = list(dict.fromkeys(tags))  # Preserve order, remove duplicates
                for tag_name in unique_tags:
                    # Check if tag exists
                    tag_query = select(Tag).where(Tag.name == tag_name)
                    tag_result = await session.execute(tag_query)
                    tag = tag_result.scalar_one_or_none()

                    if tag is None:
                        # Create new tag
                        tag = Tag(name=tag_name)
                        session.add(tag)
                        await session.flush()

                    # Link tag to candidate
                    candidate_tag = CandidateTag(
                        candidate_id=candidate.id,
                        tag_id=tag.id,
                    )
                    session.add(candidate_tag)

            # Add educations
            if educations:
                for edu in educations:
                    edu_record = CandidateEducation(
                        candidate_id=candidate.id,
                        school_name=edu.get("school_name"),
                        degree=edu.get("degree"),
                        discipline=edu.get("discipline"),
                        start_date=edu.get("start_date"),
                        end_date=edu.get("end_date"),
                    )
                    session.add(edu_record)

            # Add employments
            if employments:
                for emp in employments:
                    emp_record = CandidateEmployment(
                        candidate_id=candidate.id,
                        company_name=emp.get("company_name"),
                        title=emp.get("title"),
                        start_date=emp.get("start_date"),
                        end_date=emp.get("end_date"),
                    )
                    session.add(emp_record)

            # Create activity record for audit trail
            activity = Activity(
                candidate_id=candidate.id,
                user_id=user_id,
                subject="Candidate created",
                body=f"Candidate {first_name} {last_name} was created",
                created_at=now,
            )
            session.add(activity)

            await session.commit()

        # Use get_candidate to return the full serialized candidate
        return await self.get_candidate(candidate.id)

    async def add_candidate_note(
        self,
        candidate_id: int,
        data: dict[str, Any],
        user_id: int | None = None,
        *,
        persona: str | None = None,
    ) -> dict[str, Any]:
        """Add a note to a candidate with visibility controls."""

        from datetime import UTC, datetime

        body = str(data.get("body", "") or "")
        visibility = data.get("visibility") or "public"
        visibility_options = {"admin_only", "private", "public"}
        if visibility not in visibility_options:
            raise ValueError(f"Invalid visibility value '{visibility}'")

        if not body.strip():
            raise ValueError("Note body cannot be empty")

        async with get_session() as session:
            candidate = await session.get(Candidate, candidate_id)
            if candidate is None:
                raise CandidateNotFoundError(f"Candidate with id {candidate_id} does not exist")

            user = None
            if user_id is not None:
                user = await session.get(User, user_id)
                if user is None:
                    raise UserNotFoundError(f"User with id {user_id} does not exist")

            now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            note = Note(
                candidate_id=candidate_id,
                user_id=user_id,
                body=body,
                visibility=visibility,
                created_at=now,
            )
            session.add(note)

            candidate.last_activity = now
            candidate.updated_at = now

            await session.flush()

            note_preview = body[:100]
            await log_note_added(
                session=session,
                candidate_id=candidate_id,
                note_preview=note_preview,
                persona=persona,
                user_id=user_id,
                candidate_name=candidate.name,
            )

            user_payload = None
            if user is not None:
                user_payload = {
                    "id": user.id,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "name": user.name,
                    "employee_id": user.employee_id,
                }

            return {
                "id": note.id,
                "created_at": now,
                "body": body,
                "user": user_payload,
                "visibility": visibility,
                "private": visibility == "private",
            }

    async def update_candidate(
        self,
        candidate_id: int,
        data: dict[str, Any],
        *,
        persona: str | None = None,
        user_id: int | None = None,
    ) -> CandidateOutput:
        """Update top-level candidate fields with PATCH semantics."""
        from datetime import UTC, datetime

        contact_fields = {
            "phone_numbers",
            "email_addresses",
            "addresses",
            "website_addresses",
            "social_media_addresses",
            "tags",
        }
        ignored_fields = [field for field in data if field in contact_fields]
        update_data = {k: v for k, v in data.items() if k not in contact_fields}

        if not update_data:
            message = "No updatable fields provided"
            if ignored_fields:
                message = (
                    "Contact and tag updates are not supported in this tool. "
                    "Use dedicated tools for contact info or tags."
                )
            raise ValueError(message)

        async with get_session() as session:
            candidate = await session.get(Candidate, candidate_id)
            if candidate is None:
                raise CandidateNotFoundError(f"Candidate with id {candidate_id} does not exist")

            if "recruiter_id" in update_data and update_data["recruiter_id"] is not None:
                recruiter = await session.get(User, update_data["recruiter_id"])
                if recruiter is None:
                    raise UserNotFoundError(
                        f"Recruiter with id {update_data['recruiter_id']} does not exist"
                    )

            if "coordinator_id" in update_data and update_data["coordinator_id"] is not None:
                coordinator = await session.get(User, update_data["coordinator_id"])
                if coordinator is None:
                    raise UserNotFoundError(
                        f"Coordinator with id {update_data['coordinator_id']} does not exist"
                    )

            changes: dict[str, tuple[str | None, str | None]] = {}

            def set_field(field: str) -> None:
                if field not in update_data:
                    return
                new_val = update_data[field]
                if field in {"first_name", "last_name"} and new_val is None:
                    return
                old_val = getattr(candidate, field)
                if new_val != old_val:
                    setattr(candidate, field, new_val)
                    changes[field] = (old_val, new_val)

            set_field("first_name")
            set_field("last_name")
            set_field("company")
            set_field("title")
            set_field("is_private")
            set_field("recruiter_id")
            set_field("coordinator_id")

            now_iso = datetime.now(UTC).isoformat()
            candidate.updated_at = now_iso
            candidate.last_activity = now_iso

            if changes:
                await log_candidate_updated(
                    session=session,
                    candidate_id=candidate_id,
                    changes=changes,
                    persona=persona,
                    user_id=user_id,
                )

            await session.flush()

        return await self.get_candidate(candidate_id)

    # -------------------------------------------------------------------------
    # Application Methods
    # -------------------------------------------------------------------------

    async def list_applications(
        self,
        *,
        page: int = 1,
        per_page: int = 100,
        job_id: int | None = None,
        status: str | None = None,
        candidate_id: int | None = None,
        current_stage_id: int | None = None,
        created_before: str | None = None,
        created_after: str | None = None,
        last_activity_after: str | None = None,
    ) -> list[ApplicationOutput]:
        """List applications with optional filters and pagination."""
        filters = self._build_application_filters(
            job_id=job_id,
            status=status,
            candidate_id=candidate_id,
            current_stage_id=current_stage_id,
            created_before=created_before,
            created_after=created_after,
            last_activity_after=last_activity_after,
        )
        async with get_session() as session:
            query = (
                select(Application)
                .options(
                    selectinload(Application.current_stage),
                    selectinload(Application.job),
                    selectinload(Application.source),
                    selectinload(Application.credited_to),
                    selectinload(Application.rejection_reason),
                )
                .order_by(
                    Application.applied_at.desc().nullslast(),
                    Application.id.desc(),
                )
            )
            if filters:
                query = query.where(*filters)
            offset = (page - 1) * per_page
            query = query.offset(offset).limit(per_page)
            result = await session.execute(query)
            applications = result.scalars().unique().all()
            return [self._serialize_application(app) for app in applications]

    async def count_applications(
        self,
        *,
        job_id: int | None = None,
        status: str | None = None,
        candidate_id: int | None = None,
        current_stage_id: int | None = None,
        created_before: str | None = None,
        created_after: str | None = None,
        last_activity_after: str | None = None,
    ) -> int:
        """Return the total number of applications matching filters."""
        filters = self._build_application_filters(
            job_id=job_id,
            status=status,
            candidate_id=candidate_id,
            current_stage_id=current_stage_id,
            created_before=created_before,
            created_after=created_after,
            last_activity_after=last_activity_after,
        )
        async with get_session() as session:
            query = select(func.count()).select_from(Application)
            if filters:
                query = query.where(*filters)
            total = await session.scalar(query)
            return total or 0

    async def hire_application(
        self,
        application_id: int,
        *,
        opening_id: int | None = None,
        start_date: str | None = None,
        close_reason_id: int | None = None,
    ) -> ApplicationOutput:
        """Mark an application as hired.

        Args:
            application_id: ID of the application to hire
            opening_id: Optional job opening to close
            start_date: Optional start date for the hire
            close_reason_id: Optional reason ID for closing the opening

        Returns:
            ApplicationOutput with hired application data

        Raises:
            ApplicationNotFoundError: If application doesn't exist
            ApplicationAlreadyHiredError: If application is already hired
            ApplicationRejectedError: If application is rejected
            ApplicationIsProspectError: If application is a prospect (no job)
            InvalidJobOpeningError: If opening_id doesn't belong to the job
        """
        from datetime import UTC, datetime

        from services.activity_service import log_application_hired

        async with get_session() as session:
            # Fetch application with related data
            query = (
                select(Application)
                .options(
                    selectinload(Application.current_stage),
                    selectinload(Application.job).selectinload(Job.stages),
                    selectinload(Application.source),
                    selectinload(Application.credited_to),
                    selectinload(Application.candidate),
                )
                .where(Application.id == application_id)
            )
            result = await session.execute(query)
            application = result.scalar_one_or_none()

            if not application:
                raise ApplicationNotFoundError(
                    f"Application with id {application_id} does not exist"
                )

            # Validation checks
            if application.status == "hired":
                raise ApplicationAlreadyHiredError(f"Application {application_id} is already hired")

            if application.status == "rejected":
                raise ApplicationRejectedError(f"Cannot hire rejected application {application_id}")

            if application.prospect or application.job_id is None:
                raise ApplicationIsProspectError(
                    f"Cannot hire prospect application {application_id} (no job assigned)"
                )

            # Validate opening belongs to the job if provided
            opening = None
            if opening_id is not None:
                opening_query = select(JobOpening).where(JobOpening.id == opening_id)
                opening_result = await session.execute(opening_query)
                opening = opening_result.scalar_one_or_none()

                if not opening or opening.job_id != application.job_id:
                    raise InvalidJobOpeningError(
                        f"Opening {opening_id} does not belong to job {application.job_id}"
                    )

            # Find the Offer stage for this job
            offer_stage = None
            if application.job and application.job.stages:
                for stage in application.job.stages:
                    if stage.name == "Offer":
                        offer_stage = stage
                        break

            # Update application
            now = datetime.now(UTC).isoformat()
            application.status = "hired"
            application.hired_at = now
            application.last_activity_at = now

            # Update candidate's last_activity
            if application.candidate:
                application.candidate.last_activity = now

            # Move to Offer stage if found
            if offer_stage:
                application.current_stage_id = offer_stage.id

            # Close the opening if provided
            if opening_id is not None and opening:
                opening.status = "closed"
                opening.closed_at = now
                opening.application_id = application_id
                opening.close_reason_id = close_reason_id
                opening.close_reason_name = "Hired"

            await session.commit()

            # Refresh to get updated relationships
            await session.refresh(application, ["current_stage", "job", "source", "credited_to"])

            # Log activity
            await log_application_hired(
                session=session,
                candidate_id=application.candidate_id,
                application_id=application_id,
                start_date=start_date,
            )
            await session.commit()

            # Build response
            current_stage = None
            if application.current_stage:
                current_stage = ApplicationStageOutput(
                    id=application.current_stage.id,
                    name=application.current_stage.name,
                )

            jobs = []
            if application.job:
                jobs.append(
                    ApplicationJobOutput(
                        id=application.job.id,
                        name=application.job.name,
                    )
                )

            source = None
            if application.source:
                source = ApplicationSourceOutput(
                    id=application.source.id,
                    public_name=application.source.name,
                )

            credited_to = None
            if application.credited_to:
                user = application.credited_to
                first = user.first_name or ""
                last = user.last_name or ""
                credited_to = ApplicationCreditedToOutput(
                    id=user.id,
                    name=f"{first} {last}".strip() or None,
                )

            return ApplicationOutput(
                id=application.id,
                candidate_id=application.candidate_id,
                prospect=application.prospect,
                applied_at=application.applied_at,
                rejected_at=application.rejected_at,
                hired_at=now,
                last_activity_at=application.last_activity_at,
                status=application.status,
                current_stage=current_stage,
                jobs=jobs,
                source=source,
                credited_to=credited_to,
            )

    async def reject_application(
        self,
        application_id: int,
        *,
        rejection_reason_id: int | None = None,
        notes: str | None = None,
    ) -> ApplicationOutput:
        """Reject an application.

        Args:
            application_id: ID of the application to reject
            rejection_reason_id: Optional rejection reason ID
            notes: Optional rejection notes (creates admin_only Note)

        Returns:
            ApplicationOutput with rejected application data

        Raises:
            ApplicationNotFoundError: If application doesn't exist
            ApplicationAlreadyRejectedError: If application is already rejected
            ApplicationAlreadyHiredError: If application is hired
            InvalidRejectionReasonError: If rejection_reason_id doesn't exist
        """
        from datetime import UTC, datetime

        from db.models import Note, RejectionReason
        from services.activity_service import log_application_rejected

        async with get_session() as session:
            # Fetch application with related data
            query = (
                select(Application)
                .options(
                    selectinload(Application.current_stage),
                    selectinload(Application.job),
                    selectinload(Application.source),
                    selectinload(Application.credited_to),
                    selectinload(Application.candidate),
                )
                .where(Application.id == application_id)
            )
            result = await session.execute(query)
            application = result.scalar_one_or_none()

            if not application:
                raise ApplicationNotFoundError(
                    f"Application with id {application_id} does not exist"
                )

            # Validation checks
            if application.status == "rejected":
                raise ApplicationAlreadyRejectedError(
                    f"Application {application_id} is already rejected"
                )

            if application.status == "hired":
                raise ApplicationAlreadyHiredError(
                    f"Cannot reject hired application {application_id}"
                )

            # Validate rejection reason if provided
            rejection_reason = None
            if rejection_reason_id is not None:
                reason_query = select(RejectionReason).where(
                    RejectionReason.id == rejection_reason_id
                )
                reason_result = await session.execute(reason_query)
                rejection_reason = reason_result.scalar_one_or_none()

                if not rejection_reason:
                    raise InvalidRejectionReasonError(
                        f"Rejection reason with id {rejection_reason_id} does not exist"
                    )

            # Update application
            now = datetime.now(UTC).isoformat()
            application.status = "rejected"
            application.rejected_at = now
            application.last_activity_at = now
            if rejection_reason_id is not None:
                application.rejection_reason_id = rejection_reason_id

            # Update candidate's last_activity
            if application.candidate:
                application.candidate.last_activity = now

            # Create note if provided
            if notes:
                note = Note(
                    candidate_id=application.candidate_id,
                    body=f"Rejection notes: {notes}",
                    visibility="admin_only",
                    created_at=now,
                )
                session.add(note)

            await session.commit()

            # Log activity using service function (for middleware audit logging)
            reason_name = rejection_reason.name if rejection_reason else None
            candidate_name = None
            if application.candidate:
                first = application.candidate.first_name or ""
                last = application.candidate.last_name or ""
                candidate_name = f"{first} {last}".strip() or None
            job_name = application.job.name if application.job else None

            await log_application_rejected(
                session=session,
                candidate_id=application.candidate_id,
                application_id=application.id,
                reason=reason_name,
                candidate_name=candidate_name,
                job_name=job_name,
            )
            await session.commit()

            # Refresh to get updated relationships
            await session.refresh(
                application, ["current_stage", "job", "rejection_reason", "source", "credited_to"]
            )

            # Build response
            current_stage = None
            if application.current_stage:
                current_stage = ApplicationStageOutput(
                    id=application.current_stage.id,
                    name=application.current_stage.name,
                )

            jobs = []
            if application.job:
                jobs.append(
                    ApplicationJobOutput(
                        id=application.job.id,
                        name=application.job.name,
                    )
                )

            source = None
            if application.source:
                source = ApplicationSourceOutput(
                    id=application.source.id,
                    public_name=application.source.name,
                )

            credited_to = None
            if application.credited_to:
                first = application.credited_to.first_name or ""
                last = application.credited_to.last_name or ""
                credited_to = ApplicationCreditedToOutput(
                    id=application.credited_to.id,
                    name=f"{first} {last}".strip() or None,
                )

            rejection_reason_output = None
            if rejection_reason:
                rejection_reason_output = ApplicationRejectionReasonOutput(
                    id=rejection_reason.id,
                    name=rejection_reason.name,
                    type=RejectionReasonTypeOutput(
                        id=rejection_reason.type_id,
                        name=rejection_reason.type_name,
                    ),
                )

            return ApplicationOutput(
                id=application.id,
                candidate_id=application.candidate_id,
                prospect=application.prospect,
                applied_at=application.applied_at,
                rejected_at=application.rejected_at,
                hired_at=application.hired_at,
                last_activity_at=application.last_activity_at,
                status=application.status,
                current_stage=current_stage,
                jobs=jobs,
                source=source,
                credited_to=credited_to,
                rejection_reason=rejection_reason_output,
            )

    def _build_application_filters(
        self,
        *,
        job_id: int | None,
        status: str | None,
        candidate_id: int | None,
        current_stage_id: int | None,
        created_before: str | None,
        created_after: str | None,
        last_activity_after: str | None,
    ) -> list[Any]:
        """Build filters for application listing."""
        clauses: list[Any] = []
        if job_id is not None:
            clauses.append(Application.job_id == job_id)
        if status is not None:
            clauses.append(Application.status == status)
        if candidate_id is not None:
            clauses.append(Application.candidate_id == candidate_id)
        if current_stage_id is not None:
            clauses.append(Application.current_stage_id == current_stage_id)
        if created_before is not None:
            clauses.append(Application.created_at <= created_before)
        if created_after is not None:
            clauses.append(Application.created_at >= created_after)
        if last_activity_after is not None:
            clauses.append(Application.last_activity_at >= last_activity_after)
        return clauses

    def _serialize_application(self, application: Application) -> ApplicationOutput:
        """Serialize an Application ORM instance for listing."""
        current_stage = (
            ApplicationStageOutput(
                id=application.current_stage.id, name=application.current_stage.name
            )
            if application.current_stage
            else None
        )

        jobs = []
        if application.job:
            jobs.append(ApplicationJobOutput(id=application.job.id, name=application.job.name))

        source = None
        if application.source:
            source = ApplicationSourceOutput(
                id=application.source.id,
                public_name=getattr(application.source, "public_name", application.source.name),
            )

        credited_to = None
        if application.credited_to:
            credited_to = ApplicationCreditedToOutput(
                id=application.credited_to.id,
                name=application.credited_to.name,
            )

        rejection_reason = None
        if application.rejection_reason:
            rejection_reason = ApplicationRejectionReasonOutput(
                id=application.rejection_reason.id,
                name=application.rejection_reason.name,
                type=RejectionReasonTypeOutput(
                    id=application.rejection_reason.type_id,
                    name=application.rejection_reason.type_name,
                ),
            )

        return ApplicationOutput(
            id=application.id,
            candidate_id=application.candidate_id,
            prospect=application.prospect,
            applied_at=application.applied_at,
            rejected_at=application.rejected_at,
            hired_at=application.hired_at,
            last_activity_at=application.last_activity_at,
            status=application.status,
            current_stage=current_stage,
            jobs=jobs,
            source=source,
            credited_to=credited_to,
            rejection_reason=rejection_reason,
        )

    async def list_feedback(
        self,
        *,
        application_id: int,
        page: int = 1,
        per_page: int = 100,
    ) -> list[ScorecardOutput]:
        """List scorecards submitted for an application."""
        async with get_session() as session:
            exists_query = (
                select(func.count())
                .select_from(Application)
                .where(Application.id == application_id)
            )
            total = await session.scalar(exists_query)
            if not total:
                raise ApplicationNotFoundError(
                    f"Application with id {application_id} does not exist"
                )

            query = (
                select(Scorecard)
                .options(
                    selectinload(Scorecard.attributes),
                    selectinload(Scorecard.questions),
                    selectinload(Scorecard.interview_step),
                    selectinload(Scorecard.interviewer),
                    selectinload(Scorecard.submitted_by),
                )
                .where(Scorecard.application_id == application_id)
                .order_by(
                    Scorecard.submitted_at.desc().nullslast(),
                    Scorecard.id.desc(),
                )
            )
            offset = (page - 1) * per_page
            query = query.offset(offset).limit(per_page)
            result = await session.execute(query)
            scorecards = result.scalars().unique().all()
            return [self._serialize_scorecard(scorecard) for scorecard in scorecards]

    def _serialize_scorecard(self, scorecard: Scorecard) -> ScorecardOutput:
        """Serialize a Scorecard ORM instance into a Harvest-style schema."""
        interview_step = (
            ScorecardInterviewStepOutput(
                id=scorecard.interview_step.id,
                name=scorecard.interview_step.name,
            )
            if scorecard.interview_step
            else None
        )
        attributes = [
            ScorecardAttributeOutput(
                name=attribute.name,
                type=attribute.type,
                rating=attribute.rating,
                note=attribute.note,
            )
            for attribute in scorecard.attributes
        ]
        questions = [
            ScorecardQuestionOutput(
                id=question.id,
                question=question.question,
                answer=question.answer,
            )
            for question in scorecard.questions
        ]
        ratings = ScorecardRatingsOutput(**self._build_rating_summary(scorecard.attributes))

        return ScorecardOutput(
            id=scorecard.id,
            updated_at=scorecard.updated_at,
            created_at=scorecard.created_at,
            interview=scorecard.interview_name,
            interview_step=interview_step,
            candidate_id=scorecard.candidate_id,
            application_id=scorecard.application_id,
            interviewed_at=scorecard.interviewed_at,
            submitted_by=self._serialize_scorecard_user(scorecard.submitted_by),
            interviewer=self._serialize_scorecard_user(scorecard.interviewer),
            submitted_at=scorecard.submitted_at,
            overall_recommendation=scorecard.overall_recommendation,
            attributes=attributes,
            ratings=ratings,
            questions=questions,
        )

    def _serialize_scorecard_user(self, user: User | None) -> ScorecardUserOutput | None:
        """Serialize a user object for interviewer/submitted_by sections."""
        if user is None:
            return None
        return ScorecardUserOutput(
            id=user.id,
            first_name=user.first_name,
            last_name=user.last_name,
            name=user.name,
            employee_id=user.employee_id,
        )

    def _build_rating_summary(self, attributes: list[ScorecardAttribute]) -> dict[str, list[str]]:
        """Group attribute names by their rating for response headers."""
        summary = {rating: [] for rating in RATING_BUCKETS}
        for attribute in attributes:
            bucket = attribute.rating if attribute.rating in RATING_BUCKETS else "no_decision"
            if attribute.name not in summary[bucket]:
                summary[bucket].append(attribute.name)
        return summary

    # -------------------------------------------------------------------------
    # Activity Feed Methods
    # -------------------------------------------------------------------------

    async def get_activity_feed(self, candidate_id: int) -> dict[str, Any]:
        """Return the activity feed for a candidate or raise CandidateNotFoundError.

        Returns notes, emails, and activities for the candidate.
        All arrays are ordered by created_at DESC.
        """
        from db.models import Email, Note

        async with get_session() as session:
            # First check if candidate exists
            candidate_exists_query = select(exists().where(Candidate.id == candidate_id))
            candidate_exists = await session.scalar(candidate_exists_query)
            if not candidate_exists:
                raise CandidateNotFoundError(f"Candidate with id {candidate_id} does not exist")

            # Query notes with user relationship
            notes_query = (
                select(Note)
                .options(selectinload(Note.user))
                .where(Note.candidate_id == candidate_id)
                .order_by(Note.created_at.desc())
            )
            notes_result = await session.execute(notes_query)
            notes = notes_result.scalars().all()

            # Query emails with user relationship
            emails_query = (
                select(Email)
                .options(selectinload(Email.user))
                .where(Email.candidate_id == candidate_id)
                .order_by(Email.created_at.desc())
            )
            emails_result = await session.execute(emails_query)
            emails = emails_result.scalars().all()

            # Query activities with user relationship
            from db.models import Activity

            activities_query = (
                select(Activity)
                .options(selectinload(Activity.user))
                .where(Activity.candidate_id == candidate_id)
                .order_by(Activity.created_at.desc())
            )
            activities_result = await session.execute(activities_query)
            activities = activities_result.scalars().all()

            return {
                "notes": [self._serialize_note(note) for note in notes],
                "emails": [self._serialize_email(email) for email in emails],
                "activities": [self._serialize_activity(activity) for activity in activities],
            }

    def _serialize_activity_user(self, user: User | None) -> dict[str, Any] | None:
        """Serialize a user for activity feed items."""
        if user is None:
            return None
        return {
            "id": user.id,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "name": user.name,
            "employee_id": user.employee_id,
        }

    def _serialize_note(self, note) -> dict[str, Any]:
        """Serialize a Note ORM instance into a Harvest-style dict."""
        return {
            "id": note.id,
            "created_at": note.created_at,
            "body": note.body,
            "user": self._serialize_activity_user(note.user),
            "private": note.visibility == "private",
            "visibility": note.visibility,
        }

    def _serialize_email(self, email) -> dict[str, Any]:
        """Serialize an Email ORM instance into a Harvest-style dict."""
        return {
            "id": email.id,
            "created_at": email.created_at,
            "subject": email.subject,
            "body": email.body,
            "to": email.to_address,
            "from": email.from_address,
            "cc": email.cc_address,
            "user": self._serialize_activity_user(email.user),
        }

    def _serialize_activity(self, activity) -> dict[str, Any]:
        """Serialize an Activity ORM instance into a Harvest-style dict."""
        return {
            "id": activity.id,
            "created_at": activity.created_at,
            "subject": activity.subject,
            "body": activity.body,
            "user": self._serialize_activity_user(activity.user),
        }

    # -------------------------------------------------------------------------
    # Feedback Submission Methods
    # -------------------------------------------------------------------------

    async def submit_feedback(
        self,
        *,
        application_id: int,
        interviewer_id: int,
        overall_recommendation: str,
        interview_step_id: int | None = None,
        interviewed_at: str | None = None,
        attributes: list[dict] | None = None,
        questions: list[dict] | None = None,
    ) -> ScorecardOutput:
        """Submit interview feedback (scorecard) for an application.

        Args:
            application_id: The application to attach feedback to.
            interviewer_id: The user ID of the interviewer.
            overall_recommendation: Rating value (e.g., definitely_not, yes, strong_yes).
            interview_step_id: Optional interview step ID from job pipeline.
            interviewed_at: Optional ISO 8601 datetime of interview.
            attributes: Optional list of attribute ratings.
            questions: Optional list of interview questions/answers.

        Returns:
            ScorecardOutput with the created scorecard.

        Raises:
            ApplicationNotFoundError: If the application doesn't exist.
            UserNotFoundError: If the interviewer doesn't exist.
            InvalidInterviewStepError: If interview_step_id doesn't belong to job.
        """
        from datetime import UTC, datetime

        from db.models import Activity
        from db.models.scorecards import ScorecardQuestion as ScorecardQuestionModel

        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        async with get_session() as session:
            # Validate application exists and get candidate_id
            app_query = (
                select(Application)
                .options(selectinload(Application.candidate))
                .where(Application.id == application_id)
            )
            result = await session.execute(app_query)
            application = result.scalar_one_or_none()
            if not application:
                raise ApplicationNotFoundError(
                    f"Application with id {application_id} does not exist"
                )

            # Validate interviewer exists
            interviewer = await session.get(User, interviewer_id)
            if not interviewer:
                raise UserNotFoundError(f"User with id {interviewer_id} does not exist")

            # Validate interview_step_id belongs to job (if provided)
            interview_step = None
            interview_name = None
            if interview_step_id is not None:
                step_query = (
                    select(InterviewStep)
                    .options(selectinload(InterviewStep.stage))
                    .where(InterviewStep.id == interview_step_id)
                )
                step_result = await session.execute(step_query)
                interview_step = step_result.scalar_one_or_none()

                if not interview_step:
                    raise InvalidInterviewStepError(
                        f"Interview step with id {interview_step_id} does not exist"
                    )

                # Check if step's stage belongs to the application's job
                if interview_step.stage and interview_step.stage.job_id != application.job_id:
                    raise InvalidInterviewStepError(
                        f"Interview step {interview_step_id} does not belong to "
                        f"job {application.job_id}"
                    )

                interview_name = interview_step.name

            # Create the scorecard
            scorecard = Scorecard(
                application_id=application_id,
                candidate_id=application.candidate_id,
                interview_step_id=interview_step_id,
                interview_name=interview_name,
                interviewer_id=interviewer_id,
                submitted_by_id=interviewer_id,  # Submitter is the interviewer
                overall_recommendation=overall_recommendation,
                interviewed_at=interviewed_at,
                submitted_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(scorecard)
            await session.flush()

            # Create attributes
            if attributes:
                for attr_data in attributes:
                    attr = ScorecardAttribute(
                        scorecard_id=scorecard.id,
                        name=attr_data["name"],
                        type=attr_data.get("type", "Skills"),
                        rating=attr_data["rating"],
                        note=attr_data.get("note"),
                    )
                    session.add(attr)

            # Create questions
            if questions:
                for q_data in questions:
                    question = ScorecardQuestionModel(
                        scorecard_id=scorecard.id,
                        question=q_data["question"],
                        answer=q_data["answer"],
                    )
                    session.add(question)

            # Update application's last_activity_at
            application.last_activity_at = now

            # Update candidate's last_activity
            candidate = application.candidate
            candidate.last_activity = now

            # Create activity record
            candidate_name = f"{candidate.first_name} {candidate.last_name}"
            interviewer_name = f"{interviewer.first_name} {interviewer.last_name}"
            activity = Activity(
                candidate_id=application.candidate_id,
                application_id=application_id,
                user_id=interviewer_id,
                subject=f"Scorecard submitted by {interviewer_name}",
                body=(
                    f"Scorecard submitted for {candidate_name} "
                    f"with recommendation: {overall_recommendation}"
                ),
                created_at=now,
            )
            session.add(activity)

            await session.flush()

            # Reload scorecard with relationships for serialization
            reload_query = (
                select(Scorecard)
                .options(
                    selectinload(Scorecard.attributes),
                    selectinload(Scorecard.questions),
                    selectinload(Scorecard.interview_step),
                    selectinload(Scorecard.interviewer),
                    selectinload(Scorecard.submitted_by),
                )
                .where(Scorecard.id == scorecard.id)
            )
            reload_result = await session.execute(reload_query)
            scorecard = reload_result.scalar_one()

            return self._serialize_scorecard(scorecard)

    # -------------------------------------------------------------------------
    # Application Advance Methods
    # -------------------------------------------------------------------------

    async def advance_application(
        self,
        application_id: int,
        *,
        from_stage_id: int | None = None,
        to_stage_id: int | None = None,
    ) -> ApplicationOutput:
        """Advance an application to the next or specified stage.

        Args:
            application_id: The ID of the application to advance.
            from_stage_id: Optional current stage ID for validation (race condition prevention).
            to_stage_id: Optional target stage ID. If not provided, auto-advances to next stage.

        Returns:
            ApplicationOutput with updated stage information.

        Raises:
            ApplicationNotFoundError: If the application doesn't exist.
            InvalidStageTransitionError: If the transition is invalid
                (rejected/hired, last stage, etc.)
            StageMismatchError: If from_stage_id doesn't match current stage.
        """
        from datetime import UTC, datetime

        from db.models import Activity

        async with get_session() as session:
            # Fetch the application with relationships
            query = (
                select(Application)
                .options(
                    selectinload(Application.current_stage),
                    selectinload(Application.job),
                    selectinload(Application.source),
                    selectinload(Application.credited_to),
                    selectinload(Application.candidate),
                )
                .where(Application.id == application_id)
            )
            result = await session.execute(query)
            application = result.scalar_one_or_none()

            if application is None:
                raise ApplicationNotFoundError(
                    f"Application with id {application_id} does not exist"
                )

            # Check application status - cannot advance rejected or hired applications
            if application.status == "rejected":
                raise InvalidStageTransitionError("Cannot advance a rejected application")
            if application.status == "hired":
                raise InvalidStageTransitionError("Cannot advance a hired application")

            # Validate from_stage_id if provided
            if from_stage_id is not None:
                if application.current_stage_id != from_stage_id:
                    raise StageMismatchError(
                        f"Current stage ID ({application.current_stage_id}) does not match "
                        f"from_stage_id ({from_stage_id})"
                    )

            # Get job stages ordered by priority
            stages_query = (
                select(JobStage)
                .where(JobStage.job_id == application.job_id)
                .order_by(JobStage.priority)
            )
            stages_result = await session.execute(stages_query)
            stages = stages_result.scalars().all()

            if not stages:
                raise InvalidStageTransitionError("No stages found for this job")

            # Find current stage index for validation
            current_stage_idx = None
            for idx, stage in enumerate(stages):
                if stage.id == application.current_stage_id:
                    current_stage_idx = idx
                    break

            if current_stage_idx is None:
                # Current stage not found in job's pipeline - data integrity issue
                curr_id = application.current_stage_id
                raise InvalidStageTransitionError(
                    f"Current stage (id={curr_id}) not found in job pipeline"
                )

            # Determine target stage
            if to_stage_id is not None:
                # Explicit advance: validate to_stage_id belongs to the job
                target_stage = None
                target_stage_idx = None
                for idx, stage in enumerate(stages):
                    if stage.id == to_stage_id:
                        target_stage = stage
                        target_stage_idx = idx
                        break

                if target_stage is None:
                    raise InvalidStageTransitionError(
                        f"Stage with id {to_stage_id} does not belong to this job"
                    )

                # Validate forward movement only (advance cannot go backward)
                if target_stage_idx <= current_stage_idx:
                    raise InvalidStageTransitionError(
                        "Cannot advance backward. Use move endpoint to go to earlier stages."
                    )
            else:
                # Auto-advance: move to next stage by priority
                if current_stage_idx >= len(stages) - 1:
                    # Already at last stage
                    raise InvalidStageTransitionError("Cannot advance: already at the last stage")
                else:
                    target_stage = stages[current_stage_idx + 1]

            # Get candidate and job names for activity record
            candidate_name = (
                f"{application.candidate.first_name} {application.candidate.last_name}"
                if application.candidate
                else "Candidate"
            )
            job_name = application.job.name if application.job else "Job"

            # Update application
            now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            application.current_stage_id = target_stage.id
            application.last_activity_at = now

            # Update candidate's last_activity
            if application.candidate:
                application.candidate.last_activity = now

            # Create activity record
            activity = Activity(
                candidate_id=application.candidate_id,
                application_id=application.id,
                subject=f"Application stage changed to {target_stage.name}",
                body=f"{candidate_name} was moved into {target_stage.name} for {job_name}",
                created_at=now,
            )
            session.add(activity)

            await session.commit()

            # Refresh to get updated relationships
            await session.refresh(application)

        # Fetch and return the updated application
        return await self.get_application(application_id)

    async def get_application(self, application_id: int) -> ApplicationOutput:
        """Return a single application by ID.

        Args:
            application_id: The ID of the application to retrieve.

        Returns:
            ApplicationOutput model matching Greenhouse Harvest API format.

        Raises:
            ApplicationNotFoundError: If the application doesn't exist.
        """
        async with get_session() as session:
            query = (
                select(Application)
                .options(
                    selectinload(Application.current_stage),
                    selectinload(Application.job),
                    selectinload(Application.source),
                    selectinload(Application.credited_to),
                    selectinload(Application.rejection_reason),
                )
                .where(Application.id == application_id)
            )
            result = await session.execute(query)
            application = result.scalar_one_or_none()

            if application is None:
                raise ApplicationNotFoundError(
                    f"Application with id {application_id} does not exist"
                )

            return self._serialize_application(application)

    async def create_application(
        self,
        *,
        candidate_id: int,
        job_id: int,
        source_id: int | None = None,
        initial_stage_id: int | None = None,
        recruiter_id: int | None = None,
        coordinator_id: int | None = None,
        referrer: dict | None = None,
        attachments: list[dict] | None = None,
        answers: list[dict] | None = None,
    ) -> ApplicationOutput:
        """Create an application for a candidate to a job.

        Args:
            candidate_id: ID of the candidate applying.
            job_id: ID of the job to apply for.
            source_id: Optional source ID for attribution.
            initial_stage_id: Optional starting stage (defaults to first stage).
            recruiter_id: Optional assigned recruiter user ID.
            coordinator_id: Optional assigned coordinator user ID.
            referrer: Optional referrer info (not yet fully implemented).
            attachments: Optional list of attachments to add to the candidate.
                Each dict should have: filename (required), url, type.
            answers: Optional list of answers to job application questions.
                Each dict should have: question (required), answer.

        Returns:
            ApplicationOutput with full application details.

        Raises:
            CandidateNotFoundError: If candidate_id doesn't exist.
            JobNotFoundError: If job_id doesn't exist.
            JobNotOpenError: If job is not in 'open' status or has no pipeline stages.
            DuplicateApplicationError: If candidate has active application to this job.
            InvalidStageError: If initial_stage_id doesn't belong to the job.
            SourceNotFoundError: If source_id doesn't exist.
            UserNotFoundError: If recruiter_id or coordinator_id doesn't exist.
        """
        from datetime import UTC, datetime

        from db.models import Activity, ApplicationAnswer, CandidateAttachment

        async with get_session() as session:
            # Validate candidate exists
            candidate = await session.get(Candidate, candidate_id)
            if candidate is None:
                raise CandidateNotFoundError(f"Candidate with id {candidate_id} does not exist")

            # Validate job exists
            job = await session.get(Job, job_id)
            if job is None:
                raise JobNotFoundError(f"Job with id {job_id} does not exist")

            # Validate job is open
            if job.status != "open":
                raise JobNotOpenError(
                    f"Job {job_id} is not open for applications (status: {job.status})"
                )

            # Check for duplicate active application
            existing_app = await session.scalar(
                select(
                    exists(
                        select(Application.id)
                        .where(Application.candidate_id == candidate_id)
                        .where(Application.job_id == job_id)
                        .where(Application.status == "active")
                    )
                )
            )
            if existing_app:
                raise DuplicateApplicationError(
                    f"Candidate {candidate_id} already has an active application to job {job_id}"
                )

            # Validate source if provided
            if source_id is not None:
                source = await session.get(Source, source_id)
                if source is None:
                    raise SourceNotFoundError(f"Source with id {source_id} does not exist")

            # Validate recruiter if provided
            if recruiter_id is not None:
                recruiter = await session.get(User, recruiter_id)
                if recruiter is None:
                    raise UserNotFoundError(f"Recruiter with id {recruiter_id} does not exist")

            # Validate coordinator if provided
            if coordinator_id is not None:
                coordinator = await session.get(User, coordinator_id)
                if coordinator is None:
                    raise UserNotFoundError(f"Coordinator with id {coordinator_id} does not exist")

            # Get job stages
            stages_query = (
                select(JobStage).where(JobStage.job_id == job_id).order_by(JobStage.priority)
            )
            stages_result = await session.execute(stages_query)
            stages = stages_result.scalars().all()

            if not stages:
                raise JobNotOpenError(f"Job {job_id} has no pipeline stages configured")

            # Determine initial stage
            if initial_stage_id is not None:
                # Validate stage belongs to the job
                stage_ids = [s.id for s in stages]
                if initial_stage_id not in stage_ids:
                    raise InvalidStageError(
                        f"Stage {initial_stage_id} does not belong to job {job_id}"
                    )
                current_stage_id = initial_stage_id
            else:
                # Use first stage (lowest priority)
                current_stage_id = stages[0].id

            # Create application
            now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            application = Application(
                candidate_id=candidate_id,
                job_id=job_id,
                current_stage_id=current_stage_id,
                status="active",
                prospect=False,
                source_id=source_id,
                recruiter_id=recruiter_id,
                coordinator_id=coordinator_id,
                applied_at=now,
                last_activity_at=now,
                created_at=now,
            )
            session.add(application)
            await session.flush()

            # Update candidate's last_activity
            candidate.last_activity = now
            candidate.updated_at = now

            # Create activity record
            job_name = job.name or f"Job {job_id}"
            candidate_name = f"{candidate.first_name} {candidate.last_name}"
            activity = Activity(
                candidate_id=candidate_id,
                application_id=application.id,
                subject=f"{candidate_name} applied for {job_name}",
                body=f"{candidate_name} submitted an application for {job_name}",
                created_at=now,
            )
            session.add(activity)

            # Create candidate attachments if provided
            if attachments:
                for attachment in attachments:
                    candidate_attachment = CandidateAttachment(
                        candidate_id=candidate_id,
                        filename=attachment.get("filename", "attachment"),
                        url=attachment.get("url"),
                        type=attachment.get("type"),
                        created_at=now,
                    )
                    session.add(candidate_attachment)

            # Store application answers if provided
            if answers:
                for answer_data in answers:
                    answer_record = ApplicationAnswer(
                        application_id=application.id,
                        question=answer_data.get("question", ""),
                        answer=answer_data.get("answer"),
                    )
                    session.add(answer_record)

            await session.commit()

        # Return full application using get_application
        return await self.get_application(application.id)

    # -------------------------------------------------------------------------
    # Job Board Methods
    # -------------------------------------------------------------------------

    async def list_jobboard_jobs(self, *, content: bool = False) -> dict[str, Any]:
        """List all open jobs with live external postings (public job board).

        Args:
            content: If True, include the full job description HTML in each job.

        Returns:
            Dict containing:
                - jobs: List of job board job dicts
                - meta: Dict with total count

        Notes:
            - Only returns jobs with status='open'
            - Only returns jobs with at least one live external posting
            - No pagination (returns all matching jobs)
        """
        async with get_session() as session:
            # Query jobs with live external postings
            query = (
                select(Job, JobPost)
                .join(JobPost, Job.id == JobPost.job_id)
                .options(
                    selectinload(Job.departments)
                    .selectinload(JobDepartment.department)
                    .selectinload(Department.children),
                    selectinload(Job.offices)
                    .selectinload(JobOffice.office)
                    .selectinload(Office.children),
                )
                .where(Job.status == "open")
                .where(JobPost.live == True)  # noqa: E712
                .where(JobPost.internal == False)  # noqa: E712
                .order_by(Job.id)
            )

            result = await session.execute(query)
            rows = result.unique().all()

            jobs = []
            for job, job_post in rows:
                job_dict = self._serialize_jobboard_job(job, job_post, include_content=content)
                jobs.append(job_dict)

            return {
                "jobs": jobs,
                "meta": {"total": len(jobs)},
            }

    def _serialize_jobboard_job(
        self, job: Job, job_post: JobPost, *, include_content: bool = False
    ) -> dict[str, Any]:
        """Serialize a Job and JobPost for the public job board API.

        Args:
            job: The Job ORM instance
            job_post: The JobPost ORM instance
            include_content: Whether to include the full job description HTML

        Returns:
            Dict matching Greenhouse Job Board API format
        """
        # Serialize departments
        departments = []
        for assoc in job.departments:
            dept = assoc.department
            if dept:
                departments.append(
                    {
                        "id": dept.id,
                        "name": dept.name,
                        "parent_id": dept.parent_id,
                        "child_ids": [child.id for child in getattr(dept, "children", [])],
                    }
                )

        # Serialize offices
        offices = []
        for assoc in job.offices:
            office = assoc.office
            if office:
                offices.append(
                    {
                        "id": office.id,
                        "name": office.name,
                        "location": office.location_name,
                        "parent_id": office.parent_id,
                        "child_ids": [child.id for child in getattr(office, "children", [])],
                    }
                )

        # Build job dict
        job_dict = {
            "id": job_post.id,
            "title": job_post.title,
            "location": {"name": job_post.location_name},
            "updated_at": job_post.updated_at,
            "absolute_url": f"https://boards.greenhouse.io/company/jobs/{job_post.id}",
            "internal_job_id": job.id,
            "metadata": [],
            "requisition_id": job.requisition_id,
            "departments": departments,
            "offices": offices,
            "language": job_post.language or "en",
        }

        # Optionally include content
        if include_content:
            job_dict["content"] = job_post.content

        return job_dict

    async def jobboard_apply(
        self,
        *,
        job_post_id: int,
        first_name: str,
        last_name: str,
        email: str,
        phone: str | None = None,
        location: str | None = None,
        latitude: str | None = None,
        longitude: str | None = None,
        resume_text: str | None = None,
        resume_url: str | None = None,
        cover_letter_text: str | None = None,
        educations: list[dict] | None = None,
        employments: list[dict] | None = None,
        answers: list[dict] | None = None,
        mapped_url_token: str | None = None,
    ) -> dict[str, Any]:
        """Submit a job board application (public endpoint simulation).

        This simulates the candidate self-apply flow from a public job board.
        Creates or finds a candidate by email, then creates an application.

        Args:
            job_post_id: Job post ID from job board (not internal_job_id).
            first_name: Applicant's first name.
            last_name: Applicant's last name.
            email: Applicant's email address.
            phone: Applicant's phone number.
            location: Applicant's location/address.
            latitude: Hidden field for location latitude.
            longitude: Hidden field for location longitude.
            resume_text: Resume content as plain text.
            resume_url: URL to hosted resume file.
            cover_letter_text: Cover letter as plain text.
            educations: Education history entries.
            employments: Employment history entries.
            answers: Answers to job post questions.
            mapped_url_token: gh_src tracking parameter for attribution.

        Returns:
            Dict with success, status, application_id, and candidate_id.

        Raises:
            JobNotFoundError: If the job post doesn't exist or job is not open.
            DuplicateApplicationError: If candidate already has active application.
        """
        from datetime import UTC, datetime

        from db.models import (
            Activity,
            ApplicationAnswer,
            CandidateAddress,
            CandidateAttachment,
            CandidateEducation,
            CandidateEmailAddress,
            CandidateEmployment,
            CandidatePhoneNumber,
        )

        async with get_session() as session:
            # Look up job from job post
            job_post_query = (
                select(JobPost).options(selectinload(JobPost.job)).where(JobPost.id == job_post_id)
            )
            job_post_result = await session.execute(job_post_query)
            job_post = job_post_result.scalar_one_or_none()

            if job_post is None:
                raise JobNotFoundError(
                    f"Job post with id {job_post_id} does not exist or is not live"
                )

            # Verify job has live external posting and is open
            if not job_post.live or job_post.internal:
                raise JobNotFoundError(
                    f"Job post with id {job_post_id} does not have a live external posting"
                )

            job = job_post.job
            if job is None:
                raise JobNotFoundError(f"Job associated with post {job_post_id} does not exist")

            if job.status != "open":
                raise JobNotFoundError(f"Job {job.id} is not open for applications")

            # Find existing candidate by email or create new one
            email_query = select(CandidateEmailAddress).where(
                func.lower(CandidateEmailAddress.value) == email.lower()
            )
            email_result = await session.execute(email_query)
            existing_email = email_result.scalar_one_or_none()

            now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")

            if existing_email is not None:
                # Use existing candidate
                candidate_id = existing_email.candidate_id

                # Check for duplicate ACTIVE application to same job
                duplicate_check = await session.scalar(
                    select(
                        exists(
                            select(Application.id)
                            .where(Application.candidate_id == candidate_id)
                            .where(Application.job_id == job.id)
                            .where(Application.status == "active")
                        )
                    )
                )
                if duplicate_check:
                    raise DuplicateApplicationError(
                        f"Candidate {candidate_id} already has an active application "
                        f"to job {job.id}"
                    )
            else:
                # Create new candidate
                candidate = Candidate(
                    first_name=first_name,
                    last_name=last_name,
                    can_email=True,
                    created_at=now,
                    updated_at=now,
                    last_activity=now,
                )
                session.add(candidate)
                await session.flush()

                candidate_id = candidate.id

                # Add email address (only for new candidates)
                email_record = CandidateEmailAddress(
                    candidate_id=candidate_id,
                    value=email,
                    type="personal",
                )
                session.add(email_record)

            # Add/update candidate profile data (for both new and existing candidates)
            # This ensures returning applicants can update their information

            # Add phone number if provided (check for duplicates first)
            if phone:
                # Check if this phone number already exists for this candidate
                existing_phone_query = select(CandidatePhoneNumber).where(
                    CandidatePhoneNumber.candidate_id == candidate_id,
                    CandidatePhoneNumber.value == phone,
                )
                existing_phone_result = await session.execute(existing_phone_query)
                existing_phone = existing_phone_result.scalar_one_or_none()

                if existing_phone is None:
                    phone_record = CandidatePhoneNumber(
                        candidate_id=candidate_id,
                        value=phone,
                        type="mobile",
                    )
                    session.add(phone_record)

            # Add address if provided (check for duplicates first)
            if location:
                # Check if this address already exists for this candidate
                existing_addr_query = select(CandidateAddress).where(
                    CandidateAddress.candidate_id == candidate_id,
                    CandidateAddress.value == location,
                )
                existing_addr_result = await session.execute(existing_addr_query)
                existing_addr = existing_addr_result.scalar_one_or_none()

                if existing_addr is None:
                    addr_record = CandidateAddress(
                        candidate_id=candidate_id,
                        value=location,
                        type="home",
                    )
                    session.add(addr_record)

            # Add educations if provided
            if educations:
                for edu in educations:
                    edu_record = CandidateEducation(
                        candidate_id=candidate_id,
                        school_name=edu.get("school_name"),
                        degree=edu.get("degree"),
                        discipline=edu.get("discipline"),
                        start_date=edu.get("start_date"),
                        end_date=edu.get("end_date"),
                    )
                    session.add(edu_record)

            # Add employments if provided
            if employments:
                for emp in employments:
                    emp_record = CandidateEmployment(
                        candidate_id=candidate_id,
                        company_name=emp.get("company_name"),
                        title=emp.get("title"),
                        start_date=emp.get("start_date"),
                        end_date=emp.get("end_date"),
                    )
                    session.add(emp_record)

            # Get first stage of job pipeline (lowest priority)
            first_stage_query = (
                select(JobStage)
                .where(JobStage.job_id == job.id)
                .order_by(JobStage.priority)
                .limit(1)
            )
            first_stage_result = await session.execute(first_stage_query)
            first_stage = first_stage_result.scalar_one_or_none()

            if first_stage is None:
                raise JobNotFoundError(f"No stages found for job {job.id}")

            # Get or create "Jobs page on your website" source
            source_query = select(Source).where(Source.name == "Jobs page on your website")
            source_result = await session.execute(source_query)
            source = source_result.scalar_one_or_none()

            if source is None:
                # Create Job Board source
                source = Source(name="Jobs page on your website")
                session.add(source)
                await session.flush()

            # Create application
            application = Application(
                candidate_id=candidate_id,
                job_id=job.id,
                job_post_id=job_post_id,
                current_stage_id=first_stage.id,
                status="active",
                prospect=False,
                applied_at=now,
                last_activity_at=now,
                source_id=source.id,
                location_address=location,
            )
            session.add(application)
            await session.flush()

            # Store application answers if provided
            if answers:
                for answer_data in answers:
                    answer_record = ApplicationAnswer(
                        application_id=application.id,
                        question=answer_data.get("question", ""),
                        answer=answer_data.get("answer"),
                    )
                    session.add(answer_record)

            # Create attachment records for resume and cover letter if provided
            if resume_text:
                resume_attachment = CandidateAttachment(
                    candidate_id=candidate_id,
                    filename="resume.txt",
                    url=None,
                    type="resume",
                    created_at=now,
                )
                session.add(resume_attachment)

            # Create attachment for resume_url if provided
            if resume_url:
                # Extract filename from URL or use default
                url_filename = resume_url.split("/")[-1].split("?")[0] or "resume"
                resume_url_attachment = CandidateAttachment(
                    candidate_id=candidate_id,
                    filename=url_filename,
                    url=resume_url,
                    type="resume",
                    created_at=now,
                )
                session.add(resume_url_attachment)

            if cover_letter_text:
                cover_letter_attachment = CandidateAttachment(
                    candidate_id=candidate_id,
                    filename="cover_letter.txt",
                    url=None,
                    type="cover_letter",
                    created_at=now,
                )
                session.add(cover_letter_attachment)

            # Create activity record
            activity_body = f"{first_name} {last_name} applied to {job.name} via Job Board"
            if resume_text:
                activity_body += f"\n\nResume:\n{resume_text}"
            if cover_letter_text:
                activity_body += f"\n\nCover Letter:\n{cover_letter_text}"

            # Include tracking metadata if provided
            metadata_parts = []
            if latitude or longitude:
                coords = (
                    f"{latitude}, {longitude}" if latitude and longitude else latitude or longitude
                )
                metadata_parts.append(f"Location coordinates: {coords}")
            if mapped_url_token:
                metadata_parts.append(f"Source attribution (gh_src): {mapped_url_token}")

            if metadata_parts:
                activity_body += "\n\nMetadata:\n" + "\n".join(metadata_parts)

            activity = Activity(
                candidate_id=candidate_id,
                application_id=application.id,
                subject="Application submitted via Job Board",
                body=activity_body,
                created_at=now,
            )
            session.add(activity)

            await session.commit()

            return {
                "success": True,
                "status": "Application submitted",
                "application_id": application.id,
                "candidate_id": candidate_id,
            }

    async def add_candidate_tag(
        self,
        candidate_id: int,
        tag_name: str,
        *,
        persona: str | None = None,
        user_id: int | None = None,
    ) -> dict:
        """Add a tag to a candidate.

        Creates the tag if it doesn't exist, then links it to the candidate.
        Idempotent: if the tag is already on the candidate, does nothing.

        Args:
            candidate_id: ID of the candidate to add the tag to.
            tag_name: Name of the tag to add.
            persona: Persona performing the action (for activity logging).
            user_id: User ID performing the action (for activity logging).

        Returns:
            Dict with tag_id and tag_name.

        Raises:
            CandidateNotFoundError: If the candidate doesn't exist.
            ValueError: If the tag name is empty or whitespace-only.
        """
        from datetime import UTC, datetime

        # Trim whitespace and validate
        tag_name = tag_name.strip()
        if not tag_name:
            raise ValueError("Tag name cannot be empty or blank")

        async with get_session() as session:
            # Validate candidate exists
            candidate = await session.get(Candidate, candidate_id)
            if candidate is None:
                raise CandidateNotFoundError(f"Candidate with id {candidate_id} does not exist")

            # Get or create the tag (handle race condition)
            tag_query = select(Tag).where(Tag.name == tag_name)
            result = await session.execute(tag_query)
            tag = result.scalar_one_or_none()

            if tag is None:
                try:
                    tag = Tag(name=tag_name)
                    session.add(tag)
                    await session.flush()
                except IntegrityError:
                    # Race condition: another request created the tag first
                    await session.rollback()
                    result = await session.execute(tag_query)
                    tag = result.scalar_one()

            # Check if candidate already has this tag
            existing_query = select(CandidateTag).where(
                CandidateTag.candidate_id == candidate_id,
                CandidateTag.tag_id == tag.id,
            )
            existing_result = await session.execute(existing_query)
            existing = existing_result.scalar_one_or_none()

            if existing is None:
                # Create the association
                candidate_tag = CandidateTag(candidate_id=candidate_id, tag_id=tag.id)
                session.add(candidate_tag)

                # Update candidate's last_activity timestamp only when tag is added
                now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
                candidate.last_activity = now
                candidate.updated_at = now

                # Log activity only when tag is actually added
                await log_tag_added(
                    session=session,
                    candidate_id=candidate_id,
                    tag_name=tag_name,
                    persona=persona,
                    user_id=user_id,
                )

                try:
                    await session.commit()
                except IntegrityError:
                    # Could be race condition (duplicate tag) or foreign key violation
                    # (candidate/tag deleted). Re-validate to determine which.
                    await session.rollback()

                    # Re-check if candidate still exists
                    candidate_check = await session.get(Candidate, candidate_id)
                    if candidate_check is None:
                        raise CandidateNotFoundError(
                            f"Candidate with id {candidate_id} was deleted during operation"
                        )

                    # Re-check if tag still exists
                    tag_recheck = await session.execute(tag_query)
                    tag = tag_recheck.scalar_one_or_none()
                    if tag is None:
                        raise ValueError(f"Tag '{tag_name}' was deleted during operation")

                    # Verify the tag association now exists (race condition case)
                    verify_query = select(CandidateTag).where(
                        CandidateTag.candidate_id == candidate_id,
                        CandidateTag.tag_id == tag.id,
                    )
                    verify_result = await session.execute(verify_query)
                    if verify_result.scalar_one_or_none() is None:
                        raise ValueError(
                            f"Failed to add tag '{tag_name}' to candidate {candidate_id}"
                        )

            return {"tag_id": tag.id, "tag_name": tag.name}

    # -------------------------------------------------------------------------
    # Lookup Lists (departments, offices, sources, rejection_reasons)
    # -------------------------------------------------------------------------

    async def list_departments(self) -> list[DepartmentOutput]:
        """Return all departments."""
        async with get_session() as session:
            query = select(Department).order_by(Department.name)
            result = await session.execute(query)
            departments = result.scalars().all()

            # Build child_ids mapping
            child_ids_map: dict[int, list[int]] = {}
            for dept in departments:
                if dept.parent_id is not None:
                    child_ids_map.setdefault(dept.parent_id, []).append(dept.id)

            return [
                DepartmentOutput(
                    id=dept.id,
                    name=dept.name,
                    parent_id=dept.parent_id,
                    child_ids=child_ids_map.get(dept.id, []),
                    external_id=dept.external_id,
                )
                for dept in departments
            ]

    async def list_offices(self) -> list[OfficeOutput]:
        """Return all offices."""
        async with get_session() as session:
            query = select(Office).order_by(Office.name)
            result = await session.execute(query)
            offices = result.scalars().all()

            # Build child_ids mapping
            child_ids_map: dict[int, list[int]] = {}
            for office in offices:
                if office.parent_id is not None:
                    child_ids_map.setdefault(office.parent_id, []).append(office.id)

            return [
                OfficeOutput(
                    id=office.id,
                    name=office.name,
                    location=OfficeLocationOutput(name=office.location_name),
                    primary_contact_user_id=office.primary_contact_user_id,
                    parent_id=office.parent_id,
                    child_ids=child_ids_map.get(office.id, []),
                    external_id=office.external_id,
                )
                for office in offices
            ]

    async def list_sources(self) -> list[dict[str, Any]]:
        """Return all candidate sources."""
        async with get_session() as session:
            query = select(Source).order_by(Source.name)
            result = await session.execute(query)
            sources = result.scalars().all()
            return [
                {
                    "id": source.id,
                    "name": source.name,
                    "type_id": source.type_id,
                }
                for source in sources
            ]

    async def list_rejection_reasons(self) -> list[dict[str, Any]]:
        """Return all rejection reasons."""
        async with get_session() as session:
            query = select(RejectionReason).order_by(RejectionReason.name)
            result = await session.execute(query)
            reasons = result.scalars().all()
            return [
                {
                    "id": reason.id,
                    "name": reason.name,
                    "type_id": reason.type_id,
                    "type_name": reason.type_name,
                }
                for reason in reasons
            ]
