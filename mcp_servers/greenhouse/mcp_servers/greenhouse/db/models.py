"""Database models for sample data generation.

This file re-exports SQLAlchemy models from db/models/ for the UI generator
to create sample CSV data.

The UI generator scans this file to generate sample data for the Sample Data tab.
"""

# Re-export all models from the models package
from db.models import (
    # Activity
    Activity,
    # Applications
    Application,
    ApplicationAnswer,
    # Base class (needed for import_csv.py schema detection)
    Base,
    # Candidates
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
    # Job Board
    Degree,
    # Users
    Department,
    Discipline,
    Email,
    # Jobs
    HiringTeam,
    InterviewKitQuestion,
    InterviewStep,
    InterviewStepDefaultInterviewer,
    Job,
    JobDepartment,
    JobOffice,
    JobOpening,
    JobPost,
    JobPostQuestion,
    JobPostQuestionOption,
    JobStage,
    Note,
    Office,
    ProspectPool,
    ProspectPoolStage,
    RejectionReason,
    School,
    # Scorecards
    Scorecard,
    ScorecardAttribute,
    ScorecardQuestion,
    # Sources
    Source,
    SourceType,
    Tag,
    User,
    UserDepartment,
    UserEmail,
    UserOffice,
)

__all__ = [
    # Base class (needed for import_csv.py schema detection)
    "Base",
    # Activity
    "Activity",
    "Email",
    "Note",
    # Applications
    "Application",
    "ApplicationAnswer",
    "RejectionReason",
    # Candidates
    "Candidate",
    "CandidateAddress",
    "CandidateAttachment",
    "CandidateEducation",
    "CandidateEmailAddress",
    "CandidateEmployment",
    "CandidatePhoneNumber",
    "CandidateSocialMediaAddress",
    "CandidateTag",
    "CandidateWebsiteAddress",
    "Tag",
    # Job Board
    "Degree",
    "Discipline",
    "JobPost",
    "JobPostQuestion",
    "JobPostQuestionOption",
    "ProspectPool",
    "ProspectPoolStage",
    "School",
    # Jobs
    "HiringTeam",
    "InterviewKitQuestion",
    "InterviewStep",
    "InterviewStepDefaultInterviewer",
    "Job",
    "JobDepartment",
    "JobOffice",
    "JobOpening",
    "JobStage",
    # Scorecards
    "Scorecard",
    "ScorecardAttribute",
    "ScorecardQuestion",
    # Sources
    "Source",
    "SourceType",
    # Users
    "Department",
    "Office",
    "User",
    "UserDepartment",
    "UserEmail",
    "UserOffice",
]
