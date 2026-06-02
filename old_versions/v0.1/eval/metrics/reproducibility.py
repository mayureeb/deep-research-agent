"""Reproducibility: pairwise Jaccard across multiple runs of the same prompt.

  * `claim_jaccard`: Jaccard over the set of claims per run — end-to-end
    output stability.
  * `subquestion_jaccard`: Jaccard over the set of planner sub-questions
    per run — plan stability, useful for diagnosing whether instability
    is upstream (planner) or downstream.

Both are pairwise to give a continuous signal and to catch single-run outliers.
"""
from __future__ import annotations

from src.state import ResearchRun


def claim_jaccard(runs: list[ResearchRun]) -> dict:
    """Pairwise Jaccard over claim text sets across runs.

    Args:
        runs: List of ResearchRuns of the same prompt.

    Returns:
        {"mean_jaccard": float, "pairs": [{i, j, jaccard}, ...]}.
        Empty / single-run input returns 0.0.
    """
    claim_sets = [
        {_normalize(c.claim) for c in (rr.report.claims if rr.report else [])}
        for rr in runs
    ]
    if len(claim_sets) < 2:
        return {"mean_jaccard": 0.0, "pairs": []}

    pairs = []
    for i in range(len(claim_sets)):
        for j in range(i + 1, len(claim_sets)):
            a, b = claim_sets[i], claim_sets[j]
            # Both empty → 1.0; one empty → 0.0.
            if not a and not b:
                jac = 1.0
            elif not a or not b:
                jac = 0.0
            else:
                jac = len(a & b) / len(a | b)
            pairs.append({"i": i, "j": j, "jaccard": jac})

    mean = sum(p["jaccard"] for p in pairs) / len(pairs)
    return {"mean_jaccard": mean, "pairs": pairs}


def subquestion_jaccard(runs: list[ResearchRun]) -> dict:
    """Pairwise Jaccard over planner sub-question text sets across runs.

    Same shape as claim_jaccard so the two are directly comparable on
    the same N runs.

    Args:
        runs: List of ResearchRuns of the same prompt.

    Returns:
        {"mean_jaccard": float, "pairs": [{i, j, jaccard}, ...]}.
    """
    sq_sets = [
        {_normalize(sq.question) for sq in rr.decomposition.subquestions}
        for rr in runs
    ]
    if len(sq_sets) < 2:
        return {"mean_jaccard": 0.0, "pairs": []}

    pairs = []
    for i in range(len(sq_sets)):
        for j in range(i + 1, len(sq_sets)):
            a, b = sq_sets[i], sq_sets[j]
            if not a and not b:
                jac = 1.0
            elif not a or not b:
                jac = 0.0
            else:
                jac = len(a & b) / len(a | b)
            pairs.append({"i": i, "j": j, "jaccard": jac})

    mean = sum(p["jaccard"] for p in pairs) / len(pairs)
    return {"mean_jaccard": mean, "pairs": pairs}


def _normalize(s: str) -> str:
    """Lowercase + collapse whitespace + cap at 200 chars."""
    return " ".join(s.lower().split())[:200]
