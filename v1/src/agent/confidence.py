"""Confidence machinery: source-quality, agreement, three-axis combine.

Three axes per Finding, each in [0, 1]:
  1. self-reported (Finding.confidence)
  2. source-quality (domain-table lookup)
  3. agreement (cross-source corroboration)

Combined as weighted mean; an axis-disagreement flag surfaces wide
spreads to the critic.
"""
from __future__ import annotations

from urllib.parse import urlparse


# Peer-reviewed venues + paper repositories.
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
    "nature.com/news",
)

_CURATED_TERTIARY_DOMAINS: tuple[str, ...] = (
    "wikipedia.org",
    "stanford.edu",
    "scholarpedia.org",
)

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
    "hacker-news.firebaseapp.com",
    "news.ycombinator.com",
)


SQ_PEER_REVIEWED: float = 1.0
SQ_GOV_EDU: float = 1.0
SQ_NEWS: float = 0.85
SQ_CURATED_TERTIARY: float = 0.75
SQ_UNKNOWN: float = 0.7
SQ_BLOG_QA: float = 0.5


def compute_source_quality(url: str) -> float:
    """Return the source-quality score in [0, 1] for one URL.

    Bucketing precedence (first match wins): .gov/.edu (1.0), peer-reviewed
    (1.0), news (0.85), curated tertiary (0.75), blog/Q&A (0.5), unknown (0.7).
    Returns SQ_UNKNOWN on unparseable URLs.
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


SOURCE_QUALITY_TIERS: tuple[str, ...] = (
    "gov_edu", "peer_reviewed", "news", "curated_tertiary", "unknown", "blog_qa",
)


def source_quality_tier(url: str) -> str:
    """Return the source-quality tier name for one URL.

    Same precedence as `compute_source_quality`, but returns a string label
    from `SOURCE_QUALITY_TIERS`.
    """
    domain = _extract_domain(url)
    if not domain:
        return "unknown"
    if domain.endswith(".gov") or domain == "gov":
        return "gov_edu"
    if domain.endswith(".edu") or domain == "edu":
        return "gov_edu"
    if _domain_matches_any(domain, _PEER_REVIEWED_DOMAINS):
        return "peer_reviewed"
    if _domain_matches_any(domain, _NEWS_DOMAINS):
        return "news"
    if _domain_matches_any(domain, _CURATED_TERTIARY_DOMAINS):
        return "curated_tertiary"
    if _domain_matches_any(domain, _BLOG_QA_DOMAINS):
        return "blog_qa"
    return "unknown"


def compute_agreement(
    finding_index: int,
    finding_subq_id: str,
    all_findings: list,
    contradictions: list,
) -> float:
    """Return the agreement signal in [0, 1] for one finding.

    Among same-subquestion peers, agreement = supporting / total_peers,
    where supporting = peers - those flagged in a Contradiction with this
    finding. Returns 1.0 (neutral) when there are no peers.
    """
    peer_indices = [
        i for i, f in enumerate(all_findings)
        if i != finding_index and f.subquestion_id == finding_subq_id
    ]
    if not peer_indices:
        return 1.0

    contradicting: set[int] = set()
    for c in contradictions:
        ids = set(c.finding_ids)
        if finding_index in ids:
            for pi in peer_indices:
                if pi in ids:
                    contradicting.add(pi)

    supporting = max(0, len(peer_indices) - len(contradicting))
    return min(1.0, max(0.0, supporting / len(peer_indices)))


W_SELF: float = 0.40
W_SOURCE_QUALITY: float = 0.35
W_AGREEMENT: float = 0.25

DISAGREEMENT_THRESHOLD: float = 0.35


def combine_axes(
    self_reported: float,
    source_quality: float,
    agreement: float,
) -> tuple[float, bool]:
    """Combine the three axes into (combined, disagreement).

    combined: weighted mean clipped to [0, 1].
    disagreement: True iff max(axes) - min(axes) >= DISAGREEMENT_THRESHOLD.
    """
    s = _clip01(self_reported)
    q = _clip01(source_quality)
    a = _clip01(agreement)
    combined = W_SELF * s + W_SOURCE_QUALITY * q + W_AGREEMENT * a
    combined = _clip01(combined)
    disagreement = (max(s, q, a) - min(s, q, a)) >= DISAGREEMENT_THRESHOLD
    return combined, disagreement


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _extract_domain(url: str) -> str:
    """Lower-cased host from a URL, or "" on parse failure."""
    if not url:
        return ""
    try:
        host = urlparse(url).hostname
        return host.lower() if host else ""
    except Exception:
        return ""


def _domain_matches_any(domain: str, candidates: tuple[str, ...]) -> bool:
    """True iff `domain` equals any candidate or is a subdomain of one."""
    for c in candidates:
        if domain == c or domain.endswith("." + c):
            return True
    return False


def _clip01(x: float) -> float:
    """Clamp to [0.0, 1.0]."""
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x
