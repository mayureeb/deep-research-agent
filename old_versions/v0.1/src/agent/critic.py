"""Critic: verifies that every claim in the report is grounded in the findings.

Sees CLAIMS + EVIDENCE only — not the writer's reasoning or summary — so
verification stays independent of the writer's framing.

Issue types: ungrounded, overstated, missing_citation, source_mismatch.
"""
from __future__ import annotations

from ..config import Config
from ..llm import chat_json
from ..state import (
    CriticReport,
    Decomposition,
    FindingsStore,
    Report,
)


SYSTEM = """You are an independent fact-checker for a research report.

You do NOT see the writer's reasoning. You see only:
- Each report claim
- The findings the writer cited as supporting it (with their evidence quotes and sources)

For each claim, judge whether the cited findings ACTUALLY support it. Possible issue types:

- "ungrounded": none of the cited findings actually support this claim.
- "overstated": findings support a WEAKER version, but the claim makes a stronger assertion than the evidence justifies.
- "missing_citation": claim has supporting_finding_indices that don't exist or is empty.
- "source_mismatch": the cited finding's evidence quote doesn't match what the claim says (the writer mis-cited).

A claim is APPROVED if its evidence genuinely supports it. Be strict but fair:
- Don't penalize a claim for being narrower than the evidence allows.
- Don't penalize a claim that explicitly notes uncertainty when the evidence is uncertain.
- DO penalize claims that smooth over conflicting findings.
- DO penalize claims that paraphrase too aggressively (changing meaning, not just wording).

Output a CriticReport with:
- issues: list of CriticIssue objects (only for problematic claims)
- approved: true ONLY if there are no issues at all"""


def critique(
    cfg: Config,
    user_prompt: str,
    decomp: Decomposition,
    findings: FindingsStore,
    report: Report,
) -> CriticReport:
    """Run one critic pass on a Report.

    Args:
        cfg: Config; uses critic_model.
        user_prompt: Echoed for context only — the critic is told not to
            use it as a source of truth.
        decomp: Decomposition (currently unused inside critique).
        findings: FindingsStore. Used to render the full findings pool
            with indices and to resolve each claim's supporting_finding_indices.
        report: The Report being critiqued.
    """
    # Numbered findings — these indices MUST match what the writer used
    # in supporting_finding_indices.
    findings_text = []
    for i, f in enumerate(findings.findings):
        findings_text.append(
            f"[{i}] {f.claim}\n"
            f"    evidence: \"{f.evidence}\"\n"
            f"    source: {f.source_url}"
        )
    findings_block = "\n\n".join(findings_text)

    # Each claim with its cited findings inline; invalid indices flagged
    # so the LLM marks them as missing_citation.
    claims_text = []
    for ci, claim in enumerate(report.claims):
        cited = []
        for fi in claim.supporting_finding_indices:
            if 0 <= fi < len(findings.findings):
                f = findings.findings[fi]
                cited.append(f'  [{fi}] "{f.evidence}"  ({f.source_url})')
            else:
                cited.append(f"  [{fi}] *** INVALID INDEX ***")
        cited_block = "\n".join(cited) if cited else "  (no supporting findings cited)"
        claims_text.append(
            f"CLAIM #{ci} (writer's confidence={claim.confidence:.2f}):\n"
            f"  {claim.claim}\n"
            f"CITED FINDINGS:\n{cited_block}"
        )

    user = (
        f"USER PROMPT (for context only — do not use to judge claims):\n{user_prompt}\n\n"
        f"FINDINGS POOL:\n\n"
        f"{findings_block}\n\n"
        f"REPORT CLAIMS TO VERIFY:\n\n" + "\n\n".join(claims_text) + "\n\n"
        f"Verify each claim against its cited findings. Output a CriticReport."
    )

    return chat_json(
        cfg.anthropic_api_key,
        cfg.critic_model,
        SYSTEM,
        user,
        CriticReport,
        max_retries=2,
    )
