"""Schema-aware entity definitions for Workday CSV import.

Provides:
- ENTITY_SCHEMAS: required/optional columns and aliases per entity type
- normalize_header(): Re-exported from mcp_scripts.import_csv for convenience

Keys in ENTITY_SCHEMAS match SQLAlchemy __tablename__ values in db/models.py.
Auto-generated columns (id, created_at, updated_at) are never required.
"""

from typing import Any

from mcp_scripts.import_csv import normalize_header

ENTITY_SCHEMAS: dict[str, dict[str, Any]] = {
    # =========================================================================
    # CORE HCM TABLES
    # =========================================================================
    "supervisory_orgs": {
        "required": {
            "org_id",
            "org_name",
            "org_type",
        },
        "optional": {
            "parent_org_id",
            "manager_worker_id",
            "created_at",
            "updated_at",
        },
        "aliases": {
            "organization_id": "org_id",
            "organization_name": "org_name",
            "organization_type": "org_type",
            "name": "org_name",
            "type": "org_type",
            "parent_id": "parent_org_id",
            "parent": "parent_org_id",
            "manager_id": "manager_worker_id",
            "manager": "manager_worker_id",
        },
    },
    "job_profiles": {
        "required": {
            "job_profile_id",
            "title",
            "job_family",
        },
        "optional": {
            "job_level",
            "created_at",
        },
        "aliases": {
            "profile_id": "job_profile_id",
            "id": "job_profile_id",
            "name": "title",
            "job_title": "title",
            "family": "job_family",
            "level": "job_level",
        },
    },
    "cost_centers": {
        "required": {
            "cost_center_id",
            "cost_center_name",
            "org_id",
        },
        "optional": {
            "created_at",
        },
        "aliases": {
            "id": "cost_center_id",
            "name": "cost_center_name",
            "organization_id": "org_id",
        },
    },
    "locations": {
        "required": {
            "location_id",
            "location_name",
        },
        "optional": {
            "city",
            "country",
            "created_at",
        },
        "aliases": {
            "id": "location_id",
            "name": "location_name",
        },
    },
    "positions": {
        "required": {
            "position_id",
            "job_profile_id",
            "org_id",
            "fte",
            "status",
        },
        "optional": {
            "worker_id",
            "created_at",
            "updated_at",
        },
        "aliases": {
            "id": "position_id",
            "profile_id": "job_profile_id",
            "organization_id": "org_id",
            "full_time_equivalent": "fte",
            "position_status": "status",
        },
    },
    "workers": {
        "required": {
            "worker_id",
            "job_profile_id",
            "org_id",
            "cost_center_id",
            "employment_status",
            "fte",
            "hire_date",
            "effective_date",
        },
        "optional": {
            "location_id",
            "position_id",
            "termination_date",
            "created_at",
            "updated_at",
        },
        "aliases": {
            "id": "worker_id",
            "employee_id": "worker_id",
            "emp_id": "worker_id",
            "profile_id": "job_profile_id",
            "organization_id": "org_id",
            "status": "employment_status",
            "emp_status": "employment_status",
            "full_time_equivalent": "fte",
            "start_date": "hire_date",
            "term_date": "termination_date",
            "end_date": "termination_date",
        },
    },
    "movements": {
        "required": {
            "event_id",
            "worker_id",
            "event_type",
            "event_date",
        },
        "optional": {
            "from_org_id",
            "to_org_id",
            "from_cost_center_id",
            "to_cost_center_id",
            "from_job_profile_id",
            "to_job_profile_id",
            "from_position_id",
            "to_position_id",
            "created_at",
        },
        "aliases": {
            "id": "event_id",
            "movement_id": "event_id",
            "employee_id": "worker_id",
            "type": "event_type",
            "date": "event_date",
            "source_org_id": "from_org_id",
            "target_org_id": "to_org_id",
            "source_cost_center_id": "from_cost_center_id",
            "target_cost_center_id": "to_cost_center_id",
            "source_job_profile_id": "from_job_profile_id",
            "target_job_profile_id": "to_job_profile_id",
            "source_position_id": "from_position_id",
            "target_position_id": "to_position_id",
        },
    },
    # =========================================================================
    # PRE-ONBOARDING COORDINATION TABLES
    # =========================================================================
    "policy_references": {
        "required": {
            "policy_id",
            "country",
            "policy_type",
            "content",
            "effective_date",
            "version",
        },
        "optional": {
            "role",
            "employment_type",
            "lead_time_days",
            "created_at",
        },
        "aliases": {
            "id": "policy_id",
            "type": "policy_type",
            "lead_time": "lead_time_days",
        },
    },
    "payroll_cutoffs": {
        "required": {
            "cutoff_id",
            "country",
            "cutoff_day_of_month",
            "effective_date",
        },
        "optional": {
            "processing_days",
            "created_at",
        },
        "aliases": {
            "id": "cutoff_id",
            "cutoff_day": "cutoff_day_of_month",
            "day_of_month": "cutoff_day_of_month",
        },
    },
    "cases": {
        "required": {
            "case_id",
            "candidate_id",
            "role",
            "country",
            "owner_persona",
        },
        "optional": {
            "requisition_id",
            "employment_type",
            "status",
            "proposed_start_date",
            "confirmed_start_date",
            "due_date",
            "notes",
            "created_at",
            "updated_at",
        },
        "aliases": {
            "id": "case_id",
            "candidate": "candidate_id",
            "req_id": "requisition_id",
            "owner": "owner_persona",
            "persona": "owner_persona",
            "start_date": "proposed_start_date",
        },
    },
    "milestones": {
        "required": {
            "milestone_id",
            "case_id",
            "milestone_type",
        },
        "optional": {
            "status",
            "evidence_link",
            "completion_date",
            "completed_by",
            "notes",
            "created_at",
            "updated_at",
        },
        "aliases": {
            "id": "milestone_id",
            "type": "milestone_type",
            "evidence": "evidence_link",
        },
    },
    "tasks": {
        "required": {
            "task_id",
            "case_id",
            "title",
            "owner_persona",
        },
        "optional": {
            "milestone_id",
            "due_date",
            "status",
            "notes",
            "created_at",
            "updated_at",
        },
        "aliases": {
            "id": "task_id",
            "name": "title",
            "owner": "owner_persona",
            "persona": "owner_persona",
        },
    },
    "audit_entries": {
        "required": {
            "entry_id",
            "case_id",
            "action_type",
            "actor_persona",
        },
        "optional": {
            "rationale",
            "policy_refs",
            "evidence_links",
            "details",
            "timestamp",
        },
        "aliases": {
            "id": "entry_id",
            "audit_id": "entry_id",
            "action": "action_type",
            "type": "action_type",
            "actor": "actor_persona",
            "persona": "actor_persona",
        },
    },
    "exceptions": {
        "required": {
            "exception_id",
            "case_id",
            "milestone_type",
            "reason",
            "requested_by",
        },
        "optional": {
            "affected_policy_refs",
            "requested_at",
            "approval_status",
            "approved_by",
            "approval_notes",
            "approved_at",
        },
        "aliases": {
            "id": "exception_id",
            "type": "milestone_type",
            "requester": "requested_by",
            "status": "approval_status",
            "approver": "approved_by",
        },
    },
    "case_policy_links": {
        "required": {
            "link_id",
            "case_id",
            "policy_id",
            "attached_by",
        },
        "optional": {
            "attached_at",
            "decision_context",
        },
        "aliases": {
            "id": "link_id",
        },
    },
    "hcm_worker_state": {
        "required": {
            "worker_id",
            "case_id",
        },
        "optional": {
            "onboarding_status",
            "onboarding_readiness",
            "proposed_start_date",
            "confirmed_start_date",
            "hire_finalized",
            "effective_date",
            "created_at",
            "updated_at",
        },
        "aliases": {
            "status": "onboarding_status",
            "readiness": "onboarding_readiness",
            "start_date": "proposed_start_date",
            "finalized": "hire_finalized",
        },
    },
    "hcm_write_log": {
        "required": {
            "log_id",
            "case_id",
            "worker_id",
            "write_type",
            "new_value",
            "actor_persona",
            "policy_refs",
            "milestone_evidence",
            "rationale",
        },
        "optional": {
            "old_value",
            "timestamp",
        },
        "aliases": {
            "id": "log_id",
            "type": "write_type",
            "actor": "actor_persona",
            "persona": "actor_persona",
            "previous_value": "old_value",
            "evidence": "milestone_evidence",
        },
    },
    # =========================================================================
    # HELP MODULE TABLES
    # =========================================================================
    "help_cases": {
        "required": {
            "case_id",
            "case_type",
            "owner",
            "status",
            "candidate_identifier",
        },
        "optional": {
            "due_date",
            "created_at",
            "updated_at",
            "metadata",
        },
        "aliases": {
            "id": "case_id",
            "type": "case_type",
            "candidate_id": "candidate_identifier",
            "candidate": "candidate_identifier",
            "meta": "metadata",
        },
    },
    "help_timeline_events": {
        "required": {
            "event_id",
            "case_id",
            "event_type",
            "actor",
        },
        "optional": {
            "created_at",
            "notes",
            "metadata",
        },
        "aliases": {
            "id": "event_id",
            "type": "event_type",
            "meta": "metadata",
        },
    },
    "help_messages": {
        "required": {
            "message_id",
            "case_id",
            "direction",
            "sender",
            "body",
        },
        "optional": {
            "audience",
            "created_at",
            "metadata",
        },
        "aliases": {
            "id": "message_id",
            "content": "body",
            "text": "body",
            "message": "body",
            "meta": "metadata",
        },
    },
    "help_attachments": {
        "required": {
            "attachment_id",
            "case_id",
            "filename",
            "uploader",
        },
        "optional": {
            "mime_type",
            "source",
            "external_reference",
            "size_bytes",
            "uploaded_at",
            "metadata",
        },
        "aliases": {
            "id": "attachment_id",
            "file_name": "filename",
            "name": "filename",
            "content_type": "mime_type",
            "type": "mime_type",
            "size": "size_bytes",
            "meta": "metadata",
        },
    },
    "help_audit_log": {
        "required": {
            "log_id",
            "case_id",
            "entity_type",
            "entity_id",
            "action",
            "actor",
            "actor_persona",
        },
        "optional": {
            "created_at",
            "changes",
            "rationale",
            "metadata",
        },
        "aliases": {
            "id": "log_id",
            "audit_id": "log_id",
            "type": "entity_type",
            "persona": "actor_persona",
            "meta": "metadata",
        },
    },
}


__all__ = ["ENTITY_SCHEMAS", "normalize_header"]
