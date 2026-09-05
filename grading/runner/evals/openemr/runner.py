"""Verification Configuration - Data models for clinical verification.

This module contains the configuration and result models used by OpenEMR verifiers.
The actual verification logic is in clinical_verification.py (adapted for mercor-mcp-verifiers).
"""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field


class VerificationResult(BaseModel):
    """Comprehensive verification result for a clinical encounter."""

    encounter_id: int
    patient_id: str
    overall_pass: bool
    overall_score: float
    sql_checks: dict[str, bool] = Field(default_factory=dict)
    llm_scores: dict[str, float] = Field(default_factory=dict)
    feedback: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


@dataclass
class VerificationConfig:
    """Configuration for verification run.

    Controls which verification layers run and their behavior.
    """

    # SQL verification options
    check_differential: bool = True

    # LLM verification options
    use_llm: bool = True
    use_mock_llm: bool = False
    llm_pass_threshold: float = 70.0

    # Scoring weights (must sum to 1.0)
    sql_weight: float = 0.4
    llm_weight: float = 0.6

    # Ground truth for evaluation
    true_diagnosis: str | list[str] | None = None

    def __post_init__(self) -> None:
        """Validate configuration."""
        if abs(self.sql_weight + self.llm_weight - 1.0) > 0.01:
            raise ValueError(
                f"Weights must sum to 1.0, got {self.sql_weight + self.llm_weight}"
            )
