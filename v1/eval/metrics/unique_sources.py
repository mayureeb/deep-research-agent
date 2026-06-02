"""Unique-source cardinality scalars (URL- and domain-level, harvested vs cited)."""
from __future__ import annotations

from urllib.parse import urlparse

from src.state import FindingsStore, Report


def unique_sources(report: Report | None, findings: FindingsStore) -> dict:
    """Per-prompt: how many distinct URLs / domains back the report?

    Args:
        report: The Report. Cited URLs come from
            `claims[*].supporting_finding_indices` resolved against
            `findings.findings`. None / empty → all cited counts are 0.
        findings: The FindingsStore. Harvested URLs come from
            `Finding.source_url`. None / empty → all harvested counts are 0.

    Returns:
        n_unique_urls_cited:        distinct URLs referenced by any claim.
        n_unique_urls_harvested:    distinct URLs across all findings.
        n_unique_domains_cited:     distinct domains in cited URLs.
        n_unique_domains_harvested: distinct domains across all findings.
        cited_url_reuse_ratio:      total citation count / unique cited URLs.
                                    1.0 = every citation is to a different URL;
                                    >1.0 = some URLs are cited multiple times.
                                    0.0 if no citations.
        cited_domain_diversity_ratio:
                                    n_unique_domains_cited /
                                    n_unique_urls_cited. 1.0 = every cited
                                    URL is on a different domain;
                                    <1.0 = some domain has multiple cited
                                    pages. 0.0 if no cited URLs.
        cited_to_harvested_url_ratio:
                                    n_unique_urls_cited /
                                    n_unique_urls_harvested. The "writer
                                    actually used what researchers found"
                                    fraction. 0.0 if nothing harvested.
        cited_to_harvested_domain_ratio:
                                    same but at the domain level.
    """
    finding_list = findings.findings if findings else []

    # Harvested side: every finding's source_url is one harvested source.
    harvested_urls = {f.source_url for f in finding_list if f.source_url}
    harvested_domains = {_domain(u) for u in harvested_urls if _domain(u)}

    # Cited side: walk claims → supporting_finding_indices → finding URLs.
    # Multiset (list) to compute reuse ratio; set to compute cardinality.
    citation_url_list: list[str] = []
    if report and report.claims:
        for c in report.claims:
            for i in c.supporting_finding_indices:
                if 0 <= i < len(finding_list):
                    url = finding_list[i].source_url
                    if url:
                        citation_url_list.append(url)
    cited_urls = set(citation_url_list)
    cited_domains = {_domain(u) for u in cited_urls if _domain(u)}

    n_cited_urls = len(cited_urls)
    n_cited_domains = len(cited_domains)
    n_harvested_urls = len(harvested_urls)
    n_harvested_domains = len(harvested_domains)

    return {
        "n_unique_urls_cited": n_cited_urls,
        "n_unique_urls_harvested": n_harvested_urls,
        "n_unique_domains_cited": n_cited_domains,
        "n_unique_domains_harvested": n_harvested_domains,
        "cited_url_reuse_ratio": (
            len(citation_url_list) / n_cited_urls if n_cited_urls else 0.0
        ),
        "cited_domain_diversity_ratio": (
            n_cited_domains / n_cited_urls if n_cited_urls else 0.0
        ),
        "cited_to_harvested_url_ratio": (
            n_cited_urls / n_harvested_urls if n_harvested_urls else 0.0
        ),
        "cited_to_harvested_domain_ratio": (
            n_cited_domains / n_harvested_domains if n_harvested_domains else 0.0
        ),
    }


def _domain(url: str) -> str:
    """Strip to bare hostname, lowercase, drop a leading 'www.'.

    URLs in findings are produced by tools (web_search, fetch_url) and
    are usually already canonical, but we still normalize so e.g.
    "www.nytimes.com" and "nytimes.com" don't double-count.
    """
    if not url:
        return ""
    try:
        host = urlparse(url).hostname or ""
    except (ValueError, TypeError):
        return ""
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host
