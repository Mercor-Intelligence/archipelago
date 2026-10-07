from runner.evals.models import EvalImplInput
from runner.models import (
    VerifierResult,
)


async def template_eval_run(
    input: EvalImplInput,
) -> VerifierResult:
    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=1.0,
        verifier_result_values={},
    )
