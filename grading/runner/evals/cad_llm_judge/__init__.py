"""CAD LLM judge: a single-shot judge over digested CAD files in the final snapshot."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .main import cad_llm_judge_eval

__all__ = ["cad_llm_judge_eval"]


def __getattr__(name: str) -> Any:
    # Lazy so the geometry worker child, started as a submodule of this
    # package, does not import the judge and the LLM stack behind it.
    if name == "cad_llm_judge_eval":
        from .main import cad_llm_judge_eval  # noqa: PLC0415

        return cad_llm_judge_eval
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
