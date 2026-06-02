"""Coverage: fraction of decomposed sub-questions with at least one finding.

Returns {"rate": float, "covered": [ids], "missed": [ids]}.
"""
from __future__ import annotations

from src.state import ResearchRun


def coverage_rate(rr: ResearchRun) -> dict:
    """Compute coverage rate for one ResearchRun.

    Returns rate plus sorted covered/missed sub-question id lists.
    """
    sq_with_findings = {f.subquestion_id for f in rr.findings.findings}
    sq_total = len(rr.decomposition.subquestions)
    # max(1, ...) defends against an empty decomposition.
    rate = len(sq_with_findings) / max(1, sq_total)
    return {
        "rate": rate,
        "covered": sorted(sq_with_findings),
        "missed": sorted(
            sq.id for sq in rr.decomposition.subquestions
            if sq.id not in sq_with_findings
        ),
    }
