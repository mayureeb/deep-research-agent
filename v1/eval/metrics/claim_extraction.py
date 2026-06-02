"""Extract atomic factual claims from a free-form report.

Used to put the baseline (free-form prose) on equal footing with the
structured v0 system (structured claims) for the grounding metric. Without
this, we couldn't fairly compare grounding rates — the structured side
has explicit claims to grade; the baseline has none.

Pipeline:
  baseline_run.report.final_report_text  (free-form prose)
        ↓ extract_claims (this module)
  list[str] of atomic claims
        ↓ grounding_freeform (next module)
  grounding judgment per claim against pooled source text

What counts as an atomic claim:
  * One specific factual statement that could be true or false.
  * Could in principle be checked against a source.
  * Self-contained (no pronoun references that need surrounding context).

What we EXCLUDE:
  * Meta statements ("This report covers X.")
  * Opinions ("X is interesting.")
  * Vague framing ("The literature is rich.")
  * Definitions of terms.

The exclusion list is in the SYSTEM prompt — without it, extraction
inflates with non-claims that can't be grounded against any source.

Target count: 6-15 atomic claims. The "if mostly opinion/framing, return
fewer" instruction lets the extractor honestly bottom out on weak reports
rather than padding to 15.

Cost: 1 LLM call per baseline report. Uses judge_model (Opus by default)
because extraction quality matters — bad extraction means meaningless
grounding numbers.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from src.config import Config
from src.llm import chat_json


class ExtractedClaims(BaseModel):
    """Structured output of the extractor — a list of claim sentences."""
    claims: list[str] = Field(
        description="Atomic factual claims extracted from the report. "
                    "Each is a single sentence stating one specific, "
                    "verifiable thing. NOT opinions, framing, or transitions."
    )


# Extractor prompt. The exclusion list is the load-bearing part — without
# it the extractor pulls every sentence out of the report.
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
        cfg: Config (uses judge_model — extraction quality matters).
        report_text: The baseline's final_report_text. Empty / whitespace-
            only input returns [] without an LLM call.

    Returns:
        List of claim sentences. Empty on input failure or LLM error
        (we swallow exceptions because returning [] cleanly is better
        than crashing the whole baseline_compare run).
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
        # Don't crash the baseline-compare pipeline on extraction failure.
        # An empty list cascades to grounding_freeform returning rate=0.0,
        # which renders correctly in the comparison table.
        return []
