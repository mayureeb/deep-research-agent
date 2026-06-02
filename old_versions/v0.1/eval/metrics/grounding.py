"""Citation grounding rate.

For each ReportClaim, resolve its supporting_finding_indices to evidence
quotes and ask the judge model whether the evidence supports the claim.
The fraction of grounded claims is the grounding rate.

Returns:
  {"rate": float, "details": [{"claim_index": int, "grounded": bool, "reason": str}, ...]}

The details list is consumed by calibration.py and aggregates.calibration_slope;
both key on `claim_index`.
"""
from __future__ import annotations

from pydantic import BaseModel

from src.config import Config
from src.llm import chat_json
from src.state import Report, FindingsStore


class GroundingJudgment(BaseModel):
    """One per-claim judgment from the judge LLM."""
    grounded: bool
    reason: str


SYSTEM = """You decide whether a CLAIM is supported by EVIDENCE quotes.

A claim is GROUNDED if a fair reader of the evidence would agree the claim is what the evidence says (or directly implies).

A claim is NOT grounded if:
- The evidence is about a different topic
- The evidence supports a much weaker version of the claim
- The evidence supports the OPPOSITE of the claim
- There is no evidence

Be strict on overstatement. "Most studies show X" is NOT grounded by evidence saying "one study showed X".

Return JSON: {"grounded": bool, "reason": "<one short sentence>"}"""


def grounding_rate(cfg: Config, report: Report, findings: FindingsStore) -> dict:
    """Returns {"rate": float, "details": [per-claim judgments]}.

    Args:
        cfg: Config; uses judge_model.
        report: The Report to grade.
        findings: FindingsStore; supporting_finding_indices are resolved
            here to evidence quotes.

    Returns:
        rate: Fraction of claims grounded (0.0 to 1.0).
        details: Per-claim list with `claim_index`, `grounded`, `reason`.
    """
    if not report.claims:
        return {"rate": 0.0, "details": []}

    details = []
    grounded_count = 0

    for ci, claim in enumerate(report.claims):
        # Empty supporting_finding_indices is automatically ungrounded.
        if not claim.supporting_finding_indices:
            details.append({"claim_index": ci, "grounded": False,
                            "reason": "no supporting findings cited"})
            continue

        evidence_blocks = []
        for fi in claim.supporting_finding_indices:
            if 0 <= fi < len(findings.findings):
                evidence_blocks.append(f'"{findings.findings[fi].evidence}"')

        if not evidence_blocks:
            details.append({"claim_index": ci, "grounded": False,
                            "reason": "all cited indices invalid"})
            continue

        evidence_text = "\n\n".join(evidence_blocks)
        user = f"CLAIM:\n{claim.claim}\n\nEVIDENCE:\n{evidence_text}"

        try:
            judgment = chat_json(
                cfg.anthropic_api_key, cfg.judge_model, SYSTEM, user,
                GroundingJudgment, max_retries=1,
            )
            if judgment.grounded:
                grounded_count += 1
            details.append({"claim_index": ci, "grounded": judgment.grounded,
                            "reason": judgment.reason})
        except Exception as e:
            # Don't crash the eval on one judge hiccup; surface the error.
            details.append({"claim_index": ci, "grounded": False,
                            "reason": f"judge error: {e}"})

    return {"rate": grounded_count / len(report.claims), "details": details}
