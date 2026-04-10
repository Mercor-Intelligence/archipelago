"""Pydantic schemas for Greenhouse MCP Server.

Defines input validation schemas that match Greenhouse Harvest API specifications.
Schemas are organized by domain:

- common: Shared models (PhoneNumber, EmailAddress, etc.)
- candidates: Candidate tool input schemas
- applications: Application tool input schemas
- jobs: Job tool input schemas
- scorecards: Feedback/scorecard tool input schemas
- activity: Activity feed tool input schemas
- users: User tool input schemas
- jobboard: Job Board API tool input schemas
- admin: Administrative tools (reset_state)
"""

# Common/shared models
# Activity models
from schemas.activity import (
    ActivityEmailOutput,
    ActivityFeedOutput,
    ActivityItemOutput,
    ActivityNoteOutput,
    ActivityUserOutput,
    GetActivityFeedInput,
)
from schemas.admin import (
    GreenhouseResetStateInput,
    GreenhouseResetStateResponse,
)

# Application models
from schemas.applications import (
    AdvanceApplicationInput,
    AnswerInput,
    ApplicationCreditedToOutput,
    ApplicationJobOutput,
    ApplicationOutput,
    ApplicationRejectionReasonOutput,
    ApplicationSourceOutput,
    ApplicationStageOutput,
    AttachmentInput,
    CreateApplicationInput,
    GetApplicationInput,
    HireApplicationInput,
    ListApplicationsInput,
    ListApplicationsOutput,
    MoveApplicationInput,
    ReferrerInput,
    RejectApplicationInput,
    RejectionReasonTypeOutput,
)

# Candidate models
from schemas.candidates import (
    AddCandidateNoteInput,
    AddCandidateTagInput,
    AddCandidateTagOutput,
    CandidateAddressOutput,
    CandidateApplicationOutput,
    CandidateCurrentStageOutput,
    CandidateEducationOutput,
    CandidateEmailAddressOutput,
    CandidateEmploymentOutput,
    CandidateJobOutput,
    CandidateNoteOutput,
    CandidateNoteUserOutput,
    CandidateOutput,
    CandidatePhoneNumberOutput,
    CandidateSearchResultOutput,
    CandidateSocialMediaAddressOutput,
    CandidateUserOutput,
    CandidateWebsiteAddressOutput,
    CreateCandidateInput,
    GetCandidateInput,
    SearchCandidatesInput,
    SearchCandidatesOutput,
    TagOutput,
    UpdateCandidateInput,
)
from schemas.common import (
    Address,
    Education,
    EducationInput,
    EmailAddress,
    Employment,
    EmploymentInput,
    PaginationMeta,
    PaginationParams,
    PhoneNumber,
    ScorecardAttribute,
    ScorecardQuestion,
    SocialMediaAddress,
    TimestampFilters,
    WebsiteAddress,
)

# Job Board models
from schemas.jobboard import (
    CreateJobPostInput,
    CreateJobPostOutput,
    JobBoardApplyInput,
    JobBoardApplyOutput,
    ListJobBoardJobsInput,
    ListJobBoardJobsOutput,
)

# Job models
from schemas.jobs import (
    CreateJobInput,
    DefaultInterviewerUserOutput,
    GetJobInput,
    GetJobStagesInput,
    HiringTeamMemberOutput,
    HiringTeamOutput,
    InterviewKitOutput,
    InterviewKitQuestionOutput,
    InterviewOutput,
    JobDepartmentOutput,
    JobOfficeLocationOutput,
    JobOfficeOutput,
    JobOpeningOutput,
    JobOutput,
    JobStageOutput,
    ListJobsInput,
    ListJobsOutput,
    UpdateJobInput,
)

# Lookup models
from schemas.lookups import (
    ListDepartmentsOutput,
    ListOfficesOutput,
    ListRejectionReasonsOutput,
    ListSourcesOutput,
    RejectionReasonOutput,
    SourceOutput,
)

# Scorecard/feedback models
from schemas.scorecards import (
    ListFeedbackInput,
    ListFeedbackOutput,
    ScorecardAttributeOutput,
    ScorecardInterviewStepOutput,
    ScorecardOutput,
    ScorecardQuestionOutput,
    ScorecardRatingsOutput,
    ScorecardUserOutput,
    SubmitFeedbackInput,
)

# User models
from schemas.users import (
    CreateUserInput,
    DepartmentOutput,
    GetUserInput,
    ListUsersInput,
    ListUsersOutput,
    OfficeLocationOutput,
    OfficeOutput,
    UserOutput,
)

__all__ = [
    # Common
    "PhoneNumber",
    "EmailAddress",
    "Address",
    "WebsiteAddress",
    "SocialMediaAddress",
    "Education",
    "Employment",
    "ScorecardAttribute",
    "ScorecardQuestion",
    "PaginationMeta",
    "PaginationParams",
    "TimestampFilters",
    "EducationInput",
    "EmploymentInput",
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
    "CandidateNoteOutput",
    "CandidateNoteUserOutput",
    "CandidateUserOutput",
    "CandidateApplicationOutput",
    "CandidateCurrentStageOutput",
    "CandidateJobOutput",
    "CandidateEducationOutput",
    "CandidateEmploymentOutput",
    "CandidatePhoneNumberOutput",
    "CandidateEmailAddressOutput",
    "CandidateAddressOutput",
    "CandidateWebsiteAddressOutput",
    "CandidateSocialMediaAddressOutput",
    # Applications
    "AnswerInput",
    "ApplicationCreditedToOutput",
    "ApplicationJobOutput",
    "ApplicationOutput",
    "ApplicationRejectionReasonOutput",
    "ApplicationSourceOutput",
    "ApplicationStageOutput",
    "AttachmentInput",
    "GetApplicationInput",
    "ListApplicationsInput",
    "ListApplicationsOutput",
    "CreateApplicationInput",
    "AdvanceApplicationInput",
    "MoveApplicationInput",
    "ReferrerInput",
    "RejectApplicationInput",
    "RejectionReasonTypeOutput",
    "HireApplicationInput",
    # Jobs
    "ListJobsInput",
    "GetJobInput",
    "GetJobStagesInput",
    "CreateJobInput",
    "UpdateJobInput",
    "JobOutput",
    "JobDepartmentOutput",
    "JobOfficeOutput",
    "JobOfficeLocationOutput",
    "JobOpeningOutput",
    "HiringTeamOutput",
    "HiringTeamMemberOutput",
    "JobStageOutput",
    "InterviewOutput",
    "InterviewKitOutput",
    "InterviewKitQuestionOutput",
    "DefaultInterviewerUserOutput",
    # Scorecards
    "SubmitFeedbackInput",
    "ListFeedbackInput",
    "ListFeedbackOutput",
    "ScorecardOutput",
    "ScorecardUserOutput",
    "ScorecardInterviewStepOutput",
    "ScorecardAttributeOutput",
    "ScorecardQuestionOutput",
    "ScorecardRatingsOutput",
    # Activity
    "ActivityEmailOutput",
    "ActivityFeedOutput",
    "ActivityItemOutput",
    "ActivityNoteOutput",
    "ActivityUserOutput",
    "GetActivityFeedInput",
    # Users
    "CreateUserInput",
    "DepartmentOutput",
    "GetUserInput",
    "ListUsersInput",
    "ListUsersOutput",
    "OfficeLocationOutput",
    "OfficeOutput",
    "UserOutput",
    # Job Board
    "ListJobBoardJobsInput",
    "ListJobBoardJobsOutput",
    "JobBoardApplyInput",
    "JobBoardApplyOutput",
    "CreateJobPostInput",
    "CreateJobPostOutput",
    # Jobs (additional)
    "ListJobsOutput",
    # Admin
    "GreenhouseResetStateInput",
    "GreenhouseResetStateResponse",
    # Lookups
    "ListDepartmentsOutput",
    "ListOfficesOutput",
    "ListSourcesOutput",
    "ListRejectionReasonsOutput",
    "SourceOutput",
    "RejectionReasonOutput",
]
