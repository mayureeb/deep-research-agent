"""LLM-as-judge with a structured rubric.

Uses JUDGE_MODEL (different from the producer). Scores five dimensions
plus an overall, on a 0-5 scale. Combine with grounding / coverage /
calibration; not a sole quality signal.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from src.config import Config
from src.llm import chat_json
from src.state import ResearchRun


class JudgeScores(BaseModel):
    """The judge's structured output. Each dimension is a 0-5 float."""
    correctness: float = Field(ge=0, le=5, description="Are the claims actually true?")
    completeness: float = Field(ge=0, le=5, description="Does it address the prompt fully?")
    calibration: float = Field(ge=0, le=5, description="Are confidence labels honest?")
    source_quality: float = Field(ge=0, le=5, description="Are sources credible and diverse?")
    conflict_handling: float = Field(ge=0, le=5, description="Does it surface real disagreements?")
    overall: float = Field(ge=0, le=5)
    notes: str  # 1-3 sentences calling out strongest / weakest aspects


SYSTEM = """You are a strict reviewer of a research report.

Score the report 0-5 on each dimension, where:
  0 = absent/wrong; 2 = weak; 3 = adequate; 4 = strong; 5 = excellent

Dimensions:
- correctness: are the factual claims actually true (per your knowledge)?
- completeness: does it address all major aspects of the prompt?
- calibration: do high-confidence claims look more solid than low-confidence ones?
- source_quality: are sources credible (peer-reviewed > .org/.edu > reputable news > blogs)? Are sources diverse?
- conflict_handling: when the literature disagrees, does the report surface that vs. smooth it over?
- overall: your single-number summary

Be willing to give low scores. Most reports score 2-4 on most dimensions.
Add specific notes (1-3 sentences) calling out the strongest and weakest aspects."""


def llm_judge(cfg: Config, rr: ResearchRun, rubric: list[str] | None = None) -> dict:
    """Run the LLM judge on one ResearchRun.

    Args:
        cfg: Config; uses judge_model.
        rr: The ResearchRun to score. Must have a non-None report.
        rubric: Optional per-prompt rubric.

    Returns:
        On success: JudgeScores.model_dump() dict.
        On failure: {"error": "..."}.
    """
    if not rr.report:
        return {"error": "no report"}

    # Claims with stated confidence; the judge needs both for calibration.
    claims_text = "\n".join(
        f"- ({c.confidence:.2f}) {c.claim}" for c in rr.report.claims
    )
    sources = sorted({f.source_url for f in rr.findings.findings})

    rubric_text = ""
    if rubric:
        rubric_text = "\n\nThe prompt's specific rubric:\n" + "\n".join(
            f"- {r}" for r in rubric
        )

    user = (
        f"PROMPT:\n{rr.user_prompt}\n{rubric_text}\n\n"
        f"REPORT SUMMARY:\n{rr.report.summary}\n\n"
        f"CLAIMS (with stated confidence):\n{claims_text}\n\n"
        f"SOURCES USED ({len(sources)}):\n" + "\n".join(f"- {s}" for s in sources) + "\n\n"
        f"CONTRADICTIONS SURFACED:\n" +
        ("\n".join(f"- {c}" for c in rr.report.contradictions_surfaced)
         if rr.report.contradictions_surfaced else "(none)")
    )

    try:
        scores = chat_json(
            cfg.anthropic_api_key, cfg.judge_model, SYSTEM, user, JudgeScores,
            max_retries=1,
        )
        return scores.model_dump()
    except Exception as e:
        # Don't crash the eval on a judge hiccup; consumers render "—".
        return {"error": str(e)}
