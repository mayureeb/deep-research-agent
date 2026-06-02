"""src.agent — agent components and the orchestrator.

Modules (in pipeline order):
  * orchestrator.py — owns the full control flow.
  * researcher.py   — per-sub-question tool-using loop.
  * reconciler.py   — contradiction detection.
  * writer.py       — structured Report synthesis.
  * critic.py       — grounding verification.
  * exporter.py     — post-critic Report -> markdown rendering.
  * baseline.py     — GPT-Researcher-style baseline pipeline.
  * confidence.py   — source-quality domain lookup.
"""
