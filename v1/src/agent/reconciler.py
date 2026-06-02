"""Contradiction detection. Surfaces disagreements without picking winners.

One LLM call per run; skipped when there are fewer than 2 findings.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..config import Config
from ..llm import chat_json
from ..state import Contradiction, Decomposition, FindingsStore


class ContradictionList(BaseModel):
    """Schema for the reconciler's structured output."""
    contradictions: list[Contradiction] = Field(
        default_factory=list,
        description=(
            "Typed contradiction objects. Each: a description paragraph, "
            "finding_ids of the conflicting findings (integers from the "
            "numbered findings list above), severity, and type."
        ),
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

For each genuine contradiction, return a Contradiction object with these fields:
- description: ONE paragraph that names what the findings disagree about, quotes the conflicting evidence from each, and lists the source URLs.
- finding_ids: the integer indices (from the numbered findings list shown to you) of the findings in conflict. At least 2 indices.
- severity:
    * "major"    — the disagreement is central to the user's question; the report must lead with it.
    * "moderate" — the disagreement affects one sub-question's answer.
    * "minor"    — narrow technical disagreement that doesn't change the bottom line.
- type:
    * "factual"        — findings claim different facts.
    * "methodological" — same fact, different methods that lead to disagreement.
    * "framing"        — same data, different interpretive framings (typically lower-severity).
    * "temporal"       — claims about different time periods that look like disagreement but aren't really.
    * "other"          — escape hatch.

Do NOT pick a winner. Just describe the disagreement.

If there are no contradictions, return an empty list."""


def detect_and_surface_contradictions(
    cfg: Config,
    decomp: Decomposition,
    findings: FindingsStore,
) -> list[Contradiction]:
    """Run the reconciler on the findings pool. Returns typed Contradictions."""
    if len(findings.findings) < 2:
        return []

    findings_text = "\n\n".join(
        f"[{i}] sub-question: {f.subquestion_id}\n"
        f"    claim: {f.claim}\n"
        f"    evidence: {f.evidence}\n"
        f"    source: {f.source_url}"
        for i, f in enumerate(findings.findings)
    )

    user = (
        f"Original research prompt: {decomp.subquestions[0].rationale[:200] or '(see findings)'}\n\n"
        f"Findings to check (numbered — use these indices in Contradiction.finding_ids):\n\n"
        f"{findings_text}\n\n"
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
        n = len(findings.findings)
        return [
            c for c in result.contradictions
            if len(c.finding_ids) >= 2
            and all(0 <= fi < n for fi in c.finding_ids)
        ]
    except Exception:
        return []
