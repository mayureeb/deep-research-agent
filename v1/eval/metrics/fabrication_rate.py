"""Fabrication rate — fraction of critic-approved claims that cite a finding the
re-fetch verifier marks ``fabricated``.

If the run already has a ``verify_history`` we read the last round
directly; otherwise we invoke the verifier on the cited findings post-hoc.
"""
from __future__ import annotations

from src.config import Config
from src.state import (
    CriticReport, FindingsStore, Report, ResearchRun, VerifyReport,
)
from src.agent.refetch_verifier import verify_cited
from src.tools.fetch import FetchContext


async def fabrication_rate(
    cfg: Config,
    rr: ResearchRun,
    fetch_ctx: FetchContext | None = None,
) -> dict:
    """Compute the fabrication rate over `rr`'s critic-approved claims.

    Args:
        cfg: Loaded Config — needed only for the post-hoc verifier
            re-run path (uses cfg.parallel_researchers as the Semaphore
            cap, identical to production).
        rr: The ResearchRun to analyze. Works on v0 / v1-no-verifier /
            v1 runs uniformly.
        fetch_ctx: Optional FetchContext. Pass one if you want
            per-domain failure memory shared with another scorer (e.g.
            in eval/v0_v1_compare.py we share one across the three
            modes per prompt so a flaky source doesn't double-count).

    Returns:
        Dict — see module docstring for the keys + their semantics.

    Defensive against the empty / minimal-shape cases:
      * No report → rate = 0.0, all counts 0 (treat as "no signal").
      * Empty claims → same.
      * No cited findings (writer cited nothing valid) → rate = 0.0
        with verify_report = empty (we have nothing to verify).
    """
    if not rr.report or not rr.report.claims:
        return _empty_result()

    # Identify the approved-claim indices over the FINAL critic round.
    approved_indices = _approved_claim_indices(rr.report, rr.critic_history)

    # Resolve the VerifyReport. Three paths:
    #   1) v1 with native verify_history → reuse the last round.
    #   2) v0 / v1-no-verifier → re-run the verifier ourselves on the
    #      cited findings. The re-run is idempotent (verify_cited's
    #      cache is per-call, not cross-run).
    #   3) Pathological — verify_history is non-empty but the LAST
    #      round is empty (writer cited nothing) → just use it as-is.
    verifier_was_rerun = False
    if rr.verify_history and rr.verify_history[-1].results:
        verify_report = rr.verify_history[-1]
    else:
        cited_indices = sorted({
            fi for c in rr.report.claims for fi in c.supporting_finding_indices
            if 0 <= fi < len(rr.findings.findings)
        })
        if not cited_indices:
            return _empty_result()
        verify_report = await verify_cited(
            cfg, rr.findings.findings, cited_indices,
            fetch_ctx=fetch_ctx, cached={},
        )
        verifier_was_rerun = True

    fabricated = set(verify_report.fabricated_indices)
    # "drifted" is surfaced separately, NOT counted in the rate. See
    # module docstring for why we don't conflate it with fabrication.
    drifted = {
        r.finding_index for r in verify_report.results if r.verdict == "drifted"
    }

    # For each approved claim, does any of its cited findings have a
    # fabricated verdict? (set intersection on supporting_finding_indices.)
    approved_with_fab = 0
    approved_with_drift = 0
    for ci in approved_indices:
        if ci < 0 or ci >= len(rr.report.claims):
            continue
        cited = set(rr.report.claims[ci].supporting_finding_indices)
        if cited & fabricated:
            approved_with_fab += 1
        if cited & drifted:
            approved_with_drift += 1

    n_approved = len(approved_indices)
    rate = approved_with_fab / n_approved if n_approved else 0.0
    drift = approved_with_drift / n_approved if n_approved else 0.0

    return {
        "rate": rate,
        "approved_total": n_approved,
        "approved_with_fabricated": approved_with_fab,
        "fabricated_finding_indices": sorted(fabricated),
        "drift_rate": drift,
        "approved_with_drifted": approved_with_drift,
        "verify_report": verify_report,
        "verifier_was_rerun": verifier_was_rerun,
    }


def _approved_claim_indices(
    report: Report, critic_history: list[CriticReport],
) -> list[int]:
    """Return claim indices the FINAL critic round did NOT flag.

    Mirrors `agent/orchestrator._compute_metrics` semantics so the
    fabrication_rate denominator is consistent with the runtime
    grounding_rate's denominator. Multiple issues on the same claim
    count once (set semantics).

    Edge case: no critic ran → return ALL claim indices (we have no
    grounds to drop any). This is the v0-baseline / no-critic-mode
    path; the metric still measures something useful.
    """
    n = len(report.claims)
    if not critic_history:
        return list(range(n))
    final = critic_history[-1]
    if final.approved:
        return list(range(n))
    flagged = {issue.claim_index for issue in final.issues}
    return [i for i in range(n) if i not in flagged]


def _empty_result() -> dict:
    """Sentinel for "no signal" cases: empty report / no cited findings.

    Returning rate=0.0 instead of None on purpose — downstream tables
    render 0% cleanly, and "no signal" is closer to "nothing
    fabricated" than to "unknown" for the headline framing.
    """
    return {
        "rate": 0.0,
        "approved_total": 0,
        "approved_with_fabricated": 0,
        "fabricated_finding_indices": [],
        "drift_rate": 0.0,
        "approved_with_drifted": 0,
        "verify_report": VerifyReport(results=[]),
        "verifier_was_rerun": False,
    }


def summarize_fabrication_results(rows: list[dict]) -> dict:
    """Aggregate per-prompt fabrication results into a run-suite summary.

    Used by `eval/v0_v1_compare.py` to produce the headline number
    across all 8 prompts × 3 modes. Each `rows` entry is the dict
    `fabrication_rate` returned for one (prompt, mode) cell.

    Returns:
      {
        "n_prompts": int,
        "approved_total_sum": int,                 # Σ approved across all prompts
        "approved_with_fabricated_sum": int,       # Σ approved-with-fab
        "weighted_rate": float,                    # Σ fab / Σ approved (the headline)
        "macro_rate": float,                       # mean of per-prompt rates
        "drift_macro_rate": float,
      }

    Why two rates:
      * weighted_rate is the honest one for the headline — it counts
        every claim equally regardless of which prompt produced it.
      * macro_rate is robust to one prompt with many claims dominating
        the weighted number; useful as a sanity check.
    """
    if not rows:
        return {
            "n_prompts": 0, "approved_total_sum": 0,
            "approved_with_fabricated_sum": 0,
            "weighted_rate": 0.0, "macro_rate": 0.0, "drift_macro_rate": 0.0,
        }
    total_approved = sum(r.get("approved_total", 0) for r in rows)
    total_fab = sum(r.get("approved_with_fabricated", 0) for r in rows)
    weighted = total_fab / total_approved if total_approved else 0.0
    macro = sum(r.get("rate", 0.0) for r in rows) / len(rows)
    drift_macro = sum(r.get("drift_rate", 0.0) for r in rows) / len(rows)
    return {
        "n_prompts": len(rows),
        "approved_total_sum": total_approved,
        "approved_with_fabricated_sum": total_fab,
        "weighted_rate": weighted,
        "macro_rate": macro,
        "drift_macro_rate": drift_macro,
    }
