import io
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict

from runner.helpers.models import HelperIds
from runner.models import AgentTrajectoryOutput, GradingSettings, Verifier

from .artifact_state import artifact_state_helper
from .browser_state import browser_state_helper
from .db_diff import db_diff_helper
from .filesystem_setup import filesystem_setup_helper
from .final_answer import final_answer_helper
from .if_judge_result import if_judge_result_helper
from .if_system_steer_judge_result import if_system_steer_judge_result_helper
from .looker_state import looker_state_helper
from .playwright_trace_parser import playwright_trace_parser_helper
from .quickbooks_state import quickbooks_state_helper
from .snapshot_dbs import snapshot_dbs_helper
from .snapshot_diff import snapshot_diff_helper
from .tableau_state import tableau_state_helper
from .taxjar_state import taxjar_state_helper
from .template import template_helper
from .xero_state import xero_state_helper

# Standard helper: (initial, final, trajectory) -> Any
HelperImpl = Callable[[io.BytesIO, io.BytesIO, AgentTrajectoryOutput], Awaitable[Any]]

# Context-aware helper: also receives verifiers, eval_defn_id_by_config_id, and grading_settings
HelperImplWithContext = Callable[
    [
        io.BytesIO,
        io.BytesIO,
        AgentTrajectoryOutput,
        list[Verifier],
        dict[str, str],
        GradingSettings,
    ],
    Awaitable[Any],
]


class HelperDefn(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    helper_id: HelperIds
    helper_impl: HelperImpl | None = None
    helper_impl_with_context: HelperImplWithContext | None = None


# @apg_helper_registry:start
HELPER_REGISTRY: dict[HelperIds, HelperDefn] = {
    HelperIds.TEMPLATE: HelperDefn(
        helper_id=HelperIds.TEMPLATE, helper_impl=template_helper
    ),
    HelperIds.SNAPSHOT_DIFF: HelperDefn(
        helper_id=HelperIds.SNAPSHOT_DIFF, helper_impl=snapshot_diff_helper
    ),
    HelperIds.FINAL_ANSWER: HelperDefn(
        helper_id=HelperIds.FINAL_ANSWER, helper_impl=final_answer_helper
    ),
    HelperIds.SNAPSHOT_DBS: HelperDefn(
        helper_id=HelperIds.SNAPSHOT_DBS, helper_impl=snapshot_dbs_helper
    ),
    HelperIds.QUICKBOOKS_STATE: HelperDefn(
        helper_id=HelperIds.QUICKBOOKS_STATE, helper_impl=quickbooks_state_helper
    ),
    HelperIds.TAXJAR_STATE: HelperDefn(
        helper_id=HelperIds.TAXJAR_STATE, helper_impl=taxjar_state_helper
    ),
    HelperIds.XERO_STATE: HelperDefn(
        helper_id=HelperIds.XERO_STATE, helper_impl=xero_state_helper
    ),
    HelperIds.TABLEAU_STATE: HelperDefn(
        helper_id=HelperIds.TABLEAU_STATE, helper_impl=tableau_state_helper
    ),
    HelperIds.LOOKER_STATE: HelperDefn(
        helper_id=HelperIds.LOOKER_STATE, helper_impl=looker_state_helper
    ),
    HelperIds.FILESYSTEM_SETUP: HelperDefn(
        helper_id=HelperIds.FILESYSTEM_SETUP, helper_impl=filesystem_setup_helper
    ),
    HelperIds.ARTIFACT_STATE: HelperDefn(
        helper_id=HelperIds.ARTIFACT_STATE, helper_impl=artifact_state_helper
    ),
    HelperIds.PLAYWRIGHT_TRACE_PARSER: HelperDefn(
        helper_id=HelperIds.PLAYWRIGHT_TRACE_PARSER,
        helper_impl=playwright_trace_parser_helper,
    ),
    HelperIds.BROWSER_STATE: HelperDefn(
        helper_id=HelperIds.BROWSER_STATE, helper_impl=browser_state_helper
    ),
    HelperIds.IF_JUDGE_RESULT: HelperDefn(
        helper_id=HelperIds.IF_JUDGE_RESULT,
        helper_impl_with_context=if_judge_result_helper,
    ),
    HelperIds.IF_SYSTEM_STEER_JUDGE_RESULT: HelperDefn(
        helper_id=HelperIds.IF_SYSTEM_STEER_JUDGE_RESULT,
        helper_impl_with_context=if_system_steer_judge_result_helper,
    ),
    HelperIds.DB_DIFF: HelperDefn(
        helper_id=HelperIds.DB_DIFF, helper_impl=db_diff_helper
    ),
}
# @apg_helper_registry:end
