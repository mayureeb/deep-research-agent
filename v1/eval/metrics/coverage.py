"""Coverage: % of decomposed sub-questions that have at least one finding.

The simplest metric in v0. Pure pure-Python — no LLM calls.

What this measures: did the system ACTUALLY research every sub-question
the planner spawned? A 100% coverage doesn't mean the research was good
(grounding is for that); it just means no sub-question was abandoned.

When coverage drops:
  * Researchers hit unproductive_call_limit and recorded UncertaintyNotes
    instead of Findings. The sub-question is "covered by uncertainty" but
    not "covered by findings" — coverage_rate counts findings only.
  * Researchers crashed (return_exceptions=True swallows them in the
    orchestrator). Forensic inspection: check the run JSON for missing
    sub-question ids in findings.

Used by:
  * eval/run_eval.py — primary coverage metric.
  * eval/baseline_compare.py — baseline coverage uses a different proxy
    (% sub-questions with non-empty summary), since baseline has no
    findings. The structured side uses this function.
  * src/agent/orchestrator.py — the runtime RunMetrics.coverage_rate
    uses the same formula (computed inline there, not via this module,
    to keep src/ free of eval/ imports).

Returns:
  {
    "rate": 0.0 to 1.0,
    "covered": [sorted sub-question ids with ≥1 finding],
    "missed": [sorted sub-question ids with 0 findings],
  }
"""
from __future__ import annotations

from src.state import ResearchRun


def coverage_rate(rr: ResearchRun) -> dict:
    """Compute coverage rate for one ResearchRun.

    Args:
        rr: The full ResearchRun. We need both decomposition (the
            denominator) and findings (the numerator).

    Returns:
        Dict with rate (float) and the covered/missed id lists. The id
        lists are sorted for stable output across runs.
    """
    # Set of sub-question ids that have ≥1 finding. Set semantics so
    # multiple findings on one sub-question count once.
    sq_with_findings = {f.subquestion_id for f in rr.findings.findings}
    sq_total = len(rr.decomposition.subquestions)
    # max(1, ...) defends against an empty decomposition (would be a bug
    # upstream, but we don't want to crash the eval over division-by-zero).
    rate = len(sq_with_findings) / max(1, sq_total)
    return {
        "rate": rate,
        "covered": sorted(sq_with_findings),
        "missed": sorted(
            sq.id for sq in rr.decomposition.subquestions
            if sq.id not in sq_with_findings
        ),
    }
