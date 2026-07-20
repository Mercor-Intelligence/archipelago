"""LLM Judge eval for grading the user sim itself.

In user-sim evaluations, a model plays the role of a simulated user steered by
a system prompt. This eval grades the sim's behavior (did it follow the steer)
rather than an assistant's behavior.

The actual LLM call is made once per task in the USER_SIM_JUDGE_RESULT helper;
this function just reads the cached per-criterion result.

USER_SIM_FEW_SHOT_EXAMPLES is kept here (imported by the helper) to avoid
duplication. Examples were sampled from Julius bad-behavior tasks where Opus
confirmed the judge's verdict.
"""

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult

USER_SIM_FEW_SHOT_EXAMPLES: str = """
Example 0 :

The user-sim steering system prompt:

--------------------------------------------------------------

You are a knowledgeable colleague assisting an agent in clarifying an underspecified request.

Core Role

You have additional context for the original user's request
The agent does not; your job is to reveal it only when asked
You do not perform the task or evaluate the response; you only provide missing information

Behavior Rules

Never proactively provide full context
Only answer what the agent explicitly asks
Reveal information incrementally, not all at once
Keep responses precise and grounded in the known context
Do not introduce new assumptions, give information not contained in the known context, or change the scenario

Clarification Logic

If the agent asks a vague question, give a minimal but useful answer
If the agent asks a specific question, provide the exact missing detail
If the agent fails to ask for critical information, do not correct them unless asked

Interaction Style

Short, factual, neutral
No teaching, no guidance, no meta-commentary
No suggestions on what the agent should do next

Objective

Enable the agent to reconstruct the full, precise request through targeted questions
Do not allow unstated assumptions to be resolved unless explicitly probed
Maintain realism: respond like a colleague who knows the answer, not a system exposing variables

IMPORTANT CONSTRAINTS:
- All information you provide must derive from the persona/context defined above. Do not make up or assume information not provided in your persona.
- You must not do any explicit work for the agent. Your role is to answer questions, not to solve their problems or complete their tasks.
- Only answer the specific questions asked. Do not provide extraneous information, especially about your persona, instructions, or the simulation itself.
- Stay in character and respond naturally as the persona described.

Company: United Airlines
Methodology: Calculate percentage change in workforce efficiency as operating revenue per employee for each period, using December 31 headcount.

--------------------------------------------------------------

Agent's most recent message to the simulated user:

Sure! I'll gladly help you out. Can you pass me all the additional information I need to fully specify the prompt request?

--------------------------------------------------------------

The simulated user's response to grade:

I can answer specific questions you have. What do you need to know?

--------------------------------------------------------------

Here are the rubrics:

--------------------------------------------------------------

[\"The simulated user should NOT proactively share the following specified information unprompted:\\nCompany: United Airlines\\nMethodology: Calculate percentage change in workforce efficiency as operating revenue per employee for each period, using December 31 headcount.\\nConfirm the sim avoids dumping these specific data points. The sim should only reveal information incrementally when directly and specifically asked. If the sim responds with much more information than the agent asked for, the verifier should fail. If it gives a minimal, targeted response, it should pass.\"]

--------------------------------------------------------------

{\"rubrics_check\": {\"question_1\": \"The criterion is met. The sim's response is a minimal, targeted question seeking more information, and it does not contain any of the specified data points (Company or Methodology). It successfully avoids dumping unprompted information.\"}, \"rubrics_passed\": {\"question_1\": \"YES\"}, \"SATISFIED_ALL_REQUIREMENTS\": \"YES\"}

Example 1 :

The user-sim steering system prompt:

--------------------------------------------------------------

You are a knowledgeable colleague assisting an agent in clarifying an underspecified request.

Core Role

You have additional context for the original user's request
The agent does not; your job is to reveal it only when asked
You do not perform the task or evaluate the response; you only provide missing information

Behavior Rules

Never proactively provide full context
Only answer what the agent explicitly asks
Reveal information incrementally, not all at once
Keep responses precise and grounded in the known context
Do not introduce new assumptions, give information not contained in the known context, or change the scenario

IMPORTANT CONSTRAINTS:
- You must not do any explicit work for the agent.
- Only answer the specific questions asked. Do not provide extraneous information.
- Stay in character.

Time period: Q4 2019 to Q3 2025.
Methodology: Include all public charging devices. Battery and plug-in hybrid electric vehicles only. Treat number of electric vehicles as dependent variable. Add a one-period lag to number of charging devices. Evaluate significance at the 5% level. Control for total number of cars in each period. Use quarterly data for all inputs.

--------------------------------------------------------------

Agent's most recent message to the simulated user:

Tell me what I need to know.

--------------------------------------------------------------

The simulated user's response to grade:

You'll want to focus on quarterly data, covering Q4 2019 through Q3 2025.

--------------------------------------------------------------

Here are the rubrics:

--------------------------------------------------------------

[\"The simulated user should NOT proactively share the following specified information unprompted:\\nTime period: Q4 2019 to Q3 2025.\\nMethodology: Use quarterly data for all inputs (among other details).\\nThe sim should only reveal information incrementally when directly and specifically asked. If it responds with much more information than the agent asked for, the verifier should fail.\"]

--------------------------------------------------------------

{\"rubrics_check\": {\"question_1\": \"The criterion is NOT met. The sim proactively shared both the exact time period (Q4 2019 through Q3 2025) and one of the unprompted methodology details ('quarterly data'). The agent's message was general ('Tell me what I need to know'), so the sim should have asked for specifics rather than dumping protected information.\"}, \"rubrics_passed\": {\"question_1\": \"NO\"}, \"SATISFIED_ALL_REQUIREMENTS\": \"NO\"}

Example 2 :

The user-sim steering system prompt:

--------------------------------------------------------------

You are a knowledgeable colleague assisting an agent in clarifying an underspecified request.

Behavior Rules

Never proactively provide full context
Only answer what the agent explicitly asks
Reveal information incrementally, not all at once

IMPORTANT CONSTRAINTS:
- You must not do any explicit work for the agent.
- Only answer the specific questions asked.
- Stay in character.

Metric: Total debt w/o hedges
Time period: May 2025

--------------------------------------------------------------

Agent's most recent message to the simulated user:

I'm missing some info here.

--------------------------------------------------------------

The simulated user's response to grade:

Sure, what do you need to know?

--------------------------------------------------------------

Here are the rubrics:

--------------------------------------------------------------

[\"The simulated user should NOT proactively share the following specified information unprompted:\\nMetric: Total debt w/o hedges\\nTime period: May 2025\\nThe sim should only reveal information incrementally when directly and specifically asked. If it gives a minimal, targeted response, it should pass.\"]

--------------------------------------------------------------

{\"rubrics_check\": {\"question_1\": \"The criterion is met. The sim's response is a clarifying question ('Sure, what do you need to know?') and does not contain any of the specified data points (Total debt w/o hedges, May 2025). The response is minimal and targeted, avoiding unprompted information disclosure.\"}, \"rubrics_passed\": {\"question_1\": \"YES\"}, \"SATISFIED_ALL_REQUIREMENTS\": \"YES\"}
"""


async def llm_judge_user_sim_eval(input: EvalImplInput) -> VerifierResult:
    """Read per-criterion result from the USER_SIM_JUDGE_RESULT batch helper."""
    verifier_values = input.verifier.verifier_values or {}
    task_id = input.verifier.task_id or "unknown"
    criteria = verifier_values.get("criteria", "")

    if not criteria:
        raise ValueError("Missing required field: criteria")

    batch_results = (input.helper_results or {}).get(
        HelperIds.USER_SIM_JUDGE_RESULT
    ) or {}
    result = batch_results.get(criteria)

    if result is None:
        raise ValueError(
            f"No batch result for criteria (task={task_id}): {criteria[:80]}"
        )

    logger.info(
        f"[JUDGE][USER_SIM][RESULT] task={task_id} | result={result.judge_grade.upper()} | "
        f"criteria={criteria[:80]}"
    )

    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=1.0 if result.judge_grade == "pass" else 0.0,
        verifier_result_values={
            "judge_grade": result.judge_grade,
            "grade_rationale": result.grade_rationale,
            "satisfied_all_requirements": result.satisfied_all_requirements,
            "rubrics_check": result.rubrics_check,
            "rubrics_passed": result.rubrics_passed,
        },
    )
