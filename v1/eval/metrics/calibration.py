"""Calibration: does stated confidence track actual grounding?

We bucket claims by stated confidence and compute the fraction grounded
in each bucket. A WELL-CALIBRATED agent matches stated confidence to
actual correctness rate:
  * 0.9-1.0 confidence claims → ~90% actually grounded
  * 0.6-0.8 confidence claims → ~70% actually grounded
  * Etc.

A POORLY-CALIBRATED agent:
  * High confidence everywhere → all claims at 0.9+ regardless of
    evidence quality (model puffery).
  * Random confidence → no relationship between stated and actual.
  * Anti-calibrated → high confidence on weakly-grounded claims.

Why bucket (rather than single-number slope): the curve shows the SHAPE
of mis-calibration. Aggregates.calibration_slope gives the one-number
summary; this gives the diagnostic.

Bucket thresholds:
  [0.0, 0.3) — "low confidence"
  [0.3, 0.6) — "medium confidence"
  [0.6, 0.8) — "high confidence"
  [0.8, 1.01) — "very high confidence" (1.01 is a rounding-friendly upper bound)

Output is a list of bucket dicts ready to plot or print:
  buckets: [
    {range: [lo, hi], n: count, grounded_rate: float, mean_stated_confidence: float},
    ...
  ]
  n_claims: total

Empty buckets get n=0 and None for the rates. Don't reduce/aggregate over
None; consumers must handle the None case explicitly.

Cost: this metric does NOT make its own LLM calls — it consumes the
output of grounding_rate(). One grounding call per claim, then pure
Python bucketing.
"""
from __future__ import annotations

from src.state import Report, FindingsStore
from .grounding import grounding_rate


# Bucket bounds. Lower-inclusive, upper-exclusive (except the last bucket
# which uses 1.01 as the upper bound to include claims with confidence=1.0).
BUCKETS = [(0.0, 0.3), (0.3, 0.6), (0.6, 0.8), (0.8, 1.01)]


def calibration_curve(cfg, report: Report, findings: FindingsStore) -> dict:
    """Compute the calibration curve for a Report.

    Args:
        cfg: Config (passed through to grounding_rate, which uses the
            judge model).
        report: The Report to analyze.
        findings: The FindingsStore (needed by grounding_rate).

    Returns:
        Dict with `buckets` (list of bucket dicts) and `n_claims` (total).
    """
    # Run the grounding judge once per claim. We get the {claim_index ->
    # grounded} mapping from the details list.
    grounding = grounding_rate(cfg, report, findings)
    grounded_by_idx = {d["claim_index"]: d["grounded"] for d in grounding["details"]}

    buckets = []
    for lo, hi in BUCKETS:
        # Find claims whose confidence falls in [lo, hi).
        in_bucket = [
            (i, c) for i, c in enumerate(report.claims)
            if lo <= c.confidence < hi
        ]
        if not in_bucket:
            # Empty bucket: n=0, rates=None. Plotters / printers must
            # handle None (don't aggregate over it).
            buckets.append({
                "range": [lo, hi], "n": 0, "grounded_rate": None,
                "mean_stated_confidence": None,
            })
            continue
        n = len(in_bucket)
        # Count how many of the bucket's claims the judge said are grounded.
        grounded = sum(1 for i, _ in in_bucket if grounded_by_idx.get(i, False))
        # Mean stated confidence in this bucket — a sanity check. For a
        # well-spread agent, mean(bucket) ≈ midpoint of the bucket. If
        # all claims in bucket cluster at the bottom, the bucket boundaries
        # may be wrong for this run.
        mean_conf = sum(c.confidence for _, c in in_bucket) / n
        buckets.append({
            "range": [lo, hi],
            "n": n,
            "grounded_rate": grounded / n,
            "mean_stated_confidence": mean_conf,
        })

    return {"buckets": buckets, "n_claims": len(report.claims)}
