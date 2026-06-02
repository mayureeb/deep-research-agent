"""Citation density — how the writer uses the findings it has.

Surfaces wasted research budget (uncited harvested findings),
single-source dependence (max source share, Herfindahl concentration),
and pseudo-multi-citation (claims cited from N findings that share one
URL).
"""
from __future__ import annotations

from collections import Counter

from src.state import FindingsStore, Report


def citation_density(report: Report | None, findings: FindingsStore) -> dict:
    """Compute citation-density diagnostics for one report.

    Returns:
        avg_per_claim:        mean(len(c.supporting_finding_indices))
        median_per_claim:     median of the same
        claim_count_distribution: list[int] — index i = #claims with i citations,
                                  truncated to max observed citation count + 1
        uncited_claims:       count of claims with empty supporting list
        load_bearing_fraction: |cited_indices| / len(findings.findings)
                              — what fraction of harvested findings actually
                              got cited. Low = wasted research budget.
        n_findings:           total findings harvested
        n_cited_findings:     count of findings cited by ≥1 claim
        source_concentration_herfindahl: Σ(s_i²) where s_i = share of
                              CITATIONS from source i. 1.0 = single source;
                              ~1/N = perfectly diverse.
        max_source_share:     max single source's fraction of all citations.
                              >0.4 flags single-source dependence.
        avg_per_claim_distinct_domains: per claim, count distinct domains
                              in its citations; report mean. <1.5 flags
                              pseudo-multi-citation.
    """
    if not report or not report.claims:
        return _empty(len(findings.findings) if findings else 0)

    claims = report.claims
    finding_list = findings.findings if findings else []
    n_findings = len(finding_list)

    citation_counts = [len(c.supporting_finding_indices) for c in claims]
    uncited = sum(1 for c in citation_counts if c == 0)
    avg_per_claim = sum(citation_counts) / len(claims)
    median_per_claim = _median(citation_counts)
    distribution = _histogram(citation_counts)

    # Build cited-finding set + per-source citation counter (for the
    # concentration metrics, citing the same finding twice in different
    # claims counts twice — that's exactly what we want to measure).
    cited_indices: set[int] = set()
    citation_source_counter: Counter[str] = Counter()
    per_claim_distinct_domains: list[int] = []
    for c in claims:
        domains_for_claim: set[str] = set()
        for fi in c.supporting_finding_indices:
            if 0 <= fi < n_findings:
                cited_indices.add(fi)
                f = finding_list[fi]
                # Prefer Finding.domain (already extracted); fall back to URL.
                key = f.domain or f.source_url or f"finding_{fi}"
                citation_source_counter[key] += 1
                domains_for_claim.add(key)
        per_claim_distinct_domains.append(len(domains_for_claim))

    n_cited_findings = len(cited_indices)
    load_bearing_fraction = (
        n_cited_findings / n_findings if n_findings else 0.0
    )

    total_citations = sum(citation_source_counter.values())
    if total_citations:
        shares = [c / total_citations for c in citation_source_counter.values()]
        herfindahl = sum(s * s for s in shares)
        max_share = max(shares)
    else:
        herfindahl = 0.0
        max_share = 0.0

    avg_distinct_domains = (
        sum(per_claim_distinct_domains) / len(per_claim_distinct_domains)
        if per_claim_distinct_domains else 0.0
    )

    return {
        "avg_per_claim": avg_per_claim,
        "median_per_claim": median_per_claim,
        "claim_count_distribution": distribution,
        "uncited_claims": uncited,
        "load_bearing_fraction": load_bearing_fraction,
        "n_findings": n_findings,
        "n_cited_findings": n_cited_findings,
        "source_concentration_herfindahl": herfindahl,
        "max_source_share": max_share,
        "avg_per_claim_distinct_domains": avg_distinct_domains,
    }


def _empty(n_findings: int) -> dict:
    return {
        "avg_per_claim": 0.0,
        "median_per_claim": 0.0,
        "claim_count_distribution": [],
        "uncited_claims": 0,
        "load_bearing_fraction": 0.0,
        "n_findings": n_findings,
        "n_cited_findings": 0,
        "source_concentration_herfindahl": 0.0,
        "max_source_share": 0.0,
        "avg_per_claim_distinct_domains": 0.0,
    }


def _median(xs: list[int]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    mid = len(s) // 2
    return float(s[mid] if len(s) % 2 == 1 else (s[mid - 1] + s[mid]) / 2)


def _histogram(citation_counts: list[int]) -> list[int]:
    """Return [n_with_0, n_with_1, ..., n_with_max]. Empty list for empty input."""
    if not citation_counts:
        return []
    top = max(citation_counts)
    out = [0] * (top + 1)
    for c in citation_counts:
        out[c] += 1
    return out
