"""Aggregate metrics that compose grounding/coverage/confidence signals.

  f_score(grounding, coverage, beta) — harmonic-mean F-score.
  calibration_slope(report, grounding_details) — least-squares slope of
    confidence vs grounded (0/1), with a spread sanity check.
  fmt_slope(slope_dict) — human-readable cell for summary tables.
"""
from __future__ import annotations

import math
from typing import Iterable

from src.state import Report


def f_score(grounding: float, coverage: float, beta: float = 1.0) -> float:
    """Harmonic-mean F-score over grounding and coverage.

    Args:
        grounding: 0.0-1.0.
        coverage: 0.0-1.0.
        beta: Weighting parameter (1.0 = classical F1).

    Returns:
        F-score in [0, 1]; 0.0 on degenerate inputs.
    """
    if grounding <= 0 or coverage <= 0:
        return 0.0
    b2 = beta * beta
    return (1 + b2) * grounding * coverage / (b2 * grounding + coverage)


def calibration_slope(
    report: Report | None,
    grounding_details: Iterable[dict],
) -> dict:
    """Fit y = a + b*x where x = stated confidence, y = grounded (0/1).

    Args:
        report: The Report whose claims supply the x-values.
        grounding_details: details list from grounding_rate() with
            `claim_index` and `grounded` keys.

    Returns:
        Dict with slope, spread, n, interpretable.
        interpretable is True only if n ≥ 3 and spread > 0.05.
    """
    if not report or not report.claims:
        return {"slope": None, "spread": 0.0, "n": 0, "interpretable": False}

    grounded_by_idx = {d["claim_index"]: bool(d["grounded"])
                       for d in grounding_details}
    pairs: list[tuple[float, float]] = []
    for ci, c in enumerate(report.claims):
        if ci in grounded_by_idx:
            pairs.append((c.confidence, 1.0 if grounded_by_idx[ci] else 0.0))

    n = len(pairs)
    # n=2 slope is too noisy to be meaningful.
    if n < 3:
        return {"slope": None, "spread": 0.0, "n": n, "interpretable": False}

    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n

    # Least-squares slope; intercept isn't needed for calibration interpretation.
    num = sum((x - x_mean) * (y - y_mean) for x, y in pairs)
    den = sum((x - x_mean) ** 2 for x in xs)
    spread = math.sqrt(sum((x - x_mean) ** 2 for x in xs) / n)

    # interpretable=False on zero variance or too-clustered confidences.
    if den == 0 or spread < 0.05:
        return {"slope": None, "spread": spread, "n": n, "interpretable": False}

    slope = num / den
    return {"slope": slope, "spread": spread, "n": n, "interpretable": True}


def fmt_slope(slope_dict: dict) -> str:
    """Human-readable slope cell.

    Returns:
      "n=N"            — n < 3.
      "flat(σ=0.04)"   — spread < 0.05.
      "+0.45"          — interpretable; sign-prefixed.
    """
    if not slope_dict["interpretable"]:
        if slope_dict["n"] < 3:
            return f"n={slope_dict['n']}"
        return f"flat(σ={slope_dict['spread']:.2f})"
    return f"{slope_dict['slope']:+.2f}"
