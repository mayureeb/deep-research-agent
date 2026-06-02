"""ACE — Adaptively-generated Checklist Evaluation.

Methodology from Wan et al. 2026 ("DeepResearch Arena", arXiv:2509.01396).
Two-stage protocol that addresses a known LLM-as-judge failure mode:
when one model both INVENTS the rubric AND scores against it, the rubric
silently drifts to flatter the candidate. Separating the stages removes
this leakage.

Stage 1 (CHECKLIST GENERATION):
  Given only the user prompt (NOT the candidate report), an LLM generates
  a task-specific rubric: 5–10 criteria a good answer would meet. The
  candidate report is hidden from this stage to prevent rubric drift.

Stage 2 (SCORING):
  A separate LLM call (ideally a different model or a fresh context) scores
  the report against the checklist generated in Stage 1.

Why this beats the standard "judge with built-in rubric":
  - Removes rubric drift (judge can't tailor criteria to the candidate).
  - Auditable: you can inspect the checklist before scoring.
  - Reproducible: re-running scoring with the same checklist is stable.

Cost: 2 LLM calls per evaluation if there's one item, ~6-11 typical
(1 checklist call + 1 scoring call per item). Worth it.

The schema types (Checklist, CheckItemScore, ACEResult) live in
`src/state.py` so `RunMetrics.ace` can reference them without an
eval→src import cycle.
"""
from __future__ import annotations

from src.config import Config
from src.llm import chat_json
from src.state import ACEResult, Checklist, CheckItemScore, ResearchRun


_CHECKLIST_SYSTEM = """You generate a CHECKLIST of criteria for evaluating a research report.

You will see ONLY the research prompt — NOT the report being evaluated.

Produce 5-10 criteria a strong answer would meet. Each criterion must be:
- Specific to this prompt (not generic "is well-written")
- Measurable from the report alone (a reader could check yes/no/partial)
- Independent of any particular report's structure
- Substantive (about content/correctness/coverage), not stylistic

Return JSON: {"items": ["criterion 1", "criterion 2", ...]}"""


_SCORING_SYSTEM = """You score a research report against a pre-defined checklist.

You did NOT generate the checklist. You see only:
- The user prompt
- The checklist (already finalized)
- The candidate report

For each checklist item, decide:
- met: true if the report clearly meets this criterion
- score: 0.0–1.0 partial credit (0=absent, 0.5=partially met, 1=fully met)
- reason: one short sentence

Be strict. A report that only gestures at a criterion gets 0.3, not 0.7."""


def ace_score(cfg: Config, rr: ResearchRun) -> ACEResult:
    """Run the two-stage ACE protocol over a completed ResearchRun."""
    if not rr.report:
        return ACEResult(
            checklist=Checklist(items=[]), item_scores=[], overall=0.0,
            notes="no report",
        )

    # Stage 1: generate checklist from prompt only (report HIDDEN)
    checklist = chat_json(
        cfg.anthropic_api_key, cfg.judge_model, _CHECKLIST_SYSTEM,
        f"PROMPT:\n\n{rr.user_prompt}\n\nGenerate the evaluation checklist.",
        Checklist, max_retries=1,
    )

    # Stage 2: score the report against the checklist
    report_text = _flatten(rr)
    items_block = "\n".join(f"  {i+1}. {item}" for i, item in enumerate(checklist.items))
    item_scores: list[CheckItemScore] = []
    for item in checklist.items:
        try:
            score = chat_json(
                cfg.anthropic_api_key, cfg.judge_model, _SCORING_SYSTEM,
                f"PROMPT:\n{rr.user_prompt}\n\nCHECKLIST:\n{items_block}\n\n"
                f"REPORT:\n{report_text}\n\nScore CRITERION:\n{item}",
                CheckItemScore, max_retries=1,
            )
            item_scores.append(score)
        except Exception as e:
            item_scores.append(CheckItemScore(
                item=item, met=False, score=0.0,
                reason=f"scoring error: {e}",
            ))

    overall = sum(s.score for s in item_scores) / max(1, len(item_scores))
    n_met = sum(1 for s in item_scores if s.met)
    notes = f"{n_met}/{len(item_scores)} criteria fully met"
    return ACEResult(
        checklist=checklist, item_scores=item_scores,
        overall=overall, notes=notes,
    )


def _flatten(rr: ResearchRun) -> str:
    """Flatten ResearchRun.report to prose for the scoring prompt."""
    if not rr.report:
        return ""
    lines = [rr.report.summary, ""]
    for c in rr.report.claims:
        lines.append(f"- {c.claim} (confidence: {c.confidence:.2f})")
    if rr.report.contradictions_surfaced:
        lines.append("\nContradictions:")
        for x in rr.report.contradictions_surfaced:
            lines.append(f"- {x.description}")
    if rr.report.caveats:
        lines.append("\nCaveats:")
        for x in rr.report.caveats:
            lines.append(f"- {x}")
    return "\n".join(lines)
