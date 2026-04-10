"""LLM-based verification for clinical quality assessment.

Layer 2 of the 3-layer verification system.
Uses the standard LiteLLM infrastructure for model-agnostic evaluation.
"""

from dataclasses import dataclass, field
from typing import Any

from litellm import Choices
from loguru import logger

from runner.utils.llm import build_messages, call_llm

# Default timeout for LLM calls (5 minutes)
LLM_TIMEOUT = 300

# Scoring thresholds (configurable)
DEFAULT_PASS_THRESHOLD = 70.0


@dataclass
class LLMEvaluation:
    """Result from LLM-based evaluation."""

    name: str
    score: float  # 0-100 scale
    passed: bool  # True if score >= threshold
    feedback: str
    details: dict[str, Any] = field(default_factory=dict)


class LLMJudge:
    """LLM-based judge for clinical quality evaluation using standard infrastructure."""

    def __init__(
        self,
        model: str,
        pass_threshold: float = DEFAULT_PASS_THRESHOLD,
        extra_args: dict[str, Any] | None = None,
    ):
        """Initialize LLM Judge.

        Args:
            model: Full model string (e.g., "gemini/gemini-2.5-flash")
            pass_threshold: Score threshold for passing (0-100)
            extra_args: Extra LLM arguments (temperature, max_tokens, etc.)
        """
        self.model = model
        self.pass_threshold = pass_threshold
        self.extra_args = extra_args or {"max_tokens": 1024}

    async def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        """Call LLM using standard infrastructure.

        Args:
            system_prompt: System instructions
            user_prompt: User message/content to evaluate

        Returns:
            LLM response text
        """
        messages = build_messages(system_prompt, user_prompt)
        response = await call_llm(
            model=self.model,
            messages=messages,
            timeout=LLM_TIMEOUT,
            extra_args=self.extra_args,
        )
        # Extract content from response (same pattern as output_llm)
        choices = response.choices
        if not choices or not isinstance(choices[0], Choices):
            raise ValueError("LLM returned empty response")
        raw_content = choices[0].message.content
        return str(raw_content) if raw_content else ""

    def _parse_score_response(self, response: str) -> tuple[float, str]:
        """Parse LLM response to extract score and feedback.

        Expected format:
        SCORE: XX
        FEEDBACK: ...

        Args:
            response: Raw LLM response

        Returns:
            Tuple of (score, feedback)
        """
        lines = response.strip().split("\n")
        score = 50.0  # Default
        feedback_lines: list[str] = []
        in_feedback = False

        for line in lines:
            if line.upper().startswith("SCORE:"):
                try:
                    score_str = line.split(":", 1)[1].strip()
                    # Handle "XX/100" format
                    if "/" in score_str:
                        score_str = score_str.split("/")[0]
                    score = float(score_str)
                    score = max(0, min(100, score))  # Clamp to 0-100
                except (ValueError, IndexError):
                    logger.warning(f"Could not parse score from: {line}")
            elif line.upper().startswith("FEEDBACK:"):
                in_feedback = True
                feedback_text = line.split(":", 1)[1].strip() if ":" in line else ""
                if feedback_text:
                    feedback_lines.append(feedback_text)
            elif in_feedback:
                feedback_lines.append(line)

        feedback = " ".join(feedback_lines).strip() or response
        return score, feedback

    async def evaluate_documentation_completeness(
        self,
        note_content: str,
    ) -> LLMEvaluation:
        """Evaluate clinical documentation for completeness."""
        system_prompt = """You are a clinical documentation quality evaluator.
Evaluate the provided clinical note for completeness and quality.

Check for these required sections:
1. HPI (History of Present Illness) - Chief complaint, onset, duration, quality, severity
2. ROS (Review of Systems) - Systematic review of organ systems
3. Physical Exam - Vital signs and relevant exam findings
4. Assessment - Working diagnosis and clinical impression
5. Plan - Treatment plan, orders, follow-up

Score 0-100 based on:
- Presence of all sections (40 points)
- Substantive content in each section (40 points)
- Clinical coherence and completeness (20 points)

Respond ONLY in this format:
SCORE: [0-100]
FEEDBACK: [Specific feedback about missing/incomplete sections and suggestions]"""

        user_prompt = f"""Evaluate this clinical note:

{note_content}"""

        try:
            response = await self._call_llm(system_prompt, user_prompt)
            score, feedback = self._parse_score_response(response)

            return LLMEvaluation(
                name="documentation_completeness",
                score=score,
                passed=score >= self.pass_threshold,
                feedback=feedback,
                details={
                    "note_length": len(note_content),
                    "threshold": self.pass_threshold,
                },
            )
        except Exception as e:
            logger.error(f"Documentation evaluation failed: {e}")
            return LLMEvaluation(
                name="documentation_completeness",
                score=0,
                passed=False,
                feedback=f"Evaluation failed: {e!s}",
                details={"error": str(e)},
            )

    async def evaluate_differential_quality(
        self,
        differential: list[str],
        symptoms: str,
        reasoning: str | None = None,
    ) -> LLMEvaluation:
        """Evaluate quality of differential diagnosis."""
        system_prompt = """You are a clinical reasoning evaluator.
Evaluate the differential diagnosis list for appropriateness and completeness.

Criteria:
1. Appropriateness (40 points): Do diagnoses fit the presentation?
2. Critical diagnoses (30 points): Are life-threatening conditions considered?
3. Breadth (20 points): Covers multiple organ systems/categories?
4. Reasoning (10 points): If reasoning provided, is it sound?

Note: Medicine allows multiple correct approaches. Focus on sound reasoning,
not a single "right answer."

Respond ONLY in this format:
SCORE: [0-100]
FEEDBACK: [Specific feedback about differential quality and any missing critical diagnoses]"""

        diff_list = "\n".join(f"- {d}" for d in differential)
        user_prompt = f"""Patient presentation/symptoms:
{symptoms}

Differential diagnosis:
{diff_list}

{"Clinical reasoning: " + reasoning if reasoning else ""}"""

        try:
            response = await self._call_llm(system_prompt, user_prompt)
            score, feedback = self._parse_score_response(response)

            return LLMEvaluation(
                name="differential_quality",
                score=score,
                passed=score >= self.pass_threshold,
                feedback=feedback,
                details={
                    "diagnosis_count": len(differential),
                    "has_reasoning": bool(reasoning),
                    "threshold": self.pass_threshold,
                },
            )
        except Exception as e:
            logger.error(f"Differential evaluation failed: {e}")
            return LLMEvaluation(
                name="differential_quality",
                score=0,
                passed=False,
                feedback=f"Evaluation failed: {e!s}",
                details={"error": str(e)},
            )

    async def evaluate_order_appropriateness(
        self,
        orders: list[dict[str, Any]],
        diagnosis: str | None,
        symptoms: str,
    ) -> LLMEvaluation:
        """Evaluate appropriateness of clinical orders."""
        system_prompt = """You are a clinical order appropriateness evaluator.
Evaluate whether the ordered tests/procedures are appropriate for the presentation.

Criteria:
1. Alignment with presentation (40 points): Orders match symptoms/diagnosis
2. Completeness (30 points): Key workup items included
3. No excessive orders (20 points): Orders are necessary, not "shotgun" approach
4. Priority appropriateness (10 points): Urgent items marked appropriately

Respond ONLY in this format:
SCORE: [0-100]
FEEDBACK: [Specific feedback about order appropriateness, missing/unnecessary orders]"""

        orders_text = "\n".join(
            f"- [{o.get('order_type', 'unknown')}] {o.get('description', 'N/A')} "
            f"(Priority: {o.get('priority', 'routine')})"
            for o in orders
        )

        user_prompt = f"""Patient presentation/symptoms:
{symptoms}

{"Working diagnosis: " + diagnosis if diagnosis else "No diagnosis provided yet"}

Orders placed:
{orders_text if orders_text else "No orders placed"}"""

        try:
            response = await self._call_llm(system_prompt, user_prompt)
            score, feedback = self._parse_score_response(response)

            return LLMEvaluation(
                name="order_appropriateness",
                score=score,
                passed=score >= self.pass_threshold,
                feedback=feedback,
                details={
                    "order_count": len(orders),
                    "has_diagnosis": bool(diagnosis),
                    "threshold": self.pass_threshold,
                },
            )
        except Exception as e:
            logger.error(f"Order evaluation failed: {e}")
            return LLMEvaluation(
                name="order_appropriateness",
                score=0,
                passed=False,
                feedback=f"Evaluation failed: {e!s}",
                details={"error": str(e)},
            )

    async def evaluate_diagnostic_accuracy(
        self,
        final_diagnosis: str,
        true_diagnosis: str | list[str],
        symptoms: str | None = None,
    ) -> LLMEvaluation:
        """Evaluate diagnostic accuracy against known diagnosis."""
        system_prompt = """You are a diagnostic accuracy evaluator.
Evaluate whether the provided diagnosis matches or is clinically equivalent
to the true diagnosis.

Scoring:
- Exact match or clinical equivalent: 100 points
- Same disease category/family: 80 points
- Related condition on differential: 60 points
- Partially correct (one component right): 40 points
- Incorrect but reasonable given presentation: 20 points
- Completely wrong: 0 points

Medicine allows multiple correct approaches. A "reasonable" diagnosis
supported by the presentation is acceptable even if not the exact ground truth.

Respond ONLY in this format:
SCORE: [0-100]
FEEDBACK: [Explanation of how the diagnosis compares to ground truth]"""

        if isinstance(true_diagnosis, list):
            true_dx_text = ", ".join(true_diagnosis)
        else:
            true_dx_text = true_diagnosis

        user_prompt = f"""{"Patient presentation: " + symptoms if symptoms else ""}

Agent's diagnosis: {final_diagnosis}

True diagnosis (ground truth): {true_dx_text}"""

        try:
            response = await self._call_llm(system_prompt, user_prompt)
            score, feedback = self._parse_score_response(response)

            return LLMEvaluation(
                name="diagnostic_accuracy",
                score=score,
                passed=score >= self.pass_threshold,
                feedback=feedback,
                details={
                    "final_diagnosis": final_diagnosis,
                    "true_diagnosis": true_diagnosis,
                    "threshold": self.pass_threshold,
                },
            )
        except Exception as e:
            logger.error(f"Diagnostic accuracy evaluation failed: {e}")
            return LLMEvaluation(
                name="diagnostic_accuracy",
                score=0,
                passed=False,
                feedback=f"Evaluation failed: {e!s}",
                details={"error": str(e)},
            )

    async def evaluate_overall_encounter(
        self,
        note_content: str,
        differential: list[str],
        orders: list[dict[str, Any]],
        symptoms: str,
        final_diagnosis: str | None = None,
        true_diagnosis: str | None = None,
        reasoning: str | None = None,
    ) -> dict[str, LLMEvaluation]:
        """Run all LLM evaluations for an encounter."""
        results: dict[str, LLMEvaluation] = {}

        # Documentation completeness
        results[
            "documentation_completeness"
        ] = await self.evaluate_documentation_completeness(note_content)

        # Differential quality
        results["differential_quality"] = await self.evaluate_differential_quality(
            differential, symptoms, reasoning
        )

        # Order appropriateness
        results["order_appropriateness"] = await self.evaluate_order_appropriateness(
            orders, final_diagnosis, symptoms
        )

        # Diagnostic accuracy (if ground truth available)
        if final_diagnosis and true_diagnosis:
            results["diagnostic_accuracy"] = await self.evaluate_diagnostic_accuracy(
                final_diagnosis, true_diagnosis, symptoms
            )

        return results


class MockLLMJudge(LLMJudge):
    """Mock LLM Judge for testing without API calls."""

    def __init__(
        self,
        model: str = "mock",
        pass_threshold: float = DEFAULT_PASS_THRESHOLD,
        extra_args: dict[str, Any] | None = None,
    ):
        super().__init__(model, pass_threshold, extra_args)

    async def evaluate_documentation_completeness(
        self,
        note_content: str,
    ) -> LLMEvaluation:
        """Mock documentation evaluation."""
        sections = ["hpi", "assessment", "plan"]
        content_lower = note_content.lower()
        found = sum(1 for s in sections if s in content_lower)
        score = (found / len(sections)) * 100

        return LLMEvaluation(
            name="documentation_completeness",
            score=score,
            passed=score >= self.pass_threshold,
            feedback=f"Mock evaluation: Found {found}/{len(sections)} key sections",
            details={"mock": True, "sections_found": found},
        )

    async def evaluate_differential_quality(
        self,
        differential: list[str],
        symptoms: str,
        reasoning: str | None = None,
    ) -> LLMEvaluation:
        """Mock differential evaluation."""
        count = len(differential)
        score = min(100, count * 25)

        return LLMEvaluation(
            name="differential_quality",
            score=score,
            passed=score >= self.pass_threshold,
            feedback=f"Mock evaluation: {count} diagnoses provided",
            details={"mock": True, "diagnosis_count": count},
        )

    async def evaluate_order_appropriateness(
        self,
        orders: list[dict[str, Any]],
        diagnosis: str | None,
        symptoms: str,
    ) -> LLMEvaluation:
        """Mock order evaluation."""
        count = len(orders)
        score = min(100, count * 20)

        return LLMEvaluation(
            name="order_appropriateness",
            score=score,
            passed=score >= self.pass_threshold,
            feedback=f"Mock evaluation: {count} orders placed",
            details={"mock": True, "order_count": count},
        )

    async def evaluate_diagnostic_accuracy(
        self,
        final_diagnosis: str,
        true_diagnosis: str | list[str],
        symptoms: str | None = None,
    ) -> LLMEvaluation:
        """Mock diagnostic accuracy evaluation."""
        if isinstance(true_diagnosis, list):
            match = any(
                t.lower() in final_diagnosis.lower()
                or final_diagnosis.lower() in t.lower()
                for t in true_diagnosis
            )
        else:
            match = (
                true_diagnosis.lower() in final_diagnosis.lower()
                or final_diagnosis.lower() in true_diagnosis.lower()
            )

        score = 100 if match else 30

        return LLMEvaluation(
            name="diagnostic_accuracy",
            score=score,
            passed=score >= self.pass_threshold,
            feedback=f"Mock evaluation: {'Match' if match else 'No match'} found",
            details={"mock": True, "match": match},
        )
