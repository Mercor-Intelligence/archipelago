"""add v2 case management schema

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2025-12-20 10:30:00.000000

V2 Pre-Onboarding Coordination Schema:
- cases: Pre-onboarding case records
- milestones: Case milestone tracking
- tasks: Manual task records
- audit_entries: Append-only audit trail
- exceptions: Exception requests and approvals
- policy_references: Policy artifacts
- payroll_cutoffs: Payroll cutoff rules
- case_policy_links: Case-to-policy associations
- hcm_worker_state: Derived HCM state
- hcm_write_log: Immutable write log

See BUILD_PLAN_v2.md Section 4 for complete schema specification.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema - create V2 case management tables."""
    # Defer foreign key checks for SQLite to handle dependencies
    connection = op.get_bind()
    if "sqlite" in str(connection.engine.url):
        connection.execute(sa.text("PRAGMA defer_foreign_keys=ON"))

    # =========================================================================
    # 1. POLICY REFERENCE ENTITIES (no FK dependencies)
    # =========================================================================

    # policy_references - Policy artifacts for country/role-specific constraints
    op.create_table(
        "policy_references",
        sa.Column("policy_id", sa.String(), nullable=False),
        sa.Column("country", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=True),
        sa.Column("employment_type", sa.String(), nullable=True),
        sa.Column("policy_type", sa.String(), nullable=False),
        sa.Column("lead_time_days", sa.Integer(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),  # JSON object
        sa.Column("effective_date", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "policy_type IN ('prerequisites', 'lead_times', 'payroll_cutoffs', 'constraints')",
            name="check_policy_type",
        ),
        sa.PrimaryKeyConstraint("policy_id"),
    )

    # payroll_cutoffs - Payroll cutoff rules by country
    op.create_table(
        "payroll_cutoffs",
        sa.Column("cutoff_id", sa.String(), nullable=False),
        sa.Column("country", sa.String(), nullable=False),
        sa.Column("cutoff_day_of_month", sa.Integer(), nullable=False),
        sa.Column("processing_days", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("effective_date", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "cutoff_day_of_month >= 1 AND cutoff_day_of_month <= 31",
            name="check_cutoff_day_range",
        ),
        sa.PrimaryKeyConstraint("cutoff_id"),
    )

    # =========================================================================
    # 2. CANONICAL CASE ENTITIES
    # =========================================================================

    # cases - Pre-onboarding case records
    op.create_table(
        "cases",
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("candidate_id", sa.String(), nullable=False),
        sa.Column("requisition_id", sa.String(), nullable=True),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("country", sa.String(), nullable=False),
        sa.Column("employment_type", sa.String(), nullable=False, server_default="full_time"),
        sa.Column("owner_persona", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
        sa.Column("proposed_start_date", sa.String(), nullable=True),
        sa.Column("confirmed_start_date", sa.String(), nullable=True),
        sa.Column("due_date", sa.String(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "employment_type IN ('full_time', 'part_time', 'contractor')",
            name="check_case_employment_type",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'in_progress', 'pending_approval', 'resolved', 'closed')",
            name="check_case_status",
        ),
        sa.PrimaryKeyConstraint("case_id"),
    )

    # milestones - Case milestone tracking
    op.create_table(
        "milestones",
        sa.Column("milestone_id", sa.String(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("milestone_type", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("evidence_link", sa.String(), nullable=True),
        sa.Column("completion_date", sa.String(), nullable=True),
        sa.Column("completed_by", sa.String(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "milestone_type IN ('screening', 'work_authorization', 'documents', 'approvals')",
            name="check_milestone_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'in_progress', 'completed', 'waived', 'blocked')",
            name="check_milestone_status",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"]),
        sa.PrimaryKeyConstraint("milestone_id"),
        sa.UniqueConstraint("case_id", "milestone_type", name="uq_case_milestone_type"),
    )

    # tasks - Manual task records
    op.create_table(
        "tasks",
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("milestone_id", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("owner_persona", sa.String(), nullable=False),
        sa.Column("due_date", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'in_progress', 'completed', 'cancelled')",
            name="check_task_status",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"]),
        sa.ForeignKeyConstraint(["milestone_id"], ["milestones.milestone_id"]),
        sa.PrimaryKeyConstraint("task_id"),
    )

    # audit_entries - Append-only audit trail
    op.create_table(
        "audit_entries",
        sa.Column("entry_id", sa.String(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("action_type", sa.String(), nullable=False),
        sa.Column("actor_persona", sa.String(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("policy_refs", sa.Text(), nullable=True),  # JSON array of policy IDs
        sa.Column("evidence_links", sa.Text(), nullable=True),  # JSON array of evidence URLs
        sa.Column("details", sa.Text(), nullable=True),  # JSON object with action-specific details
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"]),
        sa.PrimaryKeyConstraint("entry_id"),
    )

    # exceptions - Exception requests and approvals
    op.create_table(
        "exceptions",
        sa.Column("exception_id", sa.String(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("milestone_type", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("affected_policy_refs", sa.Text(), nullable=True),  # JSON array
        sa.Column("requested_by", sa.String(), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("approval_status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("approval_notes", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "approval_status IN ('pending', 'approved', 'denied')",
            name="check_exception_approval_status",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"]),
        sa.PrimaryKeyConstraint("exception_id"),
    )

    # =========================================================================
    # 3. ASSOCIATION TABLES
    # =========================================================================

    # case_policy_links - Case-to-policy associations
    op.create_table(
        "case_policy_links",
        sa.Column("link_id", sa.String(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("policy_id", sa.String(), nullable=False),
        sa.Column("attached_at", sa.DateTime(), nullable=False),
        sa.Column("attached_by", sa.String(), nullable=False),
        sa.Column("decision_context", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"]),
        sa.ForeignKeyConstraint(["policy_id"], ["policy_references.policy_id"]),
        sa.PrimaryKeyConstraint("link_id"),
    )

    # =========================================================================
    # 4. HCM STATE ENTITIES (Derived/Gated)
    # =========================================================================

    # hcm_worker_state - Derived HCM state
    op.create_table(
        "hcm_worker_state",
        sa.Column("worker_id", sa.String(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("onboarding_status", sa.String(), nullable=True),
        sa.Column("onboarding_readiness", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("proposed_start_date", sa.String(), nullable=True),
        sa.Column("confirmed_start_date", sa.String(), nullable=True),
        sa.Column("hire_finalized", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("effective_date", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "onboarding_status IN ('not_started', 'in_progress', 'ready', 'finalized') "
            "OR onboarding_status IS NULL",
            name="check_onboarding_status",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"]),
        sa.PrimaryKeyConstraint("worker_id"),
        sa.UniqueConstraint("case_id", name="uq_hcm_worker_state_case"),
    )

    # hcm_write_log - Immutable write log
    op.create_table(
        "hcm_write_log",
        sa.Column("log_id", sa.String(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("worker_id", sa.String(), nullable=False),
        sa.Column("write_type", sa.String(), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),  # JSON
        sa.Column("new_value", sa.Text(), nullable=False),  # JSON
        sa.Column("actor_persona", sa.String(), nullable=False),
        sa.Column("policy_refs", sa.Text(), nullable=False),  # JSON array
        sa.Column("milestone_evidence", sa.Text(), nullable=False),  # JSON array
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "write_type IN ('confirm_start_date', 'update_readiness', 'finalize_hire')",
            name="check_write_type",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"]),
        sa.PrimaryKeyConstraint("log_id"),
    )

    # =========================================================================
    # 5. INDEXES FOR PERFORMANCE
    # =========================================================================

    # Cases indexes
    op.create_index("idx_cases_status", "cases", ["status"])
    op.create_index("idx_cases_country", "cases", ["country"])
    op.create_index("idx_cases_owner", "cases", ["owner_persona"])
    op.create_index("idx_cases_due_date", "cases", ["due_date"])

    # Milestones indexes
    op.create_index("idx_milestones_case", "milestones", ["case_id"])
    op.create_index("idx_milestones_status", "milestones", ["status"])

    # Tasks indexes
    op.create_index("idx_tasks_case", "tasks", ["case_id"])
    op.create_index("idx_tasks_status", "tasks", ["status"])

    # Audit entries indexes
    op.create_index("idx_audit_case", "audit_entries", ["case_id"])
    op.create_index("idx_audit_timestamp", "audit_entries", ["timestamp"])

    # Exceptions indexes
    op.create_index("idx_exceptions_case", "exceptions", ["case_id"])
    op.create_index("idx_exceptions_status", "exceptions", ["approval_status"])

    # Policy references indexes
    op.create_index("idx_policies_country", "policy_references", ["country"])
    op.create_index("idx_policies_type", "policy_references", ["policy_type"])

    # Payroll cutoffs indexes
    op.create_index("idx_payroll_cutoffs_country", "payroll_cutoffs", ["country"])

    # HCM state indexes
    op.create_index("idx_hcm_state_case", "hcm_worker_state", ["case_id"])
    op.create_index("idx_hcm_log_case", "hcm_write_log", ["case_id"])


def downgrade() -> None:
    """Downgrade schema - drop V2 case management tables."""
    # Drop indexes first (table_name required for MySQL compatibility)
    op.drop_index("idx_hcm_log_case", table_name="hcm_write_log")
    op.drop_index("idx_hcm_state_case", table_name="hcm_worker_state")
    op.drop_index("idx_payroll_cutoffs_country", table_name="payroll_cutoffs")
    op.drop_index("idx_policies_type", table_name="policy_references")
    op.drop_index("idx_policies_country", table_name="policy_references")
    op.drop_index("idx_exceptions_status", table_name="exceptions")
    op.drop_index("idx_exceptions_case", table_name="exceptions")
    op.drop_index("idx_audit_timestamp", table_name="audit_entries")
    op.drop_index("idx_audit_case", table_name="audit_entries")
    op.drop_index("idx_tasks_status", table_name="tasks")
    op.drop_index("idx_tasks_case", table_name="tasks")
    op.drop_index("idx_milestones_status", table_name="milestones")
    op.drop_index("idx_milestones_case", table_name="milestones")
    op.drop_index("idx_cases_due_date", table_name="cases")
    op.drop_index("idx_cases_owner", table_name="cases")
    op.drop_index("idx_cases_country", table_name="cases")
    op.drop_index("idx_cases_status", table_name="cases")

    # Drop tables in reverse dependency order
    op.drop_table("hcm_write_log")
    op.drop_table("hcm_worker_state")
    op.drop_table("case_policy_links")
    op.drop_table("exceptions")
    op.drop_table("audit_entries")
    op.drop_table("tasks")
    op.drop_table("milestones")
    op.drop_table("cases")
    op.drop_table("payroll_cutoffs")
    op.drop_table("policy_references")
