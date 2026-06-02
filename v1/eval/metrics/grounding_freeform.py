"""Grounding metric for free-form (baseline) reports.

This is the parallel of `grounding.py` but for the baseline pipeline,
which doesn't produce structured per-claim citations.

Pipeline:
  1. Extract atomic claims from the report text (claim_extraction.py).
  2. For each claim, check whether ANY of the gathered source texts
     supports it. (Baseline doesn't have per-claim citations, so we
     check against the UNION of all sources the agent saw.)
  3. Return the fraction of claims that pass.

Why pool all sources (rather than per-claim attribution): baseline
doesn't tell us which claim came from which source. Pooling treats the
baseline as charitably as possible — if any source the baseline read
supports the claim, we count it as grounded.

This is the SAME judgment task as the structured grounding metric —
SAME judge prompt (imported from grounding.py), SAME judge model. The
only difference is the evidence surface (all sources rather than
per-claim cites). That makes the rates DIRECTLY COMPARABLE in
baseline_compare.py — apples to apples on the metric, even though the
two systems produce very different report shapes.

Cost: one extraction LLM call (claim_extraction) + one judge call per
claim. With 6-15 claims per baseline report, that's ~7-16 calls per
baseline pipeline run — comparable to the structured side's per-claim
cost.

Edge case: MAX_EVIDENCE_CHARS cap.
  Pooling all source text can hit the judge's context limit on rich
  prompts (e.g., 5 sub-questions × 2-3 sources × 12K chars = 120K+
  chars). We cap at 8K chars per claim. The cap is per-CLAIM not
  per-EVIDENCE, but since we send the whole pool with each claim, the
  cap is effectively on the pool. v1 could be smarter (rank sources by
  relevance to the claim) but v0 keeps it simple.
"""
from __future__ import annotations

from src.config import Config
from src.llm import chat_json
from src.state import BaselineRun
from .claim_extraction import extract_claims
from .grounding import GroundingJudgment, SYSTEM as GROUNDING_SYSTEM


# Cap evidence text per claim — judge model has its own context limits, and
# scanning 50K tokens of raw page text per claim is wasteful. 8K chars ≈
# 2K tokens, leaves plenty of headroom for the judge's response.
MAX_EVIDENCE_CHARS = 8000


def grounding_rate_freeform(cfg: Config, br: BaselineRun) -> dict:
    """Compute grounding rate for a free-form (baseline) report.

    Args:
        cfg: Config (uses judge_model).
        br: The BaselineRun to grade. Must have populated
            br.report.summaries[].raw_source_texts (captured in
            agent/baseline.py during the researcher loop).

    Returns:
        Dict with rate (float), n_claims (int), details (list).

    Failure modes (return early with rate=0.0):
      * No claims extracted from the report (extract_claims returned []).
      * No raw_source_texts captured across any summary.
    """
    claims = extract_claims(cfg, br.report.final_report_text)
    if not claims:
        return {"rate": 0.0, "n_claims": 0, "details": []}

    # Pool all source texts into one searchable corpus for the judge.
    # We chunk by source so the judge can attribute, but pool for matching.
    pooled_evidence = "\n\n---\n\n".join(
        f"SOURCE: {s.subquestion_text}\n" + "\n".join(s.raw_source_texts)
        for s in br.report.summaries if s.raw_source_texts
    )
    # Hard cap on the pool. For sparse / single-source baselines this
    # never triggers; for rich runs it preserves the most-recent sources.
    pooled_evidence = pooled_evidence[:MAX_EVIDENCE_CHARS]

    if not pooled_evidence.strip():
        # Nothing to ground against. Render every claim as ungrounded
        # with the reason — useful in the details JSON for forensic
        # comparison ("the baseline produced X claims with no source
        # backing").
        return {
            "rate": 0.0, "n_claims": len(claims),
            "details": [{"claim": c, "grounded": False,
                         "reason": "no source text available"} for c in claims],
        }

    grounded = 0
    details = []
    for c in claims:
        try:
            judgment = chat_json(
                cfg.anthropic_api_key, cfg.judge_model, GROUNDING_SYSTEM,
                f"CLAIM:\n{c}\n\nEVIDENCE:\n{pooled_evidence}",
                GroundingJudgment, max_retries=1,
            )
            if judgment.grounded:
                grounded += 1
            details.append({
                "claim": c, "grounded": judgment.grounded,
                "reason": judgment.reason,
            })
        except Exception as e:
            # Same swallow-exceptions pattern as grounding.py — don't
            # crash on one judge hiccup.
            details.append({
                "claim": c, "grounded": False, "reason": f"judge error: {e}",
            })

    return {"rate": grounded / len(claims), "n_claims": len(claims), "details": details}
