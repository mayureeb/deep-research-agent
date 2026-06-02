"""Web search tool. Tavily by default; pluggable via the SearchProvider protocol.

Search exceptions are turned into a single SearchResult whose title carries
the error message — the researcher's prompt treats "0 useful results" as
"try a different query", so the loop stays simple.
"""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel
from tavily import TavilyClient


class SearchResult(BaseModel):
    """One search hit. Snippet is what the researcher sees; the URL is for
    downstream `fetch_url`."""
    title: str
    url: str
    snippet: str
    published_date: str | None = None


class SearchProvider(Protocol):
    """Contract for a web-search backend."""
    def search(self, query: str, k: int) -> list[SearchResult]: ...


class TavilySearch:
    """Tavily implementation of SearchProvider. Uses search_depth='basic';
    deeper retrieval happens via fetch_url on the actual pages."""

    def __init__(self, api_key: str):
        self._client = TavilyClient(api_key=api_key)

    def search(self, query: str, k: int = 8) -> list[SearchResult]:
        """Run one search query.

        Args:
            query: Free-text query string.
            k: Number of results to return.

        Returns:
            List of SearchResult, or single error-result on exception.
        """
        try:
            resp = self._client.search(
                query=query,
                max_results=k,
                search_depth="basic",
                include_answer=False,
            )
        except Exception as e:
            return [SearchResult(title=f"[search error: {e}]", url="", snippet="")]

        results = []
        for r in resp.get("results", []):
            # Truncate snippet to 500 chars; researcher should fetch_url for content.
            results.append(
                SearchResult(
                    title=r.get("title", ""),
                    url=r.get("url", ""),
                    snippet=r.get("content", "")[:500],
                    published_date=r.get("published_date"),
                )
            )
        return results


def tool_schema() -> dict:
    """Anthropic tool_use schema for web_search."""
    return {
        "name": "web_search",
        "description": (
            "Search the web for sources on a topic. Returns the top results with "
            "title, URL, and a short snippet. Use this to discover sources. "
            "If a query returns nothing useful, try a more specific or differently "
            "phrased query rather than repeating the same one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
            },
            "required": ["query"],
        },
    }
