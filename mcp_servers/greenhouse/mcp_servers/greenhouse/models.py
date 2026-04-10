"""Pydantic models for Greenhouse MCP Server.

This file re-exports key models from the schemas directory for UI generation.
The models define input/output schemas for Greenhouse Harvest API operations.

Model categories:
- Candidates: Search, create, update, and manage candidates
- Applications: Track and manage job applications through the hiring pipeline
- Jobs: Job postings, stages, and hiring team configuration
- Scorecards: Interview feedback and evaluation
- Users: Greenhouse users, departments, and offices
- Job Board: Public job listings and applications
"""

# Candidate models
# Activity models
from schemas.activity import (
    GetActivityFeedInput,
)

# Admin models
from schemas.admin import (
    GreenhouseResetStateInput,
    GreenhouseResetStateResponse,
)

# Application models
from schemas.applications import (
    AdvanceApplicationInput,
    ApplicationOutput,
    CreateApplicationInput,
    GetApplicationInput,
    HireApplicationInput,
    ListApplicationsInput,
    ListApplicationsOutput,
    MoveApplicationInput,
    RejectApplicationInput,
)
from schemas.candidates import (
    AddCandidateNoteInput,
    AddCandidateTagInput,
    AddCandidateTagOutput,
    CandidateOutput,
    CandidateSearchResultOutput,
    CreateCandidateInput,
    GetCandidateInput,
    SearchCandidatesInput,
    SearchCandidatesOutput,
    TagOutput,
    UpdateCandidateInput,
)

# Common models
from schemas.common import (
    PaginationMeta,
    PaginationParams,
    TimestampFilters,
)

# Job Board models
from schemas.jobboard import (
    JobBoardApplyInput,
    JobBoardApplyOutput,
    ListJobBoardJobsInput,
    ListJobBoardJobsOutput,
)

# Job models
from schemas.jobs import (
    CreateJobInput,
    GetJobInput,
    GetJobStagesInput,
    JobOutput,
    JobStageOutput,
    ListJobsInput,
    ListJobsOutput,
)

# Scorecard/feedback models
from schemas.scorecards import (
    ListFeedbackInput,
    ListFeedbackOutput,
    ScorecardOutput,
    SubmitFeedbackInput,
)

# User models
from schemas.users import (
    GetUserInput,
    ListUsersInput,
    ListUsersOutput,
    UserOutput,
)

__all__ = [
    # Candidates
    "GetCandidateInput",
    "SearchCandidatesInput",
    "SearchCandidatesOutput",
    "CreateCandidateInput",
    "UpdateCandidateInput",
    "AddCandidateNoteInput",
    "AddCandidateTagInput",
    "AddCandidateTagOutput",
    "TagOutput",
    "CandidateOutput",
    "CandidateSearchResultOutput",
    # Applications
    "GetApplicationInput",
    "ListApplicationsInput",
    "ListApplicationsOutput",
    "CreateApplicationInput",
    "AdvanceApplicationInput",
    "MoveApplicationInput",
    "RejectApplicationInput",
    "HireApplicationInput",
    "ApplicationOutput",
    # Jobs
    "ListJobsInput",
    "ListJobsOutput",
    "GetJobInput",
    "GetJobStagesInput",
    "CreateJobInput",
    "JobOutput",
    "JobStageOutput",
    # Scorecards
    "SubmitFeedbackInput",
    "ListFeedbackInput",
    "ListFeedbackOutput",
    "ScorecardOutput",
    # Users
    "GetUserInput",
    "ListUsersInput",
    "ListUsersOutput",
    "UserOutput",
    # Job Board
    "ListJobBoardJobsInput",
    "ListJobBoardJobsOutput",
    "JobBoardApplyInput",
    "JobBoardApplyOutput",
    # Activity
    "GetActivityFeedInput",
    # Common
    "PaginationMeta",
    "PaginationParams",
    "TimestampFilters",
    # Admin
    "GreenhouseResetStateInput",
    "GreenhouseResetStateResponse",
]
