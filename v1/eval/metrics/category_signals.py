"""Category-design signals — did each prompt category achieve its design goal?

Two metric pairs (per-prompt + aggregate): contradiction-surfacing for
the ``contradictory`` category, and uncertainty-surfacing for the
``sparse`` category. Pure-Python, $0 LLM cost.
"""
from __future__ import annotations

from collections import Counter

from src.state import Report, ResearchRun


# ---------------------------------------------------------------------------
# Per-prompt extractors.
# ---------------------------------------------------------------------------


def contradiction_signals(report: Report | None) -> dict:
    """Per-prompt: count + breakdown of typed Contradictions on the report."""
    if not report or not report.contradictions_surfaced:
        return {
            "n_surfaced": 0,
            "by_severity": {"minor": 0, "moderate": 0, "major": 0},
            "by_type": {
                "factual": 0, "methodological": 0, "framing": 0,
                "temporal": 0, "other": 0,
            },
        }
    sev = Counter(c.severity for c in report.contradictions_surfaced)
    typ = Counter(c.type for c in report.contradictions_surfaced)
    return {
        "n_surfaced": len(report.contradictions_surfaced),
        "by_severity": {
            k: sev.get(k, 0) for k in ("minor", "moderate", "major")
        },
        "by_type": {
            k: typ.get(k, 0)
            for k in ("factual", "methodological", "framing", "temporal", "other")
        },
    }


def honesty_signals(rr: ResearchRun) -> dict:
    """Per-prompt: uncertainty notes + caveats + a substring heuristic for
    whether the writer's caveats reference the researcher's uncertainty.

    `caveats_reference_uncertainty` is a generous substring check:
        for each UncertaintyNote.topic, look for the (lowercased,
        whitespace-collapsed) topic as a substring inside the joined
        report.caveats.
    False positives are possible (a generic topic word incidentally
    appears in an unrelated caveat) — acceptable. A more rigorous check
    would need an LLM judge and is post-port work.
    """
    notes = rr.findings.uncertainty_notes if rr.findings else []
    caveats = rr.report.caveats if rr.report else []

    n_notes = len(notes)
    n_caveats = len(caveats)
    fraction_subqs_with_uncertainty = (
        len({n.subquestion_id for n in notes}) / len(rr.decomposition.subquestions)
        if rr.decomposition.subquestions else 0.0
    )

    caveats_reference_uncertainty = False
    if notes and caveats:
        joined = " ".join(c.lower() for c in caveats)
        joined = " ".join(joined.split())
        for n in notes:
            topic = " ".join(n.topic.lower().split())
            if topic and topic in joined:
                caveats_reference_uncertainty = True
                break

    return {
        "n_uncertainty_notes": n_notes,
        "n_caveats": n_caveats,
        "fraction_subqs_with_uncertainty": fraction_subqs_with_uncertainty,
        "caveats_reference_uncertainty": caveats_reference_uncertainty,
    }


# ---------------------------------------------------------------------------
# Cross-prompt aggregates.
# ---------------------------------------------------------------------------


def contradiction_aggregate(rows: list[dict]) -> dict:
    """Across the run, what fraction of `contradictory`-category prompts
    actually surfaced ≥1 typed Contradiction?

    Args:
        rows: per-prompt result dicts emitted by run_eval.main(). Each
            row needs at least `category` and `contradictions.n_surfaced`.

    Returns:
        category_match_rate:  float in [0, 1] OR None if no contradictory
                              prompts ran (so the caller can render "—").
        category_total:       count of contradictory-category prompts.
        category_with_contradictions: count of those with ≥1 surfaced.
        n_surfaced_total:     total contradictions surfaced ACROSS the run
                              (all categories, not just contradictory).
    """
    contras_rows = [r for r in rows if r.get("category") == "contradictory"]
    total = len(contras_rows)
    matched = sum(
        1 for r in contras_rows
        if r.get("contradictions", {}).get("n_surfaced", 0) >= 1
    )
    n_total_across_run = sum(
        r.get("contradictions", {}).get("n_surfaced", 0) for r in rows
    )
    return {
        "category_match_rate": (matched / total) if total else None,
        "category_total": total,
        "category_with_contradictions": matched,
        "n_surfaced_total": n_total_across_run,
    }


def honesty_aggregate(rows: list[dict]) -> dict:
    """Across the run, what fraction of `sparse`-category prompts emitted
    ≥1 uncertainty signal (UncertaintyNote OR caveat)?

    Same shape as contradiction_aggregate. Both UncertaintyNote AND caveats
    count — see docstring of `honesty_signals` for why.
    """
    sparse_rows = [r for r in rows if r.get("category") == "sparse"]
    total = len(sparse_rows)
    matched = sum(
        1 for r in sparse_rows
        if (
            r.get("honesty", {}).get("n_uncertainty_notes", 0) >= 1
            or r.get("honesty", {}).get("n_caveats", 0) >= 1
        )
    )
    return {
        "category_match_rate": (matched / total) if total else None,
        "category_total": total,
        "category_with_signal": matched,
    }
