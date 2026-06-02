"""Grounding metric for free-form (baseline) reports.

Extract atomic claims from the report text, then for each claim ask the
judge model whether any of the pooled source text supports it. Same
judge prompt and model as grounding.py so rates are directly comparable
in baseline_compare.py.
"""
from __future__ import annotations

from src.config import Config
from src.llm import chat_json
from src.state import BaselineRun
from .claim_extraction import extract_claims
from .grounding import GroundingJudgment, SYSTEM as GROUNDING_SYSTEM


# Cap pooled evidence to keep within judge context.
MAX_EVIDENCE_CHARS = 8000


def grounding_rate_freeform(cfg: Config, br: BaselineRun) -> dict:
    """Compute grounding rate for a free-form report.

    Args:
        cfg: Config; uses judge_model.
        br: The BaselineRun. Must have populated raw_source_texts.

    Returns:
        Dict with rate, n_claims, details.
    """
    claims = extract_claims(cfg, br.report.final_report_text)
    if not claims:
        return {"rate": 0.0, "n_claims": 0, "details": []}

    # Chunk by source for attribution; pool for matching.
    pooled_evidence = "\n\n---\n\n".join(
        f"SOURCE: {s.subquestion_text}\n" + "\n".join(s.raw_source_texts)
        for s in br.report.summaries if s.raw_source_texts
    )
    pooled_evidence = pooled_evidence[:MAX_EVIDENCE_CHARS]

    if not pooled_evidence.strip():
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
            # Same swallow-exception pattern as grounding.py.
            details.append({
                "claim": c, "grounded": False, "reason": f"judge error: {e}",
            })

    return {"rate": grounded / len(claims), "n_claims": len(claims), "details": details}
