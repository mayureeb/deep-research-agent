"""Curated benchmark prompts spanning four difficulty modes.

Eight prompts, two per category (easy, contradictory, sparse, multimodal),
each with a 4-5 bullet rubric used by `eval/metrics/judge.py`.
"""
from __future__ import annotations

from pydantic import BaseModel


class BenchmarkPrompt(BaseModel):
    """One prompt + its category + rubric.

    Attributes:
        id: Stable short identifier (e.g. "easy-1").
        category: One of easy | contradictory | sparse | multimodal.
        prompt: The user prompt the system sees.
        rubric: Bullet criteria a good report should hit; visible to the LLM judge.
    """
    id: str
    category: str           # easy | contradictory | sparse | multimodal
    prompt: str
    rubric: list[str]       # bullet criteria a good report should hit


# The 8-prompt benchmark. Keep ids unique so --only flags work.
PROMPTS: list[BenchmarkPrompt] = [
    # ---- Easy: factual, single-domain ----
    BenchmarkPrompt(
        id="easy-1",
        category="easy",
        prompt=(
            "What are the real-world risks and benefits of using synthetic data "
            "to train or fine-tune large language models? Focus on data quality, "
            "bias, and evaluation."
        ),
        rubric=[
            "Distinguishes synthetic data generation methods (distillation, augmentation, fully synthetic)",
            "Names specific risks (model collapse, distribution shift, bias amplification)",
            "Names specific benefits (cost, scale, privacy, controllability)",
            "Cites at least one empirical study (not just opinion pieces)",
            "Acknowledges where evidence is preliminary",
        ],
    ),
    BenchmarkPrompt(
        id="easy-2",
        category="easy",
        prompt=(
            "What are the main approaches to retrieval-augmented generation (RAG) "
            "for question answering, and how do they compare on accuracy and latency?"
        ),
        rubric=[
            "Distinguishes naive RAG, hybrid (vector+BM25), re-ranking, query rewriting",
            "Reports specific accuracy numbers from at least one benchmark",
            "Notes latency tradeoffs",
            "Acknowledges the lost-in-the-middle problem or similar known failure modes",
        ],
    ),

    # ---- Contradictory: literature disagrees ----
    BenchmarkPrompt(
        id="contradictory-1",
        category="contradictory",
        prompt=(
            "Is chain-of-thought prompting an effective reasoning strategy for LLMs, "
            "or does it primarily improve output formatting? The literature disagrees — "
            "find the real fault lines and explain what accounts for the conflicting results."
        ),
        rubric=[
            "Cites both pro-CoT (e.g., Wei et al. 2022) and skeptical (e.g., Sprague et al. 2024) papers",
            "Identifies the actual fault line (e.g., math/symbolic vs. general tasks)",
            "Does NOT pick a winner without evidence",
            "Acknowledges that 'effective' depends on task and model size",
        ],
    ),
    BenchmarkPrompt(
        id="contradictory-2",
        category="contradictory",
        prompt=(
            "Do larger LLMs reliably outperform smaller fine-tuned models on "
            "domain-specific tasks, or is the picture more mixed than scaling-laws "
            "advocates suggest?"
        ),
        rubric=[
            "Acknowledges scaling laws (Kaplan, Chinchilla) and where they apply",
            "Cites cases where smaller fine-tuned models match or beat larger general ones",
            "Distinguishes by task type (knowledge vs. reasoning vs. specialized)",
            "Notes inference cost as a factor that complicates simple comparisons",
        ],
    ),

    # ---- Sparse: emerging, thin literature ----
    BenchmarkPrompt(
        id="sparse-1",
        category="sparse",
        prompt=(
            "What is the current state of inference-time compute scaling for LLM "
            "reasoning? Separate what has been empirically validated from what is "
            "still speculative, and identify where the evidence is too thin to draw conclusions."
        ),
        rubric=[
            "Discusses test-time compute methods (best-of-N, MCTS, iterative refinement, o1-style)",
            "Explicitly separates validated (e.g., best-of-N gains) from speculative",
            "Names where evidence is thin (e.g., generalization across domains)",
            "Cites at least one primary paper, not just secondary commentary",
        ],
    ),
    BenchmarkPrompt(
        id="sparse-2",
        category="sparse",
        prompt=(
            "What do we actually know about whether process reward models improve "
            "LLM reasoning more than outcome-only reward models?"
        ),
        rubric=[
            "Names PRMs vs. ORMs distinction",
            "Cites empirical comparisons (e.g., Lightman et al. or similar)",
            "Acknowledges labeling cost tradeoffs",
            "Honest about limited number of head-to-head studies",
        ],
    ),

    # ---- Multi-modal: structured/visual output expected ----
    BenchmarkPrompt(
        id="multimodal-1",
        category="multimodal",
        prompt=(
            "Map the research landscape of multi-agent LLM systems. Produce a "
            "structured report that includes a taxonomy of the major architectural "
            "patterns, how they relate, and where the open problems are."
        ),
        rubric=[
            "Names major patterns (orchestrator+workers, debate, pipeline, swarm)",
            "Describes how patterns relate (e.g., debate as a special case of multi-agent)",
            "Cites real systems (Anthropic research, AutoGen, CrewAI, Cognition's stance)",
            "Identifies open problems (coordination, eval, lossy communication)",
        ],
    ),
    BenchmarkPrompt(
        id="multimodal-2",
        category="multimodal",
        prompt=(
            "Compare the architectural choices made by major deep-research products "
            "(Anthropic, OpenAI, Perplexity, Google) and identify what they have in "
            "common and where they meaningfully diverge."
        ),
        rubric=[
            "Names at least 3 of the 4 systems specifically",
            "Identifies common pattern (decompose -> parallel research -> synthesize)",
            "Surfaces real divergences (e.g., depth of critic, source weighting)",
            "Honest about which claims are inferred vs. publicly documented",
        ],
    ),
]
