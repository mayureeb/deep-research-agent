"""Extract atomic factual claims from a free-form report.

Lets the baseline pipeline be graded on grounding the same way the
structured pipeline is. One LLM call per baseline report on judge_model.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from src.config import Config
from src.llm import chat_json


class ExtractedClaims(BaseModel):
    """Structured output: a list of claim sentences."""
    claims: list[str] = Field(
        description="Atomic factual claims extracted from the report. "
                    "Each is a single sentence stating one specific, "
                    "verifiable thing. NOT opinions, framing, or transitions."
    )


SYSTEM = """You extract atomic factual claims from a research report.

A claim:
- Is ONE specific factual statement that could be true or false
- Could in principle be checked against a source
- Is self-contained (don't extract pronoun references that need surrounding context)

NOT claims:
- "This report covers X." (meta)
- "X is interesting." (opinion)
- "The literature is rich." (vague)
- Definitions of terms

Aim for 6-15 atomic claims. If the report is mostly opinion/framing, return fewer.

Return a list of claim sentences."""


def extract_claims(cfg: Config, report_text: str) -> list[str]:
    """Extract atomic claims from free-form report prose.

    Args:
        cfg: Config; uses judge_model.
        report_text: The baseline's final_report_text.

    Returns:
        List of claim sentences. Empty on input failure or LLM error.
    """
    if not report_text or not report_text.strip():
        return []
    try:
        result = chat_json(
            cfg.anthropic_api_key, cfg.judge_model, SYSTEM,
            f"REPORT:\n\n{report_text}\n\nExtract atomic claims.",
            ExtractedClaims, max_retries=1,
        )
        return result.claims
    except Exception:
        # Empty list cascades to grounding_freeform returning rate=0.0.
        return []
