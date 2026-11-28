"""Score-only verifier fields: the server sends them apart from what evals read."""

from runner.evals.models import EvalConfig
from runner.evals.registry import EVAL_REGISTRY
from runner.models import Verifier


def with_scoring_values(verifiers: list[Verifier]) -> list[Verifier]:
    """The verifiers as scoring reads them: `scoring_values` merged back into
    `verifier_values`. The same list when no verifier has any."""
    if not any(v.scoring_values for v in verifiers):
        return verifiers
    return [
        v.model_copy(
            update={
                "verifier_values": v.verifier_values | v.scoring_values,
                "scoring_values": {},
            }
        )
        if v.scoring_values
        else v
        for v in verifiers
    ]


def without_scoring_values(
    verifiers: list[Verifier], eval_configs: list[EvalConfig]
) -> list[Verifier]:
    """The verifiers as evals and helpers receive them: no `scoring_values`, and
    no declared score-only field left in `verifier_values` by a merged config."""
    fields_by_config = {
        ec.eval_config_id: frozenset(defn.score_only_verifier_fields)
        for ec in eval_configs
        if (defn := EVAL_REGISTRY.get(ec.eval_defn_id))
        and defn.score_only_verifier_fields
    }
    judged: list[Verifier] = []
    for v in verifiers:
        fields = fields_by_config.get(v.eval_config_id, frozenset())
        if v.scoring_values or fields & v.verifier_values.keys():
            v = v.model_copy(
                update={
                    "verifier_values": {
                        k: val
                        for k, val in v.verifier_values.items()
                        if k not in fields
                    },
                    "scoring_values": {},
                }
            )
        judged.append(v)
    return judged
