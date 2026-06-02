"""Contradiction detection. Surfaces disagreements; does not pick winners.

Emits prose paragraphs (list[str]) — each names what the findings disagree
about, quotes the conflicting evidence, and lists the source URLs.

One LLM call per run; skipped entirely if there are <2 findings.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..config import Config
from ..llm import chat_json
from ..state import Decomposition, FindingsStore


class ContradictionList(BaseModel):
    """Schema for the reconciler's structured output."""
    contradictions: list[str] = Field(
        default_factory=list,
        description="Each entry: a one-paragraph description of a contradiction "
                    "between findings, naming the conflicting claims and sources.",
    )


SYSTEM = """You are checking a set of research findings for contradictions.

A contradiction is when two findings make CLAIMS THAT CANNOT BOTH BE TRUE about the same matter. Examples:
- Finding A says X improves Y; Finding B says X has no effect on Y.
- Finding A says the rate is 30%; Finding B says it's 70%.
- Finding A and B disagree on a methodological question.

NOT contradictions:
- Findings on different topics.
- Findings using different framings of the same fact.
- Findings at different levels of detail.

For each genuine contradiction you find, write ONE paragraph that:
- Names what the two findings disagree about
- Quotes the conflicting evidence from each
- Lists the source URLs

Do NOT pick a winner. Just describe the disagreement.

If there are no contradictions, return an empty list."""


def detect_and_surface_contradictions(
    cfg: Config,
    decomp: Decomposition,
    findings: FindingsStore,
) -> list[str]:
    """Run the reconciler on the findings pool.

    Args:
        cfg: Config; uses writer_model.
        decomp: Decomposition (used only for soft context).
        findings: The full FindingsStore.

    Returns:
        One contradiction paragraph per entry, or empty list if none found
        or the call failed.
    """
    if len(findings.findings) < 2:
        return []

    # All findings in one prompt so the model can spot chains, not just pairs.
    findings_text = "\n\n".join(
        f"[{i}] sub-question: {f.subquestion_id}\n"
        f"    claim: {f.claim}\n"
        f"    evidence: {f.evidence}\n"
        f"    source: {f.source_url}"
        for i, f in enumerate(findings.findings)
    )

    user = (
        f"Original research prompt: {decomp.subquestions[0].rationale[:200] or '(see findings)'}\n\n"
        f"Findings to check:\n\n{findings_text}\n\n"
        "List any contradictions between these findings."
    )

    try:
        result = chat_json(
            cfg.anthropic_api_key,
            cfg.writer_model,
            SYSTEM,
            user,
            ContradictionList,
            max_retries=1,
        )
        return result.contradictions
    except Exception:
        # Swallow all exceptions — contradiction detection is non-load-bearing.
        return []
