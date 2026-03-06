import io
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from runner.helpers.models import HelperIds
from runner.models import (
    AgentTrajectoryOutput,
    GradingSettings,
    Verifier,
    VerifierResult,
)

class EvalType(StrEnum):
    LLM_JUDGE = "llm_judge"
    PROGRAMMATIC = "programmatic"

class EvalIds(StrEnum):
    OUTPUT_LLM = "output_llm"
    OUTPUT_LLM_LITE = "output_llm_lite"

class EvalConfig(BaseModel):
    """
    These are attached to the world, and they dictate how a certain eval should be run.
    For example, if you think of an eval similar to an orchestrator/llm, the eval config fields would be like the llm extra args.
    """

    eval_config_id: str
    eval_config_name: str
    eval_defn_id: EvalIds
    eval_config_values: dict[str, Any]

class EvalImplInput(BaseModel):
    initial_snapshot_bytes: io.BytesIO
    final_snapshot_bytes: io.BytesIO
    trajectory: AgentTrajectoryOutput
    grading_settings: GradingSettings
    verifier: Verifier
    eval_config: EvalConfig
    dependencies: list[VerifierResult] | None
    helper_results: dict[HelperIds, Any] | None

    class Config:
        arbitrary_types_allowed = True
