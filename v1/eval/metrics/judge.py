"""LLM-as-judge with a structured rubric.

Uses a different/stronger model than the producer (configured via
JUDGE_MODEL). Asks for scores on five rubric dimensions plus an overall.
NOT meant as a sole quality signal — combine with grounding / coverage /
calibration above.

Why LLM-as-judge despite the obvious circularity (using an LLM to judge
an LLM):
  * No ground truth exists for open-ended research. Human review at scale
    isn't feasible during dev iteration.
  * Modern judge models (Opus default here) are reasonably calibrated on
    "is this report well-supported / well-organized" tasks.
  * Different model from the producer mitigates same-bias inflation.

Why a STRUCTURED rubric (not a single overall score):
  * Per-dimension scores make miscalibration visible. If correctness is
    high but conflict_handling is low, that's actionable.
  * The rubric gives the judge something specific to look for —
    'overall: 4/5' is too easy to give without engagement.

Five dimensions chosen for v0 (see SYSTEM prompt):
  * correctness        — are factual claims actually true?
  * completeness       — does it address the prompt?
  * calibration        — do high-conf claims look more solid than low-conf?
  * source_quality     — credible & diverse sources?
  * conflict_handling  — does it surface real disagreements?

Plus per-prompt rubric (the BenchmarkPrompt.rubric list). Visible to the
judge so its scores align with the prompt's specific deliverables.

Scoring scale (0-5):
  0 = absent/wrong, 2 = weak, 3 = adequate, 4 = strong, 5 = excellent.
The prompt explicitly tells the judge to be willing to give low scores —
without that nudge, judges anchor on 3-4 for everything.

Cost: 1 LLM call per prompt evaluation. Cheap relative to grounding (which
is per-claim).
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


# Judge prompt. Two key phrases:
#   * "Be willing to give low scores. Most reports score 2-4 on most
#     dimensions." — counters anchoring on 3-4.
#   * The dimension definitions in plain English so the judge can't
#     interpret-around them.
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
        cfg: Config (uses judge_model — Opus by default).
        rr: The ResearchRun to score. Must have a non-None report.
        rubric: Optional per-prompt rubric (from BenchmarkPrompt.rubric).
            Visible to the judge so scores align with prompt-specific
            deliverables.

    Returns:
        On success: dict with scores per dimension + overall + notes
        (JudgeScores.model_dump()).
        On failure: dict with `error` key and the exception text.

    Cost: 1 judge LLM call.
    """
    if not rr.report:
        return {"error": "no report"}

    # Compact rendering of claims with their stated confidence — the
    # judge needs both to score calibration.
    claims_text = "\n".join(
        f"- ({c.confidence:.2f}) {c.claim}" for c in rr.report.claims
    )
    sources = sorted({f.source_url for f in rr.findings.findings})

    rubric_text = ""
    if rubric:
        # Append the per-prompt rubric to the user message so the judge
        # can score against it. Bullet list is easiest to scan.
        rubric_text = "\n\nThe prompt's specific rubric:\n" + "\n".join(
            f"- {r}" for r in rubric
        )

    user = (
        f"PROMPT:\n{rr.user_prompt}\n{rubric_text}\n\n"
        f"REPORT SUMMARY:\n{rr.report.summary}\n\n"
        f"CLAIMS (with stated confidence):\n{claims_text}\n\n"
        f"SOURCES USED ({len(sources)}):\n" + "\n".join(f"- {s}" for s in sources) + "\n\n"
        f"CONTRADICTIONS SURFACED:\n" +
        ("\n".join(
            f"- [{c.severity}/{c.type}] {c.description}"
            for c in rr.report.contradictions_surfaced)
         if rr.report.contradictions_surfaced else "(none)")
    )

    try:
        scores = chat_json(
            cfg.anthropic_api_key, cfg.judge_model, SYSTEM, user, JudgeScores,
            max_retries=1,
        )
        return scores.model_dump()
    except Exception as e:
        # Don't crash the eval on a judge hiccup. The error key signals
        # to consumers (eval/run_eval._print_summary) to render "—" in
        # the table.
        return {"error": str(e)}
