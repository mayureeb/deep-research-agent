"""Citation grounding rate — the most important quality metric in v0.

For each ReportClaim:
  * Look up its supporting_finding_indices.
  * Resolve those to the actual evidence quotes from the FindingsStore.
  * Ask the JUDGE model: "does this evidence support this claim?"
  * Mark the claim grounded / ungrounded.

The fraction of grounded claims is the grounding rate.

Why an LLM judge (rather than exact substring matching):
  * Substring matching is too brittle for paraphrase. The writer often
    paraphrases findings into smoother prose; substring would say
    "ungrounded" on every paraphrased claim.
  * The judge is asked to be STRICT on overstatement ("most studies show X"
    is NOT grounded by "one study showed X"). This catches the
    most-common writer-failure mode (claim stronger than evidence).

Why JUDGE_MODEL (not the same model as the writer):
  * Same-model evaluation inflates scores via shared bias. Using a
    DIFFERENT/STRONGER model as judge gives an honest signal.
  * v0 default: writer = Sonnet, judge = Opus. This is the most common
    LLM-as-judge setup in research literature.

What this DOES NOT catch (the headline gap that v1's re-fetch verifier closes):
  * Researcher fabrication. If a researcher made up a quote and attributed
    it to a real URL, the writer cites that quote in support of a claim,
    and this metric checks whether the (fabricated) quote supports the
    claim — both pieces of text are agreed, so grounding "passes". The
    fabrication only shows when someone re-fetches the source page.

Returns:
  {
    "rate": float,
    "details": [
      {"claim_index": int, "grounded": bool, "reason": str},
      ...
    ]
  }

The details list is consumed by calibration.py (to bucket by stated
confidence) and by aggregates.calibration_slope (to fit slope of
confidence-vs-grounded). Keep the shape stable — both downstream consumers
key on `claim_index`.
"""
from __future__ import annotations

from pydantic import BaseModel

from src.config import Config
from src.llm import chat_json
from src.state import Report, FindingsStore


class GroundingJudgment(BaseModel):
    """One per-claim judgment from the judge LLM. Pydantic-enforced shape
    so we can rely on `grounded: bool` (not "yes"/"no"/"maybe" strings)."""
    grounded: bool
    reason: str


# Judge prompt. The four NOT-grounded clauses are exhaustive for what we
# want the judge to catch:
#   * Different topic — pure non sequitur.
#   * Weaker version — overstatement (the most common failure).
#   * Opposite — flipped polarity.
#   * No evidence — empty cite block.
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
        cfg: Config (uses judge_model — the strong model).
        report: The Report to grade.
        findings: The full FindingsStore — we resolve
            supporting_finding_indices to actual evidence quotes here.

    Returns:
        rate: Fraction of claims grounded (0.0 to 1.0).
        details: One dict per claim. Always present; per-claim entries
            include `claim_index` and `grounded` always, and `reason`.

    Cost: one judge LLM call per claim. With 5-10 claims per report and
    8 prompts in the eval suite, this is ~50-80 judge calls per full
    eval run.
    """
    if not report.claims:
        # Empty report → 0.0 rate (rather than raising). Lets the eval
        # row render even if the writer produced nothing.
        return {"rate": 0.0, "details": []}

    details = []
    grounded_count = 0

    for ci, claim in enumerate(report.claims):
        # Short-circuit: empty supporting_finding_indices is automatically
        # ungrounded (no evidence to check against). This is the same
        # condition the v0 critic flags as "missing_citation".
        if not claim.supporting_finding_indices:
            details.append({"claim_index": ci, "grounded": False,
                            "reason": "no supporting findings cited"})
            continue

        # Resolve cited indices to evidence quotes.
        evidence_blocks = []
        for fi in claim.supporting_finding_indices:
            if 0 <= fi < len(findings.findings):
                evidence_blocks.append(f'"{findings.findings[fi].evidence}"')

        if not evidence_blocks:
            # All cited indices were out of range. Same as missing_citation
            # but with a more specific reason.
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
            # Don't crash the whole eval on one judge hiccup. Treat the
            # claim as ungrounded with the error in `reason` so it shows
            # up in the details. The numerator is unaffected by errors —
            # only successful "grounded" judgments count.
            details.append({"claim_index": ci, "grounded": False,
                            "reason": f"judge error: {e}"})

    return {"rate": grounded_count / len(report.claims), "details": details}
