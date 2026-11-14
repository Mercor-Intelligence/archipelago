"""Prompts for the response tool verifier.

These prompts are aligned with output_llm verifier prompts for consistency.
"""

from pydantic import BaseModel, Field

# ==========================================================================
# STRUCTURED OUTPUT SCHEMAS
# ==========================================================================


class GradingResponseSchema(BaseModel):
    rationale: str = Field(description="Explanation of the assessment")
    is_criteria_true: bool = Field(description="Whether the criteria is met")


# ==========================================================================
# CONSTANTS
# ==========================================================================

SECTION_SEPARATOR: str = "\n\n"

# ==========================================================================
# REUSABLE PROMPT COMPONENTS
# ==========================================================================

STRICT_CRITERION_MATCHING: str = """<EVALUATION_STANDARD>
Every specific detail in the criterion must be precisely verified with exact values, identifiers, and specifications - partial or approximate matches are insufficient.

TOOL OUTPUT EVALUATION:
- If criterion mentions specific output (e.g., "file created"), verify the tool artifacts show this
- Agent's text claims about actions are NOT evidence - only tool execution results count
- If no tool artifacts exist for a claimed action, the criterion is NOT met
- Base64 decoded content should be evaluated for the actual content
</EVALUATION_STANDARD>"""

TOLERANCE_NOTES: str = """<TOLERANCE_RULES>
NUMERIC FORMATTING:
- Formatting differences are acceptable if substantively correct
- e.g. $153.5 and $153.50 are equivalent; 10.0 and 10 are equivalent

ROUNDING:
- Values that round to the criterion's precision are acceptable
- e.g. $2.07B rounds to $2.1B → MEETS criterion asking for "$2.1bn"
- Applies to billions, millions, percentages, etc.
- If criterion specifies rounding rules, use those instead
</TOLERANCE_RULES>"""

RATIONALE_FORMAT: str = """<RATIONALE_FORMAT>
Your rationale must be structured and concise. You must provide the assessment section with the structure below.

## Assessment
- Criterion requirement: Quote what the criterion specifically asks for
- Evidence: What you found in the agent's output (cite specific values, text, or content)
- Conclusion: Whether criterion is met and why (1-2 sentences)

LENGTH CONSTRAINTS:
- Keep your rationale under 300-400 words
- Only cite relevant snippets (1-3 lines max)
- For large content, summarize rather than reproducing
</RATIONALE_FORMAT>"""

JSON_OUTPUT_GRADING: str = """<OUTPUT_FORMAT>
Respond with a JSON object:
{
  "rationale": #string,
  "is_criteria_true": #boolean
}
- rationale: Your structured explanation following the RATIONALE_FORMAT above
- is_criteria_true: true if criterion is met, false if not
</OUTPUT_FORMAT>"""


# ==========================================================================
# GRADING SYSTEM PROMPT
# ==========================================================================

_GRADING_SYSTEM_BASE: str = """You are an expert evaluator grading an AI agent's work. Determine if a specific verification criterion was met based on the agent's output (response text and/or tool execution artifacts). Be precise, evidence-based, and objective.

<GRADING_PRINCIPLES>
- Focus on what the criterion specifically asks - nothing more, nothing less
- Don't penalize for aspects not mentioned in the criterion
- Base your assessment only on the evidence provided
- Be objective and consistent
</GRADING_PRINCIPLES>

<TOOL_ARTIFACT_RULES>
- Evaluate both the agent's text response AND any tool execution results
- Tool artifacts include: code execution outputs, file contents, decoded base64 data
- If criterion requires tool execution, verify it in the TOOL_ARTIFACTS section
- Agent's claims about tool actions must be supported by actual artifacts
- If no artifacts exist for a claimed action, the criterion may NOT be met
</TOOL_ARTIFACT_RULES>"""

GRADING_SYSTEM_PROMPT: str = (
    _GRADING_SYSTEM_BASE
    + SECTION_SEPARATOR
    + STRICT_CRITERION_MATCHING
    + SECTION_SEPARATOR
    + TOLERANCE_NOTES
    + SECTION_SEPARATOR
    + RATIONALE_FORMAT
    + SECTION_SEPARATOR
    + JSON_OUTPUT_GRADING
)


# ==========================================================================
# USER PROMPT TEMPLATES
# ==========================================================================

GRADING_USER_PROMPT: str = """Here is the original task and the agent's output for evaluation:

<ORIGINAL_TASK>
{task_prompt}
</ORIGINAL_TASK>

<AGENT_OUTPUT>
<RESPONSE>
{agent_response}
</RESPONSE>

<TOOL_ARTIFACTS>
{tool_artifacts}
</TOOL_ARTIFACTS>
</AGENT_OUTPUT>

<VERIFICATION_CRITERIA>
{criteria}
</VERIFICATION_CRITERIA>
{criteria_explanation}
<REMINDER>
- Evaluate if the agent's output meets the criterion
- Consider both the text response AND tool execution artifacts
- Use the RATIONALE_FORMAT from system instructions
- Return JSON with rationale and is_criteria_true
</REMINDER>"""

GRADING_USER_PROMPT_NO_ARTIFACTS: str = """Here is the original task and the agent's output for evaluation:

<ORIGINAL_TASK>
{task_prompt}
</ORIGINAL_TASK>

<AGENT_OUTPUT>
<RESPONSE>
{agent_response}
</RESPONSE>
</AGENT_OUTPUT>

<VERIFICATION_CRITERIA>
{criteria}
</VERIFICATION_CRITERIA>
{criteria_explanation}
<REMINDER>
- Evaluate if the agent's output meets the criterion
- Use the RATIONALE_FORMAT from system instructions
- Return JSON with rationale and is_criteria_true
</REMINDER>"""
