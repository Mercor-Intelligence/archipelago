import io
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from runner.helpers.models import HelperIds
from runner.models import (
    AgentTrajectoryOutput,
    GradingSettings,
    Verifier,
    VerifierResult,
)


# @apg_eval_ids:start
class EvalType(StrEnum):
    LLM_JUDGE = "llm_judge"
    PROGRAMMATIC = "programmatic"


class EvalIds(StrEnum):
    TEMPLATE = "template"
    OUTPUT_LLM = "output_llm"
    OUTPUT_LLM_LITE = "output_llm_lite"
    OUTPUT_LLM_WEIGHTED = "output_llm_weighted"
    OUTPUT_LLM_MULTI_REPRESENTATION = "output_llm_multi_representation"
    GOLDEN_RESPONSE_MATCH = "golden_response_match"
    SQL_VALIDATOR = "sql_validator"
    CONTENT_LENGTH_CHECK = "content_length_check"
    FILE_DIFF_CHECK = "file_diff_check"
    # QuickBooks domain-specific verifiers
    QUICKBOOKS_REPORT_LINE_ITEM = "quickbooks_report_line_item"
    QUICKBOOKS_JOURNAL_ENTRY = "quickbooks_journal_entry"
    QUICKBOOKS_VARIANCE = "quickbooks_variance"
    QUICKBOOKS_FIELD_CHECK = "quickbooks_field_check"
    # TaxJar domain-specific verifiers
    TAXJAR_FIELD_CHECK = "taxjar_field_check"
    TAXJAR_CALCULATION = "taxjar_calculation"
    # Xero domain-specific verifiers
    XERO_FIELD_CHECK = "xero_field_check"
    XERO_CALCULATION = "xero_calculation"
    # OpenEMR domain-specific verifiers
    OPENEMR_CLINICAL_VERIFICATION = "openemr_clinical_verification"
    OPENEMR_STATE_CHECK = "openemr_state_check"
    OPENEMR_FIELD_CHECK = "openemr_field_check"
    # Tableau domain-specific verifiers
    TABLEAU_FIELD_CHECK = "tableau_field_check"
    # Looker domain-specific verifiers
    LOOKER_FIELD_CHECK = "looker_field_check"
    LOOKER_CONTENT_CHECK = "looker_content_check"
    # For Jupiter
    JUPITER_TEXT_BASED_CRITERION = "jupiter_text_based_criterion"
    JUPITER_EXCEL_CONTENT = "jupiter_excel_content"
    JUPITER_EXCEL_FORMATTING = "jupiter_excel_formatting"
    JUPITER_PPTX_CONTENT = "jupiter_pptx_content"
    JUPITER_PPTX_FORMATTING = "jupiter_pptx_formatting"
    # Deep Research verifier
    DEEP_RESEARCH = "deep_research"
    # Atlas Long Horizon verifier - simplified version of deep research
    ATLAS_LONG_HORIZON = "atlas_long_horizon"
    # Apex V1 Verifier - evaluates response against a single criterion
    APEX_V1_VERIFIER = "apex_v1_verifier"
    # Post-Training Tool Call Check verifier
    POSTTRAINING_TOOL_CALL_CHECK = "posttraining_tool_call_check"
    # Tool Call Check verifier - deterministic check for specific tool calls
    TOOL_CALL_CHECK = "tool_call_check"
    # Eightfold domain-specific verifiers
    EIGHTFOLD_FIELD_CHECK = "eightfold_field_check"
    # BambooHR domain-specific verifiers
    BAMBOOHR_FIELD_CHECK = "bamboohr_field_check"
    # ADP Payroll domain-specific verifiers
    ADP_FIELD_CHECK = "adp_field_check"
    ADP_CALCULATION = "adp_calculation"
    ADP_JOURNAL_ENTRY = "adp_journal_entry"
    # SAP Onboarding domain-specific verifiers
    SAP_ONBOARDING_FIELD_CHECK = "sap_onboarding_field_check"
    # SAP Recruiting domain-specific verifiers
    SAP_RECRUITING_FIELD_CHECK = "sap_recruiting_field_check"
    # Workday Help domain-specific verifiers
    WORKDAY_HELP_FIELD_CHECK = "workday_help_field_check"
    # Greenhouse ATS domain-specific verifiers
    GREENHOUSE_FIELD_CHECK = "greenhouse_field_check"
    # Workday HCM domain-specific verifiers
    WORKDAY_FIELD_CHECK = "workday_field_check"
    # ACE criterion verifier
    ACE_CRITERION_VERIFIER = "ace_criterion_verifier"
    # [CUSTOM VERIFIER] Response tool verifier - grades agent response and tool artifacts
    RESPONSE_TOOL_VERIFIER = "response_tool_verifier"
    # Page count verifier - validates file page/slide/sheet counts
    PAGE_COUNT_CHECK = "page_count_check"
    # Pattern match verifier - checks for word/phrase patterns using regex
    PATTERN_MATCH_CHECK = "pattern_match_check"
    # Balboa Verifier - criterion-based evaluation with output grading capabilities
    BALBOA_VERIFIER = "balboa_verifier"
    # Basic LLM judge - simplified version with just criteria
    BASIC_LLM_JUDGE = "basic_llm_judge"
    # Calendar domain-specific verifiers
    CALENDAR_FIELD_CHECK = "calendar_field_check"
    # Spreadsheet verifier - validates cell values and formatting in CSV/Excel files
    SPREADSHEET_VERIFIER = "spreadsheet_verifier"
    # Code execution verifier - executes Python code with unit tests
    CODE_EXECUTION = "code_execution"
    # KiCad EDA domain-specific verifiers
    KICAD_FIELD_CHECK = "kicad_field_check"
    KICAD_LVS_CHECK = "kicad_lvs_check"
    KICAD_ROUTING_COMPLETENESS = "kicad_routing_completeness"
    KICAD_DRC_JLCPCB = "kicad_drc_jlcpcb"
    KICAD_LAYOUT_QUALITY = "kicad_layout_quality"
    KICAD_SPICE_CHECK = "kicad_spice_check"
    # FreeCAD CAD domain-specific verifiers
    FREECAD_FIELD_CHECK = "freecad_field_check"
    # Jenkins CI domain-specific verifiers
    JENKINS_FIELD_CHECK = "jenkins_field_check"
    # Golden file match - binary exact match of golden response files against trajectory files
    GOLDEN_FILE_MATCH = "golden_file_match"
    # Playground snapshot verifier - compares playground snapshot state (stub)
    PLAYGROUND_SNAPSHOT_VERIFIER = "playground_snapshot_verifier"
    # Output LLM with browsing requirement check
    OUTPUT_LLM_BROWSING_CHECK = "output_llm_browsing_check"
    # Output LLM with system prompt included in grading context (IFRubricsJudge - multi-turn/complex IF)
    OUTPUT_LLM_WITH_SYSTEM = "output_llm_with_system"
    # Output LLM system steer judge (SystemSteerIFRubricsJudge - system prompt adherence)
    OUTPUT_LLM_SYSTEM_STEER = "output_llm_system_steer"
    # Service-defined verifiers (from arco.toml)
    SERVICE_VERIFIER = "service_verifier"
    # Artifact verifier - evaluates assertions against parsed artifact state
    ARTIFACT_VERIFIER = "artifact_verifier"
    # CLI verifier - validates command execution results
    CLI_VERIFIER = "cli_verifier"
    # Browser verifier - validates browser state (URL, forms)
    BROWSER_VERIFIER = "browser_verifier"
    # Trace verifier for Playwright trace analysis (milestones and behaviors)
    TRACE_VERIFIER = "trace_verifier"
    # DB diff LLM judge - evaluates database changes against criteria
    DB_DIFF_LLM = "db_diff_llm"
    # DB diff LLM tools judge - tool-augmented evaluation for large database diffs
    DB_DIFF_LLM_TOOLS = "db_diff_llm_tools"
    # Deliverable change log - structured rubric for documenting changes to output files
    DELIVERABLE_CHANGE_LOG = "deliverable_change_log"
    # MCQ exact match - extracts answer letter (A–J) and compares to correct answer
    MCQ_EXACT_MATCH = "mcq_exact_match"
    # HLE Judge - grades HLE questions using the OSS-faithful LLM judge prompt
    HLE_JUDGE = "hle_judge"
    # Workbench - output_llm_lite with numerical weight for weighted average scoring
    WORKBENCH = "workbench"


# @apg_eval_ids:end


class EvalConfig(BaseModel):
    """
    These are attached to the world, and they dictate how a certain eval should be run.
    For example, if you think of an eval similar to an orchestrator/llm, the eval config fields would be like the llm extra args.

    For SERVICE_VERIFIER type, eval_config_values may contain:
    - source_service_id: str - Service ID that provided these verifiers
    - source_service_version: int - Service SCD version (for update detection)
    - verifier_config_fields: list[TaskFieldSchema] - Backend-generated field schemas
    """

    eval_config_id: str
    eval_config_name: str
    eval_defn_id: EvalIds
    eval_config_values: dict[str, Any]


class EvalImplInput(BaseModel):
    initial_snapshot_bytes: io.BytesIO
    final_snapshot_bytes: io.BytesIO
    golden_snapshots: list[io.BytesIO] = Field(default_factory=list)
    trajectory: AgentTrajectoryOutput
    grading_settings: GradingSettings
    verifier: Verifier
    eval_config: EvalConfig
    dependencies: list[VerifierResult] | None
    helper_results: dict[HelperIds, Any] | None

    class Config:
        arbitrary_types_allowed = True
