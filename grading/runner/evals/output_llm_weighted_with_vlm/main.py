from runner.evals.models import EvalImplInput
from runner.models import VerifierResult

from ..output_llm_multi_representation.main import multi_representation_eval


async def output_llm_weighted_with_vlm_eval(input: EvalImplInput) -> VerifierResult:
    vv = {**(input.verifier.verifier_values or {})}
    vv["enable_visual_grading"] = True
    vv.pop("artifacts_to_reference", None)
    new_verifier = input.verifier.model_copy(update={"verifier_values": vv})
    new_input = input.model_copy(update={"verifier": new_verifier})
    return await multi_representation_eval(new_input)
