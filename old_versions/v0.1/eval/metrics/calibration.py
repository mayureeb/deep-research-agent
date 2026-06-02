"""Calibration: does stated confidence track actual grounding?

Buckets claims by stated confidence and computes the fraction grounded
in each bucket. Empty buckets get n=0 and None rates; consumers must
handle None.

Output: {"buckets": [{range, n, grounded_rate, mean_stated_confidence}], "n_claims": int}.

Consumes grounding_rate() output (one judge call per claim) plus pure
Python bucketing.
"""
from __future__ import annotations

from src.state import Report, FindingsStore
from .grounding import grounding_rate


# Lower-inclusive, upper-exclusive (last bucket uses 1.01 to include 1.0).
BUCKETS = [(0.0, 0.3), (0.3, 0.6), (0.6, 0.8), (0.8, 1.01)]


def calibration_curve(cfg, report: Report, findings: FindingsStore) -> dict:
    """Compute the calibration curve for a Report.

    Args:
        cfg: Config; passed to grounding_rate.
        report: The Report to analyze.
        findings: The FindingsStore.

    Returns:
        Dict with `buckets` and `n_claims`.
    """
    grounding = grounding_rate(cfg, report, findings)
    grounded_by_idx = {d["claim_index"]: d["grounded"] for d in grounding["details"]}

    buckets = []
    for lo, hi in BUCKETS:
        in_bucket = [
            (i, c) for i, c in enumerate(report.claims)
            if lo <= c.confidence < hi
        ]
        if not in_bucket:
            buckets.append({
                "range": [lo, hi], "n": 0, "grounded_rate": None,
                "mean_stated_confidence": None,
            })
            continue
        n = len(in_bucket)
        grounded = sum(1 for i, _ in in_bucket if grounded_by_idx.get(i, False))
        # Sanity check — mean(bucket) ≈ midpoint for a well-spread agent.
        mean_conf = sum(c.confidence for _, c in in_bucket) / n
        buckets.append({
            "range": [lo, hi],
            "n": n,
            "grounded_rate": grounded / n,
            "mean_stated_confidence": mean_conf,
        })

    return {"buckets": buckets, "n_claims": len(report.claims)}
