"""Writer: synthesizes the structured Report from findings.

Single LLM call producing a Report where every claim cites integer indices
of supporting findings. Shape-aware: pick_writer_system(shape) selects a
prompt tuned for PRE_STRUCTURED / CONTESTED / SPARSE_EMERGING / DISCOVERY /
GENERAL.
"""
from __future__ import annotations

from ..config import Config
from ..llm import chat_json
from ..state import (
    ClassifiedShape,
    Contradiction,
    CriticIssue,
    Decomposition,
    FindingsStore,
    Report,
    VerifyReport,
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
- contradictions_surfaced: list of Contradiction objects (description + finding_ids + severity + type). Copy through the ones you were given, and add any others you notice. For each one, finding_ids must reference indices from the FINDINGS list. Pick severity ("minor" / "moderate" / "major") and type ("factual" / "methodological" / "framing" / "temporal" / "other") honestly.
- caveats: limitations of the research — gaps in coverage, weak sources, methodological concerns

CRITICAL rules:
- Do NOT invent claims that aren't supported by the findings.
- Do NOT use external knowledge — only what's in the findings.
- If findings disagree, say so in the claim itself ("Sources disagree: A says X, B says Y") rather than picking a winner.
- Confidence calibration: each finding has THREE axes — self-reported (the researcher's number), source-quality (a domain-table score), and agreement (cross-source corroboration). The COMBINED number (shown as `combined`) is the validated signal. Calibrate ReportClaim.confidence to combined, NOT to the self-reported number.
- When a finding is flagged with [axis-disagree] (the three axes diverge), TEMPER the claim that uses it: hedge the wording, lower the confidence, or move the finding to caveats. Axis disagreement is the most reliable signal that a finding is bias-prone.
- It is BETTER to have fewer well-grounded claims than many weakly-grounded ones."""


_SHAPE_ANNEX_PRE_STRUCTURED = """

SHAPE-SPECIFIC GUIDANCE — PRE_STRUCTURED:
The user supplied structure (a comparison axis list, an explicit field list, etc.). Your report's organization MUST mirror their structure — sections in the order they named, claims grouped per axis. Do NOT invent additional axes. Preserving the user's framing is more important than completeness on extra angles."""


_SHAPE_ANNEX_CONTESTED = """

SHAPE-SPECIFIC GUIDANCE — CONTESTED:
The answer is disputed. LEAD WITH THE DISAGREEMENT — your summary should name the disagreement in its first sentence. For each major contested point, surface BOTH sides as sibling claims (do not collapse them into a single hedged claim). When the methodology behind the disagreement is itself worth flagging, add a separate claim about the methodological dispute."""


_SHAPE_ANNEX_SPARSE_EMERGING = """

SHAPE-SPECIFIC GUIDANCE — SPARSE_EMERGING:
The literature is THIN. Be honest about that — your summary should explicitly say so when true. Prefer FEWER claims with hedged wording over many speculative ones. If a sub-question came back with mostly UncertaintyNotes, surface that as a caveat rather than thin synthesis. Lower confidence is usually the right calibration here."""


_SHAPE_ANNEX_DISCOVERY = """

SHAPE-SPECIFIC GUIDANCE — DISCOVERY:
The user wants the answer SPACE mapped, not a single answer. Organize the report by THEMES / CATEGORIES / approaches discovered, NOT by sub-question. Each claim should describe a category or approach — its definition, who's doing it, how mature it is. Caveats should call out categories you noticed but didn't fully cover."""


def pick_writer_system(shape: ClassifiedShape | None) -> str:
    """Return the writer SYSTEM prompt tuned for one shape. None or GENERAL → base."""
    if shape is None or shape.shape == "GENERAL":
        return SYSTEM
    annex = {
        "PRE_STRUCTURED": _SHAPE_ANNEX_PRE_STRUCTURED,
        "CONTESTED": _SHAPE_ANNEX_CONTESTED,
        "SPARSE_EMERGING": _SHAPE_ANNEX_SPARSE_EMERGING,
        "DISCOVERY": _SHAPE_ANNEX_DISCOVERY,
    }.get(shape.shape, "")
    return SYSTEM + annex


def write_report(
    cfg: Config,
    user_prompt: str,
    decomp: Decomposition,
    findings: FindingsStore,
    contradictions: list[Contradiction],
    prior_report: Report | None = None,
    critic_issues: list[CriticIssue] | None = None,
    verify_report: VerifyReport | None = None,
    shape: ClassifiedShape | None = None,
) -> Report:
    """Synthesize a Report from findings."""
    def _verdict_tag(idx: int) -> str:
        if verify_report is None:
            return ""
        vr = verify_report.by_index(idx)
        if vr is None:
            return ""
        return f"  [VERIFIER: {vr.verdict.upper()}]"

    findings_text = "\n\n".join(
        f"[{i}] sub-q={f.subquestion_id}  "
        f"self={f.confidence:.2f}  source_q={f.source_quality:.2f}  "
        f"agreement={f.agreement_score:.2f}  combined={f.combined_confidence:.2f}"
        + ("  [AXIS-DISAGREE]" if f.axis_disagreement else "")
        + _verdict_tag(i) + "\n"
        f"    claim: {f.claim}\n"
        f"    evidence: \"{f.evidence}\"\n"
        f"    source: {f.source_url}"
        for i, f in enumerate(findings.findings)
    )

    decomp_text = "\n".join(
        f"  - [{sq.id}] {sq.question}" for sq in decomp.subquestions
    )

    if contradictions:
        contradictions_text = "\n\n".join(
            f"- [severity={c.severity}, type={c.type}, finding_ids={c.finding_ids}]\n"
            f"  {c.description}"
            for c in contradictions
        )
    else:
        contradictions_text = "(none)"

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

    if prior_report and critic_issues:
        issues_text = "\n".join(
            f"- claim #{ci.claim_index} ({ci.issue_type}): {ci.explanation}"
            for ci in critic_issues
        )
        user += (
            f"\n\nYOUR PRIOR DRAFT had these issues from the critic. "
            f"Fix them in this revision:\n{issues_text}\n"
        )
        if verify_report is not None and (
            verify_report.fabricated_indices or verify_report.unreachable_indices
        ):
            user += (
                "\nVERIFIER VERDICT GUIDANCE for this revision:\n"
                "- Any finding tagged [VERIFIER: FABRICATED] in the FINDINGS "
                "list — REMOVE its index from any claim's "
                "supporting_finding_indices. If a claim has no other support "
                "after removal, DROP THE CLAIM entirely. Do NOT keep the "
                "claim with hedged wording — fabricated evidence is not "
                "evidence.\n"
                "- Any finding tagged [VERIFIER: UNREACHABLE] — we couldn't "
                "re-check it. Hedge any claim that leans on it; consider "
                "moving it to caveats.\n"
                "- Any finding tagged [VERIFIER: DRIFTED] — the page changed "
                "since the original fetch. The claim may still be roughly "
                "correct; hedge the wording.\n"
            )

    system_prompt = pick_writer_system(shape)

    report = chat_json(
        cfg.anthropic_api_key,
        cfg.writer_model,
        system_prompt,
        user,
        Report,
        max_retries=2,
        max_tokens=16384,
    )
    return report
