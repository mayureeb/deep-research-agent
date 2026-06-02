"""eval.metrics — the eight metric implementations.

Per-claim grounding (LLM-as-judge):
  * grounding.py            — per-claim grounding rate for the structured
                              v0 system. THE most important quality metric.
  * grounding_freeform.py   — same judgment task but for free-form
                              baseline reports (uses claim_extraction).
  * claim_extraction.py     — extract atomic claims from baseline prose.

Coverage / structure:
  * coverage.py             — % sub-questions with ≥1 finding (pure Python).

Calibration:
  * calibration.py          — bucketed confidence-vs-grounded curve.
  * aggregates.py           — f_score (harmonic mean) + calibration_slope
                              (linear fit). Composes the underlying metrics.

Quality / stability:
  * judge.py                — LLM-as-judge with structured rubric (5 dims).
  * reproducibility.py      — claim-Jaccard across 3 runs of the same prompt.

The judge_model (Opus by default) is the strong model; use it for any
metric that judges quality. The structured side and the baseline side
share the SAME judge prompts and SAME judge model so cross-system
comparisons are apples-to-apples.
"""
