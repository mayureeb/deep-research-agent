"""Re-fetch verifier.

For each cited finding, re-fetches the source URL and judges:
  - substring match → verified
  - significant token overlap → drifted
  - low overlap → fabricated
  - fetch failed → unreachable
"""
from __future__ import annotations

import asyncio
import re

from .. import events
from ..config import Config
from ..state import Finding, VerifyReport, VerifyResult
from ..tools.fetch import FetchContext, fetch as fetch_url


DRIFT_OVERLAP_THRESHOLD: float = 0.5
MIN_QUOTE_CHARS_FOR_SUBSTRING: int = 30
MIN_TOKEN_LEN: int = 4


_STOPWORDS: frozenset[str] = frozenset({
    "this", "that", "these", "those", "with", "from", "into", "onto",
    "their", "there", "where", "which", "while", "would", "could",
    "should", "about", "above", "below", "after", "before", "between",
    "because", "however", "therefore", "thus", "also", "very", "much",
    "many", "such", "some", "more", "most", "less", "least",
    "have", "been", "being", "than", "then", "when", "what", "whose",
    "they", "them", "your", "yours", "ours", "theirs",
})


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


async def verify_cited(
    cfg: Config,
    findings_store_findings: list[Finding],
    cited_indices: list[int],
    fetch_ctx: FetchContext | None = None,
    cached: dict[int, VerifyResult] | None = None,
) -> VerifyReport:
    """Re-fetch the cited findings' source URLs in parallel and judge each.

    Args:
        cfg: Config — uses parallel_researchers for concurrency.
        findings_store_findings: The full FindingsStore.findings list, so
            we can resolve `cited_indices` to actual Finding objects.
        cited_indices: The indices the writer cited (deduped + sorted by
            the orchestrator). Only these get verified.
        fetch_ctx: See verify_findings.
        cached: See verify_findings. Mutated on return with new verdicts.

    Returns:
        VerifyReport with one VerifyResult per cited_indices entry, in
        cited_indices order. Cached entries short-circuit (no re-fetch).
    """
    cache = cached if cached is not None else {}
    sem = asyncio.Semaphore(cfg.parallel_researchers)

    # Partition: which indices need a fresh fetch vs. served from cache?
    to_fetch: list[int] = []
    for idx in cited_indices:
        if idx not in cache:
            to_fetch.append(idx)

    events.orch(
        f"verifier: {len(cited_indices)} cited "
        f"({len(to_fetch)} fresh, {len(cited_indices) - len(to_fetch)} cached)"
    )

    async def _verify(idx: int) -> tuple[int, VerifyResult]:
        if not (0 <= idx < len(findings_store_findings)):
            return idx, VerifyResult(
                finding_index=idx, verdict="unreachable",
                detail="finding index out of range",
                fetch_status="error",
            )
        finding = findings_store_findings[idx]
        async with sem:
            return idx, await asyncio.to_thread(_verify_sync, idx, finding, fetch_ctx)

    results = await asyncio.gather(
        *(_verify(i) for i in to_fetch),
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, Exception):
            events.orch(f"  ⚠ verifier task raised: {r}")
            continue
        idx, verdict = r
        cache[idx] = verdict

    out: list[VerifyResult] = []
    for idx in cited_indices:
        if idx in cache:
            out.append(cache[idx])
        else:
            out.append(VerifyResult(
                finding_index=idx, verdict="unreachable",
                detail="verifier task did not produce a verdict",
                fetch_status="error",
            ))
    return VerifyReport(results=out)


# ---------------------------------------------------------------------------
# Per-finding verification (synchronous, the actual judgment).
# ---------------------------------------------------------------------------


def _verify_sync(
    finding_index: int,
    finding: Finding,
    fetch_ctx: FetchContext | None,
) -> VerifyResult:
    """Re-fetch one finding's URL and return its verdict.

    Pipeline:
      1. fetch(finding.source_url, ctx=fetch_ctx) → FetchResult.
      2. If not is_usable → "unreachable".
      3. Else: check live body for cached evidence:
           a. normalized substring match → "verified"
           b. significant token-overlap >= DRIFT_OVERLAP_THRESHOLD → "drifted"
           c. otherwise → "fabricated"
    """
    try:
        result = fetch_url(finding.source_url, ctx=fetch_ctx)
    except Exception as e:
        return VerifyResult(
            finding_index=finding_index,
            verdict="unreachable",
            detail=f"refetch raised: {e}",
            fetch_status="error",
        )

    if not result.is_usable:
        return VerifyResult(
            finding_index=finding_index,
            verdict="unreachable",
            detail=f"refetch status={result.status}: {result.error or '(no detail)'}",
            fetch_status=result.status,
        )

    quote = finding.evidence
    body = result.text

    if _quote_present(quote, body):
        return VerifyResult(
            finding_index=finding_index,
            verdict="verified",
            fetch_status=result.status,
        )

    overlap = _significant_token_overlap(quote, body)
    if overlap >= DRIFT_OVERLAP_THRESHOLD:
        return VerifyResult(
            finding_index=finding_index,
            verdict="drifted",
            detail=f"quote not present verbatim; {overlap:.0%} significant-token overlap",
            fetch_status=result.status,
        )

    return VerifyResult(
        finding_index=finding_index,
        verdict="fabricated",
        detail=f"quote not in live page; only {overlap:.0%} significant-token overlap",
        fetch_status=result.status,
    )


# ---------------------------------------------------------------------------
# Quote-vs-page heuristics.
# ---------------------------------------------------------------------------


def _quote_present(quote: str, body: str) -> bool:
    """True iff the cached evidence quote appears in the live body.

    Tries normalized substring match, then a 60-char prefix substring for
    long quotes.
    """
    nq = _normalize_for_match(quote)
    nb = _normalize_for_match(body)
    if not nq:
        return False
    if nq in nb:
        return True
    if len(nq) >= 2 * MIN_QUOTE_CHARS_FOR_SUBSTRING and nq[:60] in nb:
        return True
    return False


def _significant_token_overlap(quote: str, body: str) -> float:
    """Fraction of the quote's significant tokens (long, non-stopword) that
    appear in the body."""
    qtokens = {
        t for t in _tokenize(quote)
        if len(t) >= MIN_TOKEN_LEN and t not in _STOPWORDS
    }
    if not qtokens:
        return 0.0
    body_tokens = set(_tokenize(body))
    return len(qtokens & body_tokens) / len(qtokens)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


_NORMALIZE_WS_RE = re.compile(r"\s+")
_TOKENIZE_RE = re.compile(r"[a-z0-9]+")


def _normalize_for_match(s: str) -> str:
    """Lowercase + collapse whitespace; preserves punctuation."""
    return _NORMALIZE_WS_RE.sub(" ", s.lower()).strip()


def _tokenize(s: str) -> list[str]:
    """Split on non-alphanumeric. Lowercase. Used by the overlap heuristic
    only — we don't need to preserve punctuation here, just the words."""
    return _TOKENIZE_RE.findall(s.lower())
