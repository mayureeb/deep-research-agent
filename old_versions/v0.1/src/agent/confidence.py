"""Source-quality domain lookup.

`compute_source_quality(url)` returns a score in [0, 1] used as a soft
multiplier on the researcher's self-reported confidence at save_finding time:
    confidence = self_reported * source_quality
"""
from __future__ import annotations

from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Source-quality domain table.
# ---------------------------------------------------------------------------


# Peer-reviewed venues + paper repositories. Suffix-matched (so subdomains
# like "www.nature.com" or "europepmc.org" still match).
_PEER_REVIEWED_DOMAINS: tuple[str, ...] = (
    "arxiv.org",
    "biorxiv.org",
    "medrxiv.org",
    "semanticscholar.org",
    "nature.com",
    "science.org",
    "sciencedirect.com",
    "pubmed.ncbi.nlm.nih.gov",
    "ncbi.nlm.nih.gov",
    "europepmc.org",
    "ieee.org",
    "ieeexplore.ieee.org",
    "acm.org",
    "dl.acm.org",
    "springer.com",
    "link.springer.com",
    "wiley.com",
    "onlinelibrary.wiley.com",
    "plos.org",
    "journals.plos.org",
    "cell.com",
    "thelancet.com",
    "nejm.org",
    "bmj.com",
    "jamanetwork.com",
    "openreview.net",
)

# Reputable news / wire services / specialized tech reporting.
_NEWS_DOMAINS: tuple[str, ...] = (
    "nytimes.com",
    "washingtonpost.com",
    "wsj.com",
    "bbc.com",
    "bbc.co.uk",
    "reuters.com",
    "apnews.com",
    "theguardian.com",
    "npr.org",
    "ft.com",
    "economist.com",
    "bloomberg.com",
    "theatlantic.com",
    "newyorker.com",
    "technologyreview.com",
    "wired.com",
    "arstechnica.com",
    "theverge.com",
    "spectrum.ieee.org",
)

# Curated tertiary (encyclopedic, well-edited but not primary).
_CURATED_TERTIARY_DOMAINS: tuple[str, ...] = (
    "wikipedia.org",
    "scholarpedia.org",
)

# Blog hosts + Q&A sites + general user-generated content.
_BLOG_QA_DOMAINS: tuple[str, ...] = (
    "medium.com",
    "substack.com",
    "dev.to",
    "blogspot.com",
    "wordpress.com",
    "tumblr.com",
    "ghost.io",
    "stackoverflow.com",
    "stackexchange.com",
    "reddit.com",
    "quora.com",
    "news.ycombinator.com",
)


# Default-bucket scores. Tests pin these so a silent edit triggers a failure.
SQ_PEER_REVIEWED: float = 1.0
SQ_GOV_EDU: float = 1.0
SQ_NEWS: float = 0.85
SQ_CURATED_TERTIARY: float = 0.75
SQ_UNKNOWN: float = 0.7
SQ_BLOG_QA: float = 0.5


def compute_source_quality(url: str) -> float:
    """Return the source-quality score in [0, 1] for one URL.

    Pure-Python, no I/O. Lookup is by domain only.

    Bucketing precedence (first match wins):
      1. .gov / .edu                     → 1.0
      2. Peer-reviewed venue suffix      → 1.0
      3. Reputable news suffix           → 0.85
      4. Curated tertiary (Wikipedia)    → 0.75
      5. Blog hosts / Q&A                → 0.5
      6. Anything else                   → 0.7

    Returns SQ_UNKNOWN (0.7) on parse failure rather than raising.
    """
    domain = _extract_domain(url)
    if not domain:
        return SQ_UNKNOWN
    if domain.endswith(".gov") or domain == "gov":
        return SQ_GOV_EDU
    if domain.endswith(".edu") or domain == "edu":
        return SQ_GOV_EDU
    if _domain_matches_any(domain, _PEER_REVIEWED_DOMAINS):
        return SQ_PEER_REVIEWED
    if _domain_matches_any(domain, _NEWS_DOMAINS):
        return SQ_NEWS
    if _domain_matches_any(domain, _CURATED_TERTIARY_DOMAINS):
        return SQ_CURATED_TERTIARY
    if _domain_matches_any(domain, _BLOG_QA_DOMAINS):
        return SQ_BLOG_QA
    return SQ_UNKNOWN


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _extract_domain(url: str) -> str:
    """Lower-cased registered domain (host without port). Returns "" on parse failure."""
    if not url:
        return ""
    try:
        host = urlparse(url).hostname
        return host.lower() if host else ""
    except Exception:
        return ""


def _domain_matches_any(domain: str, candidates: tuple[str, ...]) -> bool:
    """True iff `domain` equals any candidate or is a subdomain of one.

    Suffix-with-dot avoids false positives like "evilnature.com" matching
    "nature.com".
    """
    for c in candidates:
        if domain == c or domain.endswith("." + c):
            return True
    return False
