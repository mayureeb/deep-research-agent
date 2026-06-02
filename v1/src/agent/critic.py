"""Critic: verifies that every claim in the report is grounded in the findings.

The critic sees claims + evidence (and per-finding verifier verdicts and
confidence axes), but never the writer's reasoning. Returns a CriticReport
with typed issues (ungrounded / overstated / missing_citation /
source_mismatch). approved=True only when issues is empty.
"""
from __future__ import annotations

from ..config import Config
from ..llm import chat_json
from ..state import (
    CriticIssue,
    CriticReport,
    Decomposition,
    FindingsStore,
    Report,
    VerifyReport,
)


SYSTEM = """You are an independent fact-checker for a research report.

You do NOT see the writer's reasoning. You see only:
- Each report claim
- The findings the writer cited as supporting it (with their evidence quotes, sources, and per-finding confidence axes)

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

Confidence axes (v1):
- Each cited finding shows: self (researcher's self-reported), source_q (domain quality), agreement (cross-source corroboration), combined (the three-axis combine).
- A finding tagged [AXIS-DISAGREE] has axes that diverge by a lot (e.g. self=0.9 but source_q=0.4) — the researcher's confidence is NOT corroborated by objective signals. SCRUTINIZE claims that lean on [AXIS-DISAGREE] findings harder: did the writer temper the wording? If the claim sounds confident but its supporting findings are flagged, prefer "overstated".
- A claim's stated confidence should roughly track the average of its cited findings' combined values. Wide gap → "overstated".

Verifier verdicts (v1, the headline):
- Each cited finding ALSO shows a VERDICT from the re-fetch verifier — it re-fetched the source URL and checked whether the cached evidence quote actually appears in the live page:
    * "verified"    — the quote is present in the live page. Trust the finding.
    * "fabricated"  — the quote is NOT in the live page. The researcher made it up. Any claim citing a fabricated finding is automatically "ungrounded" — flag it. Do not give benefit of the doubt; the orchestrator's defense-in-depth will also auto-flag, but YOU should flag too so the explanation reaches the writer in human language.
    * "drifted"     — the quote isn't verbatim but significant words overlap; the source likely changed since fetching. Temper the claim if it cites these (a hedged claim is fine; an unhedged "X is true" is overstated).
    * "unreachable" — the source could not be re-fetched (paywall, 404, timeout). Do NOT mark as fabricated — we have no signal. Treat as you would any cited finding without the verifier signal.

Output a CriticReport with:
- issues: list of CriticIssue objects (only for problematic claims)
- approved: true ONLY if there are no issues at all"""


def critique(
    cfg: Config,
    user_prompt: str,
    decomp: Decomposition,
    findings: FindingsStore,
    report: Report,
    verify_report: VerifyReport | None = None,
) -> CriticReport:
    """Run one critic pass on a Report.

    When verify_report is provided, fabricated-finding verdicts both feed
    the LLM critic prompt and synthesize ungrounded CriticIssues for any
    claim citing a fabricated finding the LLM missed.
    """
    findings_text = []
    for i, f in enumerate(findings.findings):
        flag = " [AXIS-DISAGREE]" if f.axis_disagreement else ""
        axes = (
            f"self={f.confidence:.2f} source_q={f.source_quality:.2f} "
            f"agreement={f.agreement_score:.2f} combined={f.combined_confidence:.2f}"
            f"{flag}"
        )
        findings_text.append(
            f"[{i}] {f.claim}\n"
            f"    axes: {axes}\n"
            f"    evidence: \"{f.evidence}\"\n"
            f"    source: {f.source_url}"
        )
    findings_block = "\n\n".join(findings_text)

    claims_text = []
    for ci, claim in enumerate(report.claims):
        cited = []
        for fi in claim.supporting_finding_indices:
            if 0 <= fi < len(findings.findings):
                f = findings.findings[fi]
                axis_tag = (
                    f"combined={f.combined_confidence:.2f}"
                    + (" [AXIS-DISAGREE]" if f.axis_disagreement else "")
                )
                verdict_tag = ""
                if verify_report is not None:
                    vr = verify_report.by_index(fi)
                    if vr is not None:
                        verdict_tag = f"  [VERIFIER: {vr.verdict.upper()}]"
                cited.append(
                    f'  [{fi}] "{f.evidence}"  ({f.source_url})  ({axis_tag}){verdict_tag}'
                )
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

    llm_report = chat_json(
        cfg.anthropic_api_key,
        cfg.critic_model,
        SYSTEM,
        user,
        CriticReport,
        max_retries=2,
    )

    if verify_report is None:
        return llm_report
    return _merge_with_synthetic_fabrication_issues(
        llm_report, report, verify_report
    )


def _merge_with_synthetic_fabrication_issues(
    llm_report: CriticReport,
    report: Report,
    verify_report: VerifyReport,
) -> CriticReport:
    """Inject ungrounded CriticIssues for any claim citing a fabricated finding."""
    fabricated = set(verify_report.fabricated_indices)
    if not fabricated:
        return llm_report

    existing_keys = {(i.claim_index, i.issue_type) for i in llm_report.issues}
    merged_issues = list(llm_report.issues)

    for ci, claim in enumerate(report.claims):
        cited_fab = [fi for fi in claim.supporting_finding_indices if fi in fabricated]
        if not cited_fab:
            continue
        if (ci, "ungrounded") in existing_keys:
            continue
        merged_issues.append(CriticIssue(
            claim_index=ci,
            issue_type="ungrounded",
            explanation=(
                f"Re-fetch verifier marked the cited finding(s) "
                f"{cited_fab} as fabricated — the cached evidence quote "
                f"does not appear in the live source page. The claim is "
                f"therefore ungrounded regardless of how plausible the "
                f"text reads."
            ),
        ))

    return CriticReport(
        issues=merged_issues,
        approved=(len(merged_issues) == 0),
    )
