from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from litellm.types.llms.openai import AllMessageValues
from litellm.types.utils import Message
from pydantic import BaseModel, Field

LitellmAnyMessage = AllMessageValues | Message


class AgentStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    ERROR = "error"


class GradingRunStatus(StrEnum):
    """Status of a grading run."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    ERROR = "error"


class EvaluationTarget(StrEnum):
    TARGET_AGENT = "target_agent"
    VIRTUAL_COWORKER_AGENT = "virtual_coworker_agent"


class AgentTrajectoryOutput(BaseModel):
    messages: list[LitellmAnyMessage]
    output: dict[str, Any] | None = None
    status: AgentStatus
    time_elapsed: float
    evaluation_target: EvaluationTarget = EvaluationTarget.TARGET_AGENT
    ta_trajectory_id: str | None = None
    vca_id: str | None = None
    # The grading run's task custom_fields, merged in from
    # GradingRunTrajectoryMetadata. Lets evals read task-level inputs
    # (e.g. browsecomp_judge_2 reads `expected_answer`) at grade time.
    task_custom_fields: dict[str, Any] = Field(default_factory=dict)


class GradingRunTrajectoryMetadata(BaseModel):
    evaluation_target: EvaluationTarget = EvaluationTarget.TARGET_AGENT
    ta_trajectory_id: str | None = None
    vca_id: str | None = None
    # The grading run's task custom_fields, merged onto the trajectory so evals
    # read task-level inputs (e.g. browsecomp_judge_2 reads `expected_answer`)
    # at grade time.
    task_custom_fields: dict[str, Any] = Field(default_factory=dict)
    # Set when the graded trajectory was launched as part of a batch.
    # Drives LLM Gateway workload selection in runner.utils.llm:
    #   set  -> "grading_batch" (P1)
    #   None -> "grading_single" (P0)
    trajectory_batch_id: str | None = None
    # Drives LLM Gateway X-Fairness-Key (interleaves grading traffic
    # across campaigns within the same priority bucket). None when the
    # server is older than the field — runner omits the header.
    campaign_id: str | None = None
    # Owning account for campaign_id, resolved server-side — CAS spend
    # attribution. None for older servers that omit the field.
    account_id: str | None = None
    # The graded trajectory's owning task — CAS `task_id` drill-down
    # attribution, same rationale as campaign_id/account_id above. None for
    # trajectories with no owning task, or an older server.
    task_id: str | None = None
    # The dispatched world_id — used with campaign_id to build the X-Work-Unit
    # `{campaign}~{world}` so per-unit attribution matches the priority the
    # server resolved. Falls back to the first verifier's world_id when None
    # (older server).
    world_id: str | None = None
    # Acting user's email, recorded on the graded trajectory at creation.
    # Threaded onto actor_email_ctx at worker entry so the grading run's
    # outbound external-platform API calls are attributed to the acting
    # user (per-user rate-limit buckets). None (system runs / older server)
    # keeps today's unattributed behavior.
    studio_actor_email: str | None = None

    # Acting user's id — companion to studio_actor_email; CAS spend
    # attribution needs the id, not just the email.
    studio_actor_user_id: str | None = None

    # Acting user's IP — companion to studio_actor_email; threaded to the runtime
    # so its outbound calls can be attributed to the real user.
    studio_actor_ip: str | None = None
    # S3 download backend selected server-side (GRADING_S3_TRANSFER_BACKEND
    # PostHog flag): "boto3" (default) or "s5cmd". The grading entrypoints set
    # the s3_backend_ctx ContextVar from this so the snapshot-download seam
    # routes the prebuilt-archive object through the chosen backend.
    s3_transfer_backend: str = "boto3"
    # Backend-resolved LLM Gateway routing decision for this grading run.
    # The server evaluates the `LLM_GATEWAY_ROUTING` PostHog flag at
    # config-build time (Modal workers can't call PostHog) and threads the
    # bool here. Read by `Settings.is_gateway_routed` via the
    # `gateway_routing_enabled_ctx` ContextVar; None is treated as False
    # (fail-closed) so older servers that omit the field keep traffic on
    # LiteLLM.
    gateway_routing_enabled: bool | None = None

    # Backend-resolved (SPARTA_TAIGA_IMPERSONATION, per campaign) gate for
    # whether outbound Sparta/Taiga calls attach `x-biome-impersonate-user`.
    # Threaded onto `impersonation_enabled_ctx` at worker entry. None → False
    # (service-account, pre-#13344). Gates ONLY the header — actor logging
    # unaffected.
    taiga_impersonation_enabled: bool | None = None

    # Backend-resolved X-Priority (0..5) for this grading run. Server runs
    # `resolve_priority` at config-build time — full precedence (PostHog
    # override → workload dict → default P3). Read by `Settings.resolve_priority`
    # via the `priority_ctx` ContextVar; None falls back to worker-default
    # P3 so older servers that omit the field grade at middle priority.
    resolved_priority: int | None = None

    # Per-grading-run arguments from Studio's `grading_runs.grading_run_args`
    # jsonb column. Threaded verbatim into `EvalImplInput.grading_run_args`.
    # The server controls this value. Public request bodies cannot set it.
    # Consumers parse the keys they understand at their own boundary. The
    # Sparta grading verifiers read `regrade_attempt_id` to ingest a Taiga
    # regrade instead of the run's original grade. None (default, or an older
    # server) grades exactly as before.
    grading_run_args: dict[str, Any] | None = None


class Verifier(BaseModel):
    """
    Verifier model for config-based verification system.
    """

    verifier_id: str
    verifier_version: int = 1
    world_id: str | None
    task_id: str | None

    eval_config_id: str
    verifier_values: dict[str, Any]
    verifier_custom_field_values: dict[str, Any] | None = None
    verifier_index: int
    evaluation_target: EvaluationTarget = EvaluationTarget.TARGET_AGENT
    vca_id: str | None = None

    verifier_dependencies: list[str] | None = None


class GradingSettings(BaseModel):
    llm_judge_model: str  # full model name (provider/model)
    llm_judge_extra_args: dict[str, Any] | None = None
    # Optional judge system-prompt override, resolved from the judge's pinned prompt version
    # (judge_prompt_id/version) by the grading dispatch. None = grader uses the hardcoded
    # GRADING_SYSTEM_PROMPT (backwards-compatible).
    llm_judge_system_prompt: str | None = None
    # Server-resolved per-token rate card for the judge model (input / output /
    # cached-read / cache-creation) for spend metering. None → local fallback.
    llm_judge_model_rates: dict[str, float] | None = None
    # True iff the graded trajectory's batch is guardrail-tracked — the single
    # switch for grading spend metering; False for older servers.
    budget_metering_enabled: bool = False


class VerifierResultStatus(StrEnum):
    """Status of a verifier result grading a criterion."""

    OK = "ok"
    ERROR = "error"


class VerifierResult(BaseModel):
    verifier_id: str
    verifier_version: int
    score: float
    verifier_result_values: dict[str, Any]
    status: VerifierResultStatus = VerifierResultStatus.OK
    message: str = ""


class ScoringMethodResult(BaseModel):
    """
    Result of scoring a single grading run.
    """

    final_score: float
    scoring_method_result_values: dict[str, Any]


class TaskFieldType(StrEnum):
    """Supported custom field types for task fields."""

    TEXT = "text"  # Single-line text input
    TEXTAREA = "textarea"  # Multi-line text input
    NUMBER = "number"  # Numeric input
    BOOLEAN = "boolean"  # Checkbox
    DATE = "date"  # Date picker
    DATETIME = "datetime"  # Date and time picker
    SELECT = "select"  # Single choice dropdown
    MULTISELECT = "multiselect"  # Multiple choice dropdown
    URL = "url"  # URL input with validation
    EMAIL = "email"  # Email input with validation
    ARTIFACT_MULTISELECT = (
        "artifact_multiselect"  # Multi-select file picker from snapshots
    )
    ARTIFACT_MULTISELECT_TRANSFORMED = (
        "artifact_multiselect_transformed"  # Multi-select with transformation options
    )
    LIKERT_SCALE = "likert_scale"  # Sliding integer scale with endpoint labels
    FILE = "file"  # File upload field, stores S3 keys
    SUBSCHEMA_LIST = "subschema_list"  # List of nested field groups
    JSON = "json"  # Arbitrary JSON data
    CODE = "code"  # Monospaced, syntax-highlighted code editor (Monaco)
    ORCHESTRATOR_SELECT = "orchestrator_select"  # Dropdown populated with the world's orchestrators (value = orchestrator_id)
    SERVICE_MULTISELECT = "service_multiselect"  # Multi-select populated with the world's services (value = list of service_id)


class FilterComparison(StrEnum):
    """Subset of the studio filter comparisons used by conditional rendering.

    Mirrors rl-studio ``models.db.worlds.FilterComparison`` (kept minimal — this
    is only used to type the frontend-only ``conditional_render_filter`` values
    that ride along in verifier config schemas)."""

    EQ = "eq"
    IS_NOT = "is_not"
    IS_ANY_OF = "is_any_of"
    IS_NONE_OF = "is_none_of"
    EMPTY = "empty"
    NOT_EMPTY = "not_empty"


class CustomViewFilter(BaseModel):
    """Frontend field-visibility predicate (mirror of the studio model).

    Grading never evaluates these; they are passthrough metadata for the task
    UI's conditional_render_filter on (sub)fields."""

    predicate_custom_field_id: str | None = None
    predicate_field_key: str | None = None
    comparison: FilterComparison | None = None
    value: Any = None
    children: list[CustomViewFilter] | None = None
    children_require_any: bool | None = None


CustomViewFilter.model_rebuild()


class TaskFieldOptionGroup(BaseModel):
    """A labelled subset of a field's options.

    Mirrors rl-studio ``models.db.worlds.TaskFieldOptionGroup``. Kept minimal,
    like ``CustomViewFilter`` above: the runner never renders a field, it only
    declares one, so this carries what the UI needs and nothing else.
    """

    label: str = Field(..., description="Section header shown above this group")
    options: list[str] = Field(..., description="Option values in this group")
    option_descriptions: dict[str, str] | None = Field(
        default=None,
        description="Optional per-option tooltip descriptions keyed by option value",
    )
    conditional_render_filter: list[CustomViewFilter] | None = Field(
        default=None,
        description=(
            "When set, this group is hidden unless all filters pass against "
            "sibling field values. Absent means always show."
        ),
    )
    allows_custom: bool = Field(
        default=False,
        description=(
            "When true, a select offers a Custom option alongside this group so "
            "an author can enter a value the group does not list."
        ),
    )


class TaskFieldSchema(BaseModel):
    """Schema definition for a single custom task field."""

    field_id: str = Field(
        ...,
        description="Immutable server-managed identifier for this field (e.g., 'field_<hex>').",
    )
    field_type: TaskFieldType = Field(
        ...,
        description="Type of field determines UI component and validation",
    )
    label: str = Field(
        ...,
        description="Human-readable label shown in UI",
    )
    required: bool = Field(
        default=False,
        description="Whether this field is required",
    )

    # Optional metadata
    description: str | None = Field(
        default=None,
        description="Help text shown to users",
    )
    placeholder: str | None = Field(
        default=None,
        description="Placeholder text for input fields",
    )
    default_value: Any | None = Field(
        default=None,
        description="Default value when creating new tasks",
    )

    # For select/multiselect fields
    options: list[str] | None = Field(
        default=None,
        description="Available options for select/multiselect fields",
    )
    option_groups: list[TaskFieldOptionGroup] | None = Field(
        default=None,
        description=(
            "Options split into labelled groups, each optionally shown only when "
            "its filters pass against sibling field values. A reader that ignores "
            "groups falls back to the flat `options` above."
        ),
    )

    # Validation rules
    min_length: int | None = Field(
        default=None,
        description="Minimum length for text fields",
    )
    max_length: int | None = Field(
        default=None,
        description="Maximum length for text fields",
    )
    min_value: float | None = Field(
        default=None,
        description="Minimum value for number fields",
    )
    max_value: float | None = Field(
        default=None,
        description="Maximum value for number fields",
    )
    pattern: str | None = Field(
        default=None,
        description="Regex pattern for validation (text fields)",
    )

    # UI hints
    display_width: Literal["full", "half", "third"] = Field(
        default="full",
        description="Width in form layout (full=100%, half=50%, third=33%)",
    )
    display_hidden: bool | None = Field(
        default=None, description="Whether or not this field is hidden in the UI"
    )

    # Likert scale display labels
    display_min_explanation: str | None = Field(
        default=None,
        description="Label shown at the min end of a likert scale (e.g., 'Strongly Disagree')",
    )
    display_max_explanation: str | None = Field(
        default=None,
        description="Label shown at the max end of a likert scale (e.g., 'Strongly Agree')",
    )

    # File field configuration
    max_files: int | None = Field(
        default=None,
        description="Maximum number of files allowed for file fields",
    )

    # A field the runner overrides regardless of what is configured. Both
    # default to None, so a field that says nothing behaves exactly as before.
    #
    # The pair exists because the alternative is a form that lies: the control
    # accepts a value, the runner logs that it ignored it, and the only person
    # who finds out reads the grading logs. Carrying the real value lets the UI
    # disable the control and say what will actually be used, and sourcing it
    # from the same constant the runner reads means the two cannot drift.
    frozen_value: Any | None = Field(
        default=None,
        description="Value the runner uses regardless of configuration.",
    )
    frozen_reason: str | None = Field(
        default=None,
        description="Why the field is frozen; shown wherever it is disabled.",
    )

    # Calibration configuration
    qualifies_no_change: bool | None = Field(
        default=None,
        description="If True, changes to this field do not invalidate calibration runs",
    )
    subschema: list[TaskFieldSchema] | None = Field(
        default=None,
        description="Schema for items when field_type is subschema_list.",
    )
    # Sibling-conditional visibility. Passthrough to the frontend only — grading
    # never evaluates these; each entry is a CustomViewFilter-shaped dict (e.g.
    # {"predicate_custom_field_id": "type", "comparison": "is_any_of",
    # "value": ["resolution"]}) that the task UI evaluates against the row's own
    # values to show/hide a subschema sub-field based on a sibling's value.
    conditional_render_filter: list[Any] | None = Field(
        default=None,
        description="Sibling-conditional visibility filters (frontend-only).",
    )
    conditional_render_require_any: bool | None = Field(
        default=None,
        description="If True, any matching filter shows the field; else all must match.",
    )


TaskFieldSchema.model_rebuild()
