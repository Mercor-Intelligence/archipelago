"""SQLAlchemy ORM models for Greenhouse MCP Server.

Models are organized by domain:
- base: Base class and common utilities
- users: Users, departments, offices
- jobs: Jobs, stages, hiring team, openings
- candidates: Candidates and related contact info
- applications: Applications, answers, rejection reasons
- scorecards: Interview feedback/scorecards
- activity: Notes, emails, activities
- jobboard: Job posts, prospect pools
- sources: Candidate sources
"""

from db.models.activity import Activity, Email, Note
from db.models.applications import Application, ApplicationAnswer, RejectionReason
from db.models.base import Base, TimestampMixin, utc_now
from db.models.candidates import (
    Candidate,
    CandidateAddress,
    CandidateAttachment,
    CandidateEducation,
    CandidateEmailAddress,
    CandidateEmployment,
    CandidatePhoneNumber,
    CandidateSocialMediaAddress,
    CandidateTag,
    CandidateWebsiteAddress,
    Tag,
)
from db.models.jobboard import (
    Degree,
    Discipline,
    JobPost,
    JobPostQuestion,
    JobPostQuestionOption,
    ProspectPool,
    ProspectPoolStage,
    School,
)
from db.models.jobs import (
    HiringTeam,
    InterviewKitQuestion,
    InterviewStep,
    InterviewStepDefaultInterviewer,
    Job,
    JobDepartment,
    JobOffice,
    JobOpening,
    JobStage,
)
from db.models.scorecards import Scorecard, ScorecardAttribute, ScorecardQuestion
from db.models.sources import Source, SourceType
from db.models.users import (
    Department,
    Office,
    User,
    UserDepartment,
    UserEmail,
    UserOffice,
)

__all__ = [
    # Base
    "Base",
    "TimestampMixin",
    "utc_now",
    # Users
    "User",
    "UserEmail",
    "Department",
    "Office",
    "UserDepartment",
    "UserOffice",
    # Jobs
    "Job",
    "JobDepartment",
    "JobOffice",
    "HiringTeam",
    "JobStage",
    "InterviewStep",
    "InterviewKitQuestion",
    "InterviewStepDefaultInterviewer",
    "JobOpening",
    # Candidates
    "Candidate",
    "CandidatePhoneNumber",
    "CandidateEmailAddress",
    "CandidateAddress",
    "CandidateWebsiteAddress",
    "CandidateSocialMediaAddress",
    "CandidateEducation",
    "CandidateEmployment",
    "CandidateAttachment",
    "Tag",
    "CandidateTag",
    # Applications
    "Application",
    "ApplicationAnswer",
    "RejectionReason",
    # Scorecards
    "Scorecard",
    "ScorecardAttribute",
    "ScorecardQuestion",
    # Activity
    "Note",
    "Email",
    "Activity",
    # Job Board
    "JobPost",
    "JobPostQuestion",
    "JobPostQuestionOption",
    "ProspectPool",
    "ProspectPoolStage",
    "Degree",
    "Discipline",
    "School",
    # Sources
    "SourceType",
    "Source",
]
