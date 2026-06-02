"""Plan judge — LLM-as-judge over the planner's decomposition.

One LLM call per prompt returning two scores: surface coverage (does
the decomposition cover the prompt's surface area?) and intent
alignment (do the sub-questions stay on-topic?). Blind to the final
report by design.
"""
from __future__ import annotations

from src.config import Config
from src.llm import chat_json
from src.state import Decomposition, PlanJudgeResult


_PLAN_JUDGE_SYSTEM = """You evaluate a research-agent's PLAN against the user's prompt.

You see ONLY the user prompt and the decomposition (a list of sub-questions
with rationales). You do NOT see the final report — your job is to judge
the PLAN, not the answer.

Score two dimensions independently, each 0.0–1.0 with partial credit:

1. SURFACE COVERAGE
   Do the sub-questions cover the prompt's surface area? Imagine a domain
   expert reading the prompt — would they say "the decomposition addresses
   every major axis the prompt asks about"?
     1.0 = no major axis missing.
     0.5 = one major axis is missing or under-covered.
     0.0 = the decomposition skips what the prompt centrally asks.
   List `missing_axes` for any gap (one short noun-phrase per gap, e.g.
   "evaluation methodology", "cost considerations").

2. INTENT ALIGNMENT
   Are the sub-questions about what the user asked, or have they drifted
   to adjacent topics? "Drifted but interesting" still counts as drifted
   — the prompt is the contract.
     1.0 = every sub-question is on-topic.
     0.5 = some scope creep but the core is intact.
     0.0 = the decomposition has wandered to a different problem.
   List `drift_examples` (sub-question id + a brief description of how
   it drifts).

Be strict but charitable. A sub-question that's "narrowly on-topic but
peripheral" is on-topic (don't penalize it under intent_alignment), but
if the prompt asks about A and B and the decomposition only covers A,
that's a surface_coverage problem.

Provide one short paragraph of `reasoning` summarizing both scores."""


def plan_judge(
    cfg: Config,
    user_prompt: str,
    decomp: Decomposition,
) -> PlanJudgeResult:
    """Run the combined plan judge over one decomposition.

    Falls back to a 0.0/0.0 result with the error in `reasoning` if the
    LLM call raises — the eval row should never crash on a single judge
    failure (mirrors the defensive behavior of `llm_judge`).
    """
    if not decomp or not decomp.subquestions:
        return PlanJudgeResult(
            surface_coverage=0.0, intent_alignment=0.0,
            missing_axes=[], drift_examples=[],
            reasoning="empty decomposition — nothing to judge",
        )

    decomp_text = "\n".join(
        f"  [{sq.id}] {sq.question}\n      rationale: {sq.rationale or '(none)'}"
        for sq in decomp.subquestions
    )
    user = (
        f"USER PROMPT:\n{user_prompt}\n\n"
        f"DECOMPOSITION (sub-questions the planner produced):\n{decomp_text}\n\n"
        f"Score surface_coverage and intent_alignment per the system instructions."
    )

    try:
        return chat_json(
            cfg.anthropic_api_key, cfg.judge_model,
            _PLAN_JUDGE_SYSTEM, user, PlanJudgeResult,
            max_retries=1,
        )
    except Exception as e:
        return PlanJudgeResult(
            surface_coverage=0.0, intent_alignment=0.0,
            missing_axes=[], drift_examples=[],
            reasoning=f"plan judge failed: {e}",
        )
