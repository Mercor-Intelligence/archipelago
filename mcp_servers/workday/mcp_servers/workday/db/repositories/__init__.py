"""Database repositories for Workday HCM.

V1 Repositories (Worker Management):
- WorkerRepository: Worker CRUD and lifecycle management
- PositionRepository: Position CRUD and status management
- OrgRepository: Organization hierarchy management
- JobProfileRepository: Job profile CRUD
- ReportRepository: RaaS reporting queries

V2 Repositories (Pre-Onboarding Coordination):
- CaseRepository: Case, milestone, and task management
- AuditRepository: Append-only audit logging
- ExceptionRepository: Exception request workflow
- HCMRepository: Gated HCM state write-backs
- PolicyRepository: Policy lookup and management
"""

# V1 Repositories
# V2 Repositories
from db.repositories.audit_repository import AuditRepository
from db.repositories.case_repository import CaseRepository
from db.repositories.exception_repository import ExceptionRepository
from db.repositories.hcm_repository import HCMRepository
from db.repositories.job_profile_repository import JobProfileRepository
from db.repositories.org_repository import OrgRepository
from db.repositories.policy_repository import PolicyRepository
from db.repositories.position_repository import PositionRepository
from db.repositories.report_repository import ReportRepository
from db.repositories.worker_repository import WorkerRepository

__all__ = [
    # V1
    "WorkerRepository",
    "PositionRepository",
    "OrgRepository",
    "JobProfileRepository",
    "ReportRepository",
    # V2
    "CaseRepository",
    "AuditRepository",
    "ExceptionRepository",
    "HCMRepository",
    "PolicyRepository",
]
