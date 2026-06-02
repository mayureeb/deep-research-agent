"""Fetch and clean a URL with typed FetchResult and per-domain failure memory.

Returns a FetchResult whose `status` enum (ok / paywall / blocked /
not_found / timeout / rate_limited / non_html / error) lets callers
distinguish "no evidence" from "made up evidence". Includes pre-GET URL
validation and a paywall / bot-detection scan on the extracted body.
"""
from __future__ import annotations

import ipaddress
from typing import Literal
from urllib.parse import urlparse

import httpx
import trafilatura
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Status enum + typed result.
# ---------------------------------------------------------------------------


# All fetch outcomes, typed. Researcher dispatch consumes this; the only
# status the researcher should treat as evidence is `ok`.
FetchStatus = Literal[
    "ok",            # extracted body, no paywall/bot markers
    "paywall",       # body extracted but reads as paywall content
    "blocked",       # bot-detection / captcha challenge
    "not_found",     # HTTP 404
    "timeout",       # network timeout
    "rate_limited",  # HTTP 429
    "non_html",      # PDF/binary/empty body that trafilatura can't extract
    "error",         # everything else (DNS, SSL, unparseable, etc.)
]


# Paywall keyword set. Conservative — false positives waste evidence; false
# negatives leak paywall text into findings. Keep these as PHRASES (not
# single words) to keep the false-positive rate down.
_PAYWALL_PHRASES = (
    "subscribe to read",
    "subscribe to continue",
    "subscribe for full access",
    "sign in to read",
    "log in to read",
    "log in to continue",
    "create a free account to continue",
    "this article is for subscribers",
    "subscribers only",
    "members only",
    "premium content",
    "register to continue",
    "you've reached your free article limit",
    "to continue reading",
    "support our journalism",
)

# Bot-detection / challenge keywords. Cloudflare, Akamai, hCaptcha, etc.
_BOT_PHRASES = (
    "verify you are human",
    "verify you're human",
    "are you a robot",
    "complete the security check",
    "checking if the site connection is secure",
    "cloudflare ray id",
    "please enable javascript and cookies to continue",
    "press and hold to confirm",
    "please solve the captcha",
    "access denied",
)


class FetchResult(BaseModel):
    """Cleaned result of one URL fetch with a typed status enum."""
    url: str
    status: FetchStatus
    title: str = ""
    text: str = ""
    error: str | None = None

    @property
    def is_usable(self) -> bool:
        """True iff this result is suitable as evidence for save_finding."""
        return self.status == "ok"


# ---------------------------------------------------------------------------
# Per-run state: domain failure memory.
# ---------------------------------------------------------------------------


class FetchContext(BaseModel):
    """Per-run state for the fetch tool. Tracks per-domain failures.

    A domain that hits ``failure_threshold`` failures within one run is
    refused for the rest of that run.
    """
    domain_failures: dict[str, int] = Field(default_factory=dict)
    failed_domains: set[str] = Field(default_factory=set)
    failure_threshold: int = 3

    def record_failure(self, url: str) -> None:
        """Increment failure count for the URL's domain. Promotes to
        failed_domains when the threshold is hit."""
        domain = _extract_domain(url)
        if not domain:
            return
        self.domain_failures[domain] = self.domain_failures.get(domain, 0) + 1
        if self.domain_failures[domain] >= self.failure_threshold:
            self.failed_domains.add(domain)

    def is_blocked(self, url: str) -> bool:
        """True iff this URL's domain has hit the failure threshold."""
        domain = _extract_domain(url)
        return bool(domain) and domain in self.failed_domains


# ---------------------------------------------------------------------------
# Fetch.
# ---------------------------------------------------------------------------


def fetch(
    url: str,
    max_chars: int = 12_000,
    timeout: float = 15.0,
    ctx: FetchContext | None = None,
) -> FetchResult:
    """Fetch one URL and return a typed FetchResult."""
    parse_err = _validate_url(url)
    if parse_err:
        return FetchResult(
            url=url, status="error", error=parse_err,
        )

    if ctx is not None and ctx.is_blocked(url):
        return FetchResult(
            url=url, status="blocked",
            error=f"domain blocked after {ctx.failure_threshold} prior failures in this run",
        )

    try:
        r = httpx.get(url, timeout=timeout, follow_redirects=True, headers={
            "User-Agent": "deep-research-agent/1.0 (+research)",
        })
    except httpx.TimeoutException as e:
        if ctx is not None:
            ctx.record_failure(url)
        return FetchResult(url=url, status="timeout", error=str(e))
    except Exception as e:
        if ctx is not None:
            ctx.record_failure(url)
        return FetchResult(url=url, status="error", error=f"fetch failed: {e}")

    if r.status_code == 404:
        if ctx is not None:
            ctx.record_failure(url)
        return FetchResult(url=url, status="not_found",
                           error="HTTP 404 — page does not exist")
    if r.status_code == 429:
        if ctx is not None:
            ctx.record_failure(url)
        return FetchResult(url=url, status="rate_limited",
                           error="HTTP 429 — rate limited; back off")
    if r.status_code in (401, 403):
        if ctx is not None:
            ctx.record_failure(url)
        return FetchResult(url=url, status="blocked",
                           error=f"HTTP {r.status_code} — auth/bot block")
    if r.status_code >= 400:
        if ctx is not None:
            ctx.record_failure(url)
        return FetchResult(url=url, status="error",
                           error=f"HTTP {r.status_code}")

    try:
        extracted = trafilatura.extract(
            r.text,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        ) or ""
        meta = trafilatura.extract_metadata(r.text)
        title = meta.title if meta and meta.title else ""
    except Exception as e:
        if ctx is not None:
            ctx.record_failure(url)
        return FetchResult(url=url, status="error",
                           error=f"extract failed: {e}")

    if not extracted.strip():
        return FetchResult(
            url=url, status="non_html", title=title,
            error="no readable text — likely JS-rendered, PDF, or empty",
        )

    body = extracted[:max_chars]

    body_lc = body.lower()
    if _matches_any(body_lc, _PAYWALL_PHRASES):
        return FetchResult(
            url=url, status="paywall", title=title,
            error="page body contains paywall markers — content not usable",
        )
    if _matches_any(body_lc, _BOT_PHRASES):
        return FetchResult(
            url=url, status="blocked", title=title,
            error="page body contains bot-detection / challenge markers",
        )

    return FetchResult(
        url=url,
        status="ok",
        title=title,
        text=body,
    )


# ---------------------------------------------------------------------------
# Local URL validation.
# ---------------------------------------------------------------------------


def _validate_url(url: str) -> str | None:
    """Pre-GET URL validation. Returns an error string if the URL is bad,
    else None.

    Checks (in order):
      1. Non-empty, parseable.
      2. Scheme is http or https.
      3. Hostname present.
      4. If hostname is a literal IP: not RFC1918 / loopback / link-local.
    """
    if not url or not isinstance(url, str):
        return "invalid URL: empty or non-string"
    try:
        parsed = urlparse(url)
    except Exception as e:
        return f"invalid URL: parse failed: {e}"

    if parsed.scheme not in ("http", "https"):
        return f"invalid URL scheme: {parsed.scheme!r} (only http/https allowed)"

    host = parsed.hostname
    if not host:
        return "invalid URL: missing host"

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None

    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
        return f"refusing internal IP: {host}"
    return None


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _matches_any(haystack_lc: str, phrases: tuple[str, ...]) -> bool:
    """True if any of the phrases (assumed already lowercase) appears in
    haystack. Used by the paywall + bot-detection heuristics."""
    return any(p in haystack_lc for p in phrases)


def _extract_domain(url: str) -> str | None:
    """Pull the host (without port) out of a URL. Subdomains are not stripped."""
    if not url:
        return None
    try:
        host = urlparse(url).hostname
        return host.lower() if host else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Tool schema.
# ---------------------------------------------------------------------------


def tool_schema() -> dict:
    """Anthropic tool_use schema for fetch_url."""
    return {
        "name": "fetch_url",
        "description": (
            "Fetch a URL and return its cleaned main-text content (no HTML, no nav). "
            "Use this AFTER finding a promising result via web_search.\n\n"
            "The result has a STATUS:\n"
            "- 'ok'           → real content, safe to extract a finding\n"
            "- 'paywall'      → body is paywall text; do NOT save a finding\n"
            "- 'blocked'      → bot-detection / 401-403; do NOT save a finding\n"
            "- 'not_found'    → HTTP 404; pick another source\n"
            "- 'rate_limited' → HTTP 429; pick a different domain\n"
            "- 'non_html'     → PDF / binary / empty; can't extract text\n"
            "- 'timeout' / 'error' → transport failure; pick another source\n\n"
            "Only save_finding from `ok` results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
            },
            "required": ["url"],
        },
    }
