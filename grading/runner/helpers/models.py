from enum import StrEnum


# @apg_helper_ids:start
class HelperIds(StrEnum):
    TEMPLATE = "template"
    SNAPSHOT_DIFF = "snapshot_diff"
    FINAL_ANSWER = "final_answer"
    SNAPSHOT_DBS = "snapshot_dbs"
    QUICKBOOKS_STATE = "quickbooks_state"
    TAXJAR_STATE = "taxjar_state"
    XERO_STATE = "xero_state"
    TABLEAU_STATE = "tableau_state"
    LOOKER_STATE = "looker_state"
    FILESYSTEM_SETUP = "filesystem_setup"
    ARTIFACT_STATE = "artifact_state"
    PLAYWRIGHT_TRACE_PARSER = "playwright_trace_parser"
    BROWSER_STATE = "browser_state"
    IF_JUDGE_RESULT = "if_judge_result"
    IF_SYSTEM_STEER_JUDGE_RESULT = "if_system_steer_judge_result"
    DB_DIFF = "db_diff"


# @apg_helper_ids:end
