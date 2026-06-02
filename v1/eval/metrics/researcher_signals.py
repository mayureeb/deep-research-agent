"""Researcher / findings signals — counts and concentration over the researcher pool.

Per-prompt diagnostics: how many sub-questions came back with no
findings, how many researchers emitted uncertainty notes, and how
concentrated the finding output is across researchers.
"""
from __future__ import annotations

from src.state import ResearchRun


def researcher_signals(rr: ResearchRun) -> dict:
    """Per-prompt: counts and concentration metrics over the researcher pool.

    Args:
        rr: ResearchRun. Reads from `rr.findings`, `rr.decomposition`.
            Robust to None / empty fields.

    Returns:
        n_subquestions:                count of sub-qs in the decomposition.
        n_subquestions_without_findings:
                                       sub-qs where `FindingsStore.by_subquestion(id)`
                                       returned an empty list. Best observable
                                       proxy for "researcher with 0 findings".
        n_researchers_with_findings:   distinct researcher_ids appearing in
                                       FindingsStore.findings.
        n_researchers_with_uncertainty:
                                       distinct researcher_ids appearing in
                                       FindingsStore.uncertainty_notes.
        n_researchers_with_both:       intersection of the above two —
                                       researchers that found something AND
                                       flagged something else.
        total_findings:                len(findings.findings).
        total_uncertainty_notes:       len(findings.uncertainty_notes).
        mean_findings_per_active_researcher:
                                       total_findings / n_researchers_with_findings,
                                       or 0.0 if no active researchers.
        max_findings_one_researcher:   max # findings any single researcher_id
                                       contributed. High vs the mean signals
                                       researcher concentration.
    """
    findings = rr.findings.findings if rr.findings else []
    notes = rr.findings.uncertainty_notes if rr.findings else []
    subqs = rr.decomposition.subquestions if rr.decomposition else []
    n_subqs = len(subqs)

    # Sub-questions that produced no Finding.
    sq_with_findings = {f.subquestion_id for f in findings}
    n_subqs_no_findings = sum(1 for sq in subqs if sq.id not in sq_with_findings)

    # Researcher counts.
    finding_rids: set[str] = {f.researcher_id for f in findings if f.researcher_id}
    note_rids: set[str] = {n.researcher_id for n in notes if n.researcher_id}
    n_with_findings = len(finding_rids)
    n_with_uncertainty = len(note_rids)
    n_with_both = len(finding_rids & note_rids)

    # Concentration. Per-researcher finding count, then max + mean.
    per_rid_count: dict[str, int] = {}
    for f in findings:
        if f.researcher_id:
            per_rid_count[f.researcher_id] = per_rid_count.get(f.researcher_id, 0) + 1
    max_findings_one_rid = max(per_rid_count.values(), default=0)
    mean_findings_per_active = (
        len(findings) / n_with_findings if n_with_findings else 0.0
    )

    return {
        "n_subquestions": n_subqs,
        "n_subquestions_without_findings": n_subqs_no_findings,
        "n_researchers_with_findings": n_with_findings,
        "n_researchers_with_uncertainty": n_with_uncertainty,
        "n_researchers_with_both": n_with_both,
        "total_findings": len(findings),
        "total_uncertainty_notes": len(notes),
        "mean_findings_per_active_researcher": mean_findings_per_active,
        "max_findings_one_researcher": max_findings_one_rid,
    }
