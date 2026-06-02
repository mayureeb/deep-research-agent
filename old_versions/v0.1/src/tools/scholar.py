"""Semantic Scholar paper search. Free public API, no key required.

Resilience: bounded retry on 429s, then a sticky window that marks the tool
unavailable for the rest of the run so researchers route to web_search
instead of burning budget on a sustained limiter.
"""
from __future__ import annotations

import threading
import time

import httpx
from pydantic import BaseModel


# Module-level sticky-window state, shared across parallel researchers.
_unavailable_until: float = 0.0  # 0 = available
_unavailable_lock = threading.Lock()


def _scholar_is_unavailable() -> bool:
    """True if we're inside the sticky-back-off window."""
    return time.monotonic() < _unavailable_until


def _mark_scholar_unavailable(window_seconds: int) -> None:
    """Set the sticky-back-off window."""
    global _unavailable_until
    with _unavailable_lock:
        _unavailable_until = time.monotonic() + window_seconds


def _reset_unavailable() -> None:
    """Test/REPL helper."""
    global _unavailable_until
    with _unavailable_lock:
        _unavailable_until = 0.0


# Directive phrased so the researcher LLM routes to web_search instead of
# retrying with a different query.
_UNAVAILABLE_MESSAGE = (
    "[search_papers temporarily unavailable for this run — use web_search instead]"
)


# Semantic Scholar API endpoint + the fields we want returned.
SS_BASE = "https://api.semanticscholar.org/graph/v1"
FIELDS = "paperId,title,abstract,authors,year,venue,citationCount,openAccessPdf,url"


class Paper(BaseModel):
    """One paper record from Semantic Scholar.

    Attributes:
        paper_id: Semantic Scholar's stable id; "" on error results.
        title: Paper title.
        abstract: First 500 chars of the abstract.
        authors: First 6 author names.
        year, venue, citation_count: Standard bibliographic fields.
        pdf_url: Open-access PDF URL if available.
        url: Semantic Scholar page URL.
    """
    paper_id: str
    title: str
    abstract: str = ""
    authors: list[str] = []
    year: int | None = None
    venue: str = ""
    citation_count: int = 0
    pdf_url: str | None = None
    url: str = ""


def search_papers(
    query: str,
    k: int = 8,
    timeout: float = 15.0,
    max_retries: int = 0,
    backoff_seconds: float = 1.0,
    unavailable_window_seconds: int = 120,
) -> list[Paper]:
    """Run one paper search query against Semantic Scholar.

    Args:
        query: Free-text query.
        k: Number of results.
        timeout: Per-request timeout.
        max_retries: Retries on HTTP 429 only.
        backoff_seconds: Base sleep between retries; doubles each attempt.
        unavailable_window_seconds: Sticky window after retries exhausted on 429.

    Returns:
        List of Paper records, or single error-result on exception, non-200,
        or sticky-back-off window.
    """
    # Sticky-window short-circuit — no HTTP roundtrip while the limiter is sustained.
    if _scholar_is_unavailable():
        return [Paper(paper_id="", url="", title=_UNAVAILABLE_MESSAGE)]

    for attempt in range(max_retries + 1):
        try:
            r = httpx.get(
                f"{SS_BASE}/paper/search",
                params={"query": query, "limit": k, "fields": FIELDS},
                timeout=timeout,
                headers={"User-Agent": "deep-research-agent/0.1"},
            )
        except Exception as e:
            # Transport errors fail fast — surface as error-result like TavilySearch.
            return [Paper(paper_id="", title=f"[scholar error: {e}]", url="")]

        if r.status_code == 200:
            out: list[Paper] = []
            # Defensive slice; the API usually honors `limit` but not always.
            for item in (r.json().get("data") or [])[:k]:
                out.append(Paper(
                    paper_id=item.get("paperId") or "",
                    title=item.get("title") or "",
                    abstract=(item.get("abstract") or "")[:500],
                    authors=[a.get("name", "") for a in (item.get("authors") or [])][:6],
                    year=item.get("year"),
                    venue=item.get("venue") or "",
                    citation_count=item.get("citationCount") or 0,
                    pdf_url=(item.get("openAccessPdf") or {}).get("url"),
                    url=item.get("url") or "",
                ))
            return out

        # Only retry on 429; other non-200s won't recover from a sleep.
        if r.status_code != 429:
            return [Paper(
                paper_id="", title=f"[scholar HTTP {r.status_code}]", url="",
            )]

        # Skip the sleep on the final attempt — about to flip the sticky flag.
        if attempt < max_retries:
            time.sleep(backoff_seconds * (2 ** attempt))

    # Retries exhausted on a sustained 429.
    _mark_scholar_unavailable(unavailable_window_seconds)
    return [Paper(paper_id="", url="", title=_UNAVAILABLE_MESSAGE)]


def tool_schema() -> dict:
    """Anthropic tool_use schema for search_papers."""
    return {
        "name": "search_papers",
        "description": (
            "Search peer-reviewed and academic papers via Semantic Scholar. "
            "PREFER THIS over web_search when the sub-question is technical, "
            "scientific, or asks about prior research. Returns structured records "
            "with title, abstract, authors, year, venue, citation count, and "
            "(when available) an open-access PDF URL.\n\n"
            "Use the citation_count to gauge influence — papers with hundreds "
            "of citations are usually more foundational. Use the abstract to "
            "decide whether to fetch_url on the pdf_url for full text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The paper search query"},
            },
            "required": ["query"],
        },
    }
