"""eval — the v0 evaluation suite.

Top-level CLIs (each is `python -m eval.<name>`):
  * run_eval.py         — full eval suite per prompt (or single prompt;
                          or 3-run reproducibility mode).
  * ablation.py         — critic ON vs OFF ablation.
  * baseline_compare.py — v0 vs GPT-Researcher-style baseline (head-to-head).

Curated benchmark prompts live in `prompts.py` (8 prompts, 4 categories ×
2 each). Each prompt has a per-prompt rubric used by the LLM judge.

Sub-package `metrics/` contains the eight metric implementations — see
metrics/__init__.py for the per-metric breakdown.
"""
