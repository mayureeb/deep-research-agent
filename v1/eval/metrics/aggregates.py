"""Tradeoff-aware aggregate metrics.

Two functions, both deliberately small:

  f_score(grounding, coverage, beta) — harmonic-mean-style aggregate that
    penalizes systems gaming one metric at the expense of the other.
    Same shape as classical F-score over precision/recall.

  calibration_slope(report, grounding_details) — slope of the linear fit
    between stated confidence and actual-grounded (0/1). Slope ≈ 1 means
    confidence labels track reality; slope ≈ 0 means labels are
    decoration. Also returns the std of stated confidences as a sanity
    check — if the model collapsed to a single value, the slope is
    meaningless and the spread tells you so.

  fmt_slope(slope_dict) — formatter for the slope output, used in eval
    summary tables.

These live in `aggregates.py` rather than next to the underlying metrics
because they COMPOSE other metrics (f_score takes grounding + coverage,
calibration_slope takes report + grounding_details). Keeping the
composition in one file makes the eval pipeline easier to follow.

Mathematical notes:
  * F-score with beta=1 is the harmonic mean. We use it because both
    grounding and coverage are bounded [0,1] and a system that scores
    100% on one and 0% on the other is useless — the harmonic mean
    correctly returns 0.
  * calibration_slope uses simple least-squares (no scipy dependency).
    For ~5-15 claims per report, this is fine.
"""
from __future__ import annotations

import math
from typing import Iterable

from src.state import Report


def f_score(grounding: float, coverage: float, beta: float = 1.0) -> float:
    """Harmonic-mean-style aggregate of grounding and coverage.

    Args:
        grounding: 0.0-1.0. The grounding rate.
        coverage: 0.0-1.0. The coverage rate.
        beta: Weighting parameter.
            beta = 1.0 → equal weight (classical F1).
            beta < 1   → weights grounding higher (precision-leaning).
            beta > 1   → weights coverage higher (recall-leaning).

    Returns:
        F-score in [0, 1]. Returns 0.0 if either input is ≤0 (degenerate).
    """
    # Defensive: zero or negative inputs collapse F to 0. Avoids
    # divide-by-zero on the standard formula.
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
        report: The Report whose claims supply the x-values (confidence).
        grounding_details: The details list from grounding_rate(). Each
            dict has `claim_index` and `grounded` keys.

    Returns:
        Dict with:
            slope:        the fitted b. ≈ 1 well-calibrated; ≈ 0 meaningless
                          labels; < 0 anti-correlated (worse than no labels).
            spread:       std-dev of stated confidences across claims. If
                          this is ~0 the model collapsed to one value and
                          the slope can't be interpreted.
            n:            number of claims used in the fit.
            interpretable: True only if n ≥ 3 and spread > 0.05.

    Why we return interpretable=False instead of None: explicit tri-state
    (interpretable / not-enough-data / collapsed) is clearer than
    overloading None.
    """
    # Empty report case.
    if not report or not report.claims:
        return {"slope": None, "spread": 0.0, "n": 0, "interpretable": False}

    # Index grounded judgments by claim_index (so we can pair with
    # confidences in claim order).
    grounded_by_idx = {d["claim_index"]: bool(d["grounded"])
                       for d in grounding_details}
    pairs: list[tuple[float, float]] = []
    for ci, c in enumerate(report.claims):
        if ci in grounded_by_idx:
            pairs.append((c.confidence, 1.0 if grounded_by_idx[ci] else 0.0))

    n = len(pairs)
    # Need ≥3 to fit a line. With n=2, slope is just (y1-y0)/(x1-x0) which
    # is too noisy to be meaningful.
    if n < 3:
        return {"slope": None, "spread": 0.0, "n": n, "interpretable": False}

    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n

    # Standard least-squares slope. Numerator: covariance(x,y); denominator:
    # variance(x). The intercept a = y_mean - b * x_mean isn't computed
    # here — we only care about the slope for calibration interpretation.
    num = sum((x - x_mean) * (y - y_mean) for x, y in pairs)
    den = sum((x - x_mean) ** 2 for x in xs)
    spread = math.sqrt(sum((x - x_mean) ** 2 for x in xs) / n)

    # Two failure modes for slope interpretation:
    #   1. den == 0: zero variance in confidences (all claims at same value).
    #   2. spread < 0.05: confidences are too clustered to fit reliably.
    # Both → interpretable=False so consumers can render "flat".
    if den == 0 or spread < 0.05:
        return {"slope": None, "spread": spread, "n": n, "interpretable": False}

    slope = num / den
    return {"slope": slope, "spread": spread, "n": n, "interpretable": True}


def fmt_slope(slope_dict: dict) -> str:
    """Human-readable slope cell for the comparison tables.

    Returns:
      "n=N"            — when there weren't enough claims (n < 3).
      "flat(σ=0.04)"   — when all claims clustered (spread < 0.05).
      "+0.45"          — when interpretable. Sign-prefixed so anti-
                         calibration (negative slope) is obvious.
    """
    if not slope_dict["interpretable"]:
        if slope_dict["n"] < 3:
            return f"n={slope_dict['n']}"
        return f"flat(σ={slope_dict['spread']:.2f})"
    return f"{slope_dict['slope']:+.2f}"
