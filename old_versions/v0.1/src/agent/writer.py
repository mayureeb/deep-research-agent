"""Writer: synthesizes the structured Report from findings.

Produces a Report where every claim cites the integer indices of the
supporting findings. The writer prompt enforces no invention, no external
knowledge, surfacing contradictions in claim wording, and confidence
calibrated to evidence strength.
"""
from __future__ import annotations

from ..config import Config
from ..llm import chat_json
from ..state import (
    CriticIssue,
    Decomposition,
    FindingsStore,
    Report,
)


SYSTEM = """You are the writer of a research report. You must produce a STRUCTURED report where every claim is explicitly linked to the findings that support it.

Inputs you receive:
- The user's original prompt
- The decomposition (sub-questions)
- A numbered list of findings (each with claim, evidence, source, confidence)
- Optional: a list of contradictions surfaced between findings
- Optional: critic issues from a prior draft (you must address these in your revision)

Your output is a Report object with:
- summary: 2–4 sentence overview of what the research found
- claims: a list of ReportClaim objects. Each ReportClaim has:
    - claim: a sentence stating something concrete
    - confidence: 0.0–1.0, calibrated honestly (lower if findings disagree or are weak)
    - supporting_finding_indices: the integer indices of findings (from the input list) that support this claim. EVERY claim must have at least one supporting finding.
- contradictions_surfaced: copy through the contradictions you were given, plus any others you noticed
- caveats: limitations of the research — gaps in coverage, weak sources, methodological concerns

CRITICAL rules:
- Do NOT invent claims that aren't supported by the findings.
- Do NOT use external knowledge — only what's in the findings.
- If findings disagree, say so in the claim itself ("Sources disagree: A says X, B says Y") rather than picking a winner.
- Confidence should reflect the EVIDENCE'S strength, not your prose's confidence.
- It is BETTER to have fewer well-grounded claims than many weakly-grounded ones."""


def write_report(
    cfg: Config,
    user_prompt: str,
    decomp: Decomposition,
    findings: FindingsStore,
    contradictions: list[str],
    prior_report: Report | None = None,
    critic_issues: list[CriticIssue] | None = None,
) -> Report:
    """Synthesize a Report from findings.

    Args:
        cfg: Config; uses writer_model.
        user_prompt: Original prompt for the writer's context.
        decomp: Decomposition; listed in the prompt so the writer structures
            the report along the planner's lines.
        findings: Full FindingsStore. Numbered findings are rendered into
            the prompt so the writer knows what integer to cite in
            supporting_finding_indices.
        contradictions: Reconciler output for the writer to surface.
        prior_report: Only set on revision rounds.
        critic_issues: Only set on revision rounds.

    Returns:
        A validated Report.
    """
    # Numbered findings list — integer indices are the writer's referent
    # for supporting_finding_indices.
    findings_text = "\n\n".join(
        f"[{i}] sub-q={f.subquestion_id} | conf={f.confidence:.2f}\n"
        f"    claim: {f.claim}\n"
        f"    evidence: \"{f.evidence}\"\n"
        f"    source: {f.source_url}"
        for i, f in enumerate(findings.findings)
    )

    # Brief outline so the writer organizes along the sub-question structure.
    decomp_text = "\n".join(
        f"  - [{sq.id}] {sq.question}" for sq in decomp.subquestions
    )

    contradictions_text = (
        "\n\n".join(f"- {c}" for c in contradictions) if contradictions else "(none)"
    )

    # Uncertainty notes are surfaced as caveats, not as claims.
    if findings.uncertainty_notes:
        uncertainty_text = "\n".join(
            f"  - [{n.subquestion_id}] tried: {n.topic} — could not because: {n.reason}"
            for n in findings.uncertainty_notes
        )
    else:
        uncertainty_text = "(none)"

    user = (
        f"USER PROMPT:\n{user_prompt}\n\n"
        f"DECOMPOSITION:\n{decomp_text}\n\n"
        f"FINDINGS (numbered — use these indices in supporting_finding_indices):\n\n"
        f"{findings_text}\n\n"
        f"CONTRADICTIONS DETECTED:\n{contradictions_text}\n\n"
        f"UNCERTAINTY NOTES (researchers tried these but couldn't find concrete evidence — "
        f"surface these as caveats in your report, do NOT make claims about them):\n"
        f"{uncertainty_text}\n"
    )

    # Revision branch: append prior critic issues so the writer can target fixes.
    if prior_report and critic_issues:
        issues_text = "\n".join(
            f"- claim #{ci.claim_index} ({ci.issue_type}): {ci.explanation}"
            for ci in critic_issues
        )
        user += (
            f"\n\nYOUR PRIOR DRAFT had these issues from the critic. "
            f"Fix them in this revision:\n{issues_text}\n"
        )

    return chat_json(
        cfg.anthropic_api_key,
        cfg.writer_model,
        SYSTEM,
        user,
        Report,
        max_retries=2,
    )
