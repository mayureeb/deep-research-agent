"""Semantic Scholar paper search.

Returns structured paper records (title, abstract, authors, year, venue,
citation count, PDF URL). On sustained 429s, marks the tool unavailable
for the rest of the run so subsequent calls short-circuit to web_search
instead of stalling researchers.
"""
from __future__ import annotations

import threading
import time

import httpx
from pydantic import BaseModel


# Sticky back-off state shared across researchers in the same run.
_unavailable_until: float = 0.0
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
    """Test/REPL helper. Not called from production code."""
    global _unavailable_until
    with _unavailable_lock:
        _unavailable_until = 0.0


_UNAVAILABLE_MESSAGE = (
    "[search_papers temporarily unavailable for this run — use web_search instead]"
)


SS_BASE = "https://api.semanticscholar.org/graph/v1"
FIELDS = "paperId,title,abstract,authors,year,venue,citationCount,openAccessPdf,url"


class Paper(BaseModel):
    """One paper record from Semantic Scholar."""
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
    """Run one paper search query against Semantic Scholar."""
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
            return [Paper(paper_id="", title=f"[scholar error: {e}]", url="")]

        if r.status_code == 200:
            out: list[Paper] = []
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

        if r.status_code != 429:
            return [Paper(
                paper_id="", title=f"[scholar HTTP {r.status_code}]", url="",
            )]

        if attempt < max_retries:
            time.sleep(backoff_seconds * (2 ** attempt))

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
