"""Source quality aggregate — average ``Finding.source_quality`` across cited findings.

Reports both ``_cited`` (what the writer actually used) and ``_all``
(what was harvested) so a reviewer can spot the failure mode
"researcher found good sources, writer didn't use them."
"""
from __future__ import annotations

from collections import Counter

from src.agent.confidence import SOURCE_QUALITY_TIERS, source_quality_tier
from src.state import FindingsStore, Report


def source_quality_summary(
    report: Report | None,
    findings: FindingsStore,
) -> dict:
    """Aggregate Finding.source_quality across cited and all findings.

    Returns:
        mean_cited_quality / median_cited_quality / min_cited_quality:
            floats. 0.0 if no cited findings.
        by_tier_cited:    dict[tier_name, count]. Every key in
            SOURCE_QUALITY_TIERS appears (count 0 if absent) so the
            JSON schema is stable across runs.
        n_cited_findings: count.
        mean_all_quality: float. Same population as compute_source_quality
            ran over.
        by_tier_all:      dict[tier_name, count]. Same shape as cited.
        n_all_findings:   count.
    """
    finding_list = findings.findings if findings else []

    cited_indices = _cited_finding_indices(report)
    cited = [finding_list[i] for i in cited_indices if 0 <= i < len(finding_list)]

    return {
        "mean_cited_quality": _mean([f.source_quality for f in cited]),
        "median_cited_quality": _median([f.source_quality for f in cited]),
        "min_cited_quality": min((f.source_quality for f in cited), default=0.0),
        "by_tier_cited": _tier_counts([f.source_url for f in cited]),
        "n_cited_findings": len(cited),
        "mean_all_quality": _mean([f.source_quality for f in finding_list]),
        "by_tier_all": _tier_counts([f.source_url for f in finding_list]),
        "n_all_findings": len(finding_list),
    }


def _cited_finding_indices(report: Report | None) -> set[int]:
    """Set of finding indices that appear in any claim's supporting list."""
    if not report or not report.claims:
        return set()
    out: set[int] = set()
    for c in report.claims:
        out.update(c.supporting_finding_indices)
    return out


def _tier_counts(urls: list[str]) -> dict[str, int]:
    """Build a tier → count dict, with all tiers always present (0 if absent)."""
    c: Counter[str] = Counter(source_quality_tier(u) for u in urls)
    return {t: c.get(t, 0) for t in SOURCE_QUALITY_TIERS}


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 == 1 else (s[mid - 1] + s[mid]) / 2
