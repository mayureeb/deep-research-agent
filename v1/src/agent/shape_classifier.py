"""Shape classifier — one LLM call up front that decides downstream routing.

Classifies the prompt into one of five shapes (PRE_STRUCTURED, CONTESTED,
SPARSE_EMERGING, DISCOVERY, GENERAL), which the orchestrator uses to
branch planner / topology / writer behavior.
"""
from __future__ import annotations

from .. import events
from ..config import Config
from ..llm import chat_json
from ..state import ClassifiedShape


SYSTEM = """You are classifying a research prompt into one of five shapes. The choice routes the rest of the pipeline (planner, topology, writer prompt), so be deliberate.

The five shapes:

PRE_STRUCTURED — the user has already specified the structure of the answer (a comparison axis list, an explicit field list, an enumeration target). Examples:
- "Compare React vs Vue on developer experience, performance, ecosystem"
- "List the pros and cons of monorepos"
- "What are React, Vue, and Svelte's bundle sizes?"

CONTESTED — the answer is known to be disputed in the literature, or the prompt explicitly probes a disagreement. Examples:
- "Is chain-of-thought prompting effective, or does it just improve formatting?"
- "Does intermittent fasting actually work, controlling for caloric restriction?"
- "Is GDP a good measure of welfare?"

SPARSE_EMERGING — the topic is recent / niche / has limited literature; the researcher will need to map what's out there before going deep. Examples:
- "What's the current state of AI evals beyond MMLU?"
- "Recent work on automated mechanistic interpretability"
- "Studies on LLM agent reliability in production"

DISCOVERY — there's no clear answer space yet; the user wants exploration to find what the question even is. Examples:
- "What are the open problems in alignment?"
- "What approaches exist for speeding up transformer inference?"
- "Survey the landscape of GPU programming abstractions"

GENERAL — none of the above. Default; use this when the prompt doesn't clearly fit one of the specialized shapes.

Decision rules:
- PRE_STRUCTURED takes precedence over CONTESTED if the user supplied structure even when the substance is contested ("Compare X vs Y" is PRE_STRUCTURED even if X-vs-Y is debated — preserve the user's framing).
- Use SPARSE_EMERGING when the topic exists but is thin; use DISCOVERY when the answer SPACE itself is what the user wants to find.
- When uncertain between two specialized shapes, prefer GENERAL — wrong specialization is worse than no specialization.

Output a ClassifiedShape with:
- shape: one of the five strings exactly.
- confidence: 0.0–1.0; how sure you are. Borderline cases → 0.5.
- rationale: ONE short sentence explaining the choice."""


def classify_shape(cfg: Config, user_prompt: str) -> ClassifiedShape:
    """Classify the user's prompt into one of the five shapes.

    Returns a GENERAL fallback on failure rather than raising.
    """
    try:
        result = chat_json(
            cfg.anthropic_api_key,
            cfg.writer_model,
            SYSTEM,
            f"Research prompt:\n\n{user_prompt}",
            ClassifiedShape,
            max_retries=1,
        )
        events.planner(
            f"shape: {result.shape} (conf {result.confidence:.2f}) — {result.rationale}"
        )
        return result
    except Exception as e:
        events.planner(f"shape classifier failed ({e}); defaulting to GENERAL")
        return ClassifiedShape(
            shape="GENERAL",
            confidence=0.0,
            rationale=f"classifier failed; default GENERAL ({e})",
        )


def planner_annex(shape: ClassifiedShape | None) -> str:
    """Return the shape-specific guidance to append to DECOMPOSE_SYSTEM.

    Empty string for None / GENERAL.
    """
    if shape is None or shape.shape == "GENERAL":
        return ""
    if shape.shape == "PRE_STRUCTURED":
        return (
            "\n\nSHAPE-SPECIFIC GUIDANCE — PRE_STRUCTURED:\n"
            "The user has already supplied structure (a comparison axis list, "
            "an explicit field list, etc.). Your sub-questions MUST mirror "
            "the user's structure — one sub-question per axis/field they named, "
            "in the order they named them. Do NOT invent additional axes; "
            "preserving the user's framing is the goal."
        )
    if shape.shape == "CONTESTED":
        return (
            "\n\nSHAPE-SPECIFIC GUIDANCE — CONTESTED:\n"
            "The answer is known to be disputed. Include sub-questions that "
            "explicitly probe BOTH sides: e.g. 'What evidence supports X?' "
            "AND 'What evidence challenges X?'. Also include a sub-question "
            "on the methodological disagreement (why do studies differ?) "
            "when one is plausible."
        )
    if shape.shape == "SPARSE_EMERGING":
        return (
            "\n\nSHAPE-SPECIFIC GUIDANCE — SPARSE_EMERGING:\n"
            "Literature is thin. Include a sub-question on 'what evidence is "
            "missing or weak?' as a peer to the substantive sub-questions. "
            "Set preferred_search='search_papers' for any sub-question that "
            "would benefit from peer-reviewed sources (likely most of them); "
            "use 'web_search' for current-events / news angles."
        )
    if shape.shape == "DISCOVERY":
        return (
            "\n\nSHAPE-SPECIFIC GUIDANCE — DISCOVERY:\n"
            "The user wants to map the answer space, not deep-dive a known "
            "answer. Decompose into sub-questions that ENUMERATE candidate "
            "approaches / sub-areas / open problems, rather than questions "
            "that have a single answer. Each sub-question should be of the "
            "form 'What [approaches / open problems / categories] exist for X?'"
        )
    return ""
