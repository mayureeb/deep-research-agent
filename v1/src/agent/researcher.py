"""Researcher sub-agent: a tool-using loop scoped to one sub-question.

The orchestrator spawns one per SubQuestion and gathers them under a
semaphore. Each researcher owns a fresh context window, has 5 tools
(web_search, search_papers, fetch_url, save_finding, note_uncertainty),
and is bounded by per-researcher caps on tool calls and findings.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

from .. import events
from ..config import Config
from ..llm import chat_with_tools
from ..state import Finding, SubQuestion, UncertaintyNote


@dataclass
class ResearcherResult:
    """One researcher's full output bundle."""
    findings: list[Finding] = field(default_factory=list)
    uncertainty: list[UncertaintyNote] = field(default_factory=list)
    tool_counts: dict[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    trace_log: list = field(default_factory=list)
from ..tools import findings as findings_tool
from ..tools import search as search_tool
from ..tools import fetch as fetch_tool
from ..tools import scholar as scholar_tool
from ..tools.search import TavilySearch
from ..tools.fetch import fetch as fetch_url, FetchContext
from ..tools.scholar import search_papers
from .confidence import compute_source_quality
from .trace import TraceController, TraceStep


SYSTEM = """You are a research sub-agent. You are assigned ONE specific sub-question and your job is to find concrete, sourced answers to it — and ONLY it.

Tools available:
- web_search: general web. Good for current/practical/blog-style sources.
- search_papers: peer-reviewed academic search via Semantic Scholar. PREFER THIS for technical/scientific sub-questions or when asked about "research", "studies", "evidence", "literature".
- fetch_url: read the full text of a specific URL.
- save_finding: record one claim with verbatim evidence.
- note_uncertainty: record explicitly that you searched but couldn't find concrete evidence on a topic. USE THIS rather than silently returning fewer findings.

Process — extract as you go, do not gather then extract:

1. Start with ONE search call (web_search or search_papers per the tool descriptions).
2. From results, fetch_url ONE promising source.
3. CHECK the fetch status BEFORE deciding what to do:
   - status='ok'           → real content; proceed to step 4.
   - status='paywall'      → body is paywall text (e.g. "Subscribe to read"). Do NOT save a finding. Go to step 4 (note_uncertainty) or pick another source.
   - status='blocked'      → bot-detection / 401-403. Do NOT save a finding. Pick another source.
   - status='not_found'    → HTTP 404. Pick another source.
   - status='rate_limited' → HTTP 429. Pick a DIFFERENT DOMAIN (don't retry).
   - status='non_html'     → PDF / binary / empty body. Can't extract text; pick another source.
   - status='timeout' / 'error' → transport failure. Pick another source.
4. IMMEDIATELY after that fetch returns, your next action MUST be either:
     - save_finding — if status is `ok` AND the page contains any quotable claim that answers ANY part of the sub-question, OR
     - note_uncertainty — if the page does not contain a usable claim (any non-`ok` status, or `ok` but contentless/off-topic).
   Do NOT call web_search, search_papers, or fetch_url again until you have saved or noted uncertainty about the page you just read.
5. Repeat from step 1 with a new query or source until you have enough findings.

Compound sub-questions: if the question has multiple clauses (e.g. "X and Y", "methods AND scalability"), save SEPARATE findings for each clause as you encounter them. Do NOT wait for one source that covers everything — that source usually does not exist.

Stop when: you have at least {min_findings} solid findings, OR you've made {max_calls} tool calls.

Quality bar:
- A finding may answer only PART of the sub-question — that's fine and expected.
- NEVER save a finding from a fetch result whose status is not `ok`.
- Each finding must be grounded in a verbatim quote from the source you just read.
- Prefer DIVERSE sources — don't save 3 findings from the same article.
- Be HONEST about confidence. If a source hedges, your finding should hedge.
- If the literature disagrees, save findings from BOTH sides.
- An honest note_uncertainty is BETTER than fabricating a finding.

When done, respond with a final text message summarizing what you found (not a tool call)."""


def _meets_multi_signal_stop(
    findings: list[Finding], cfg: Config
) -> tuple[bool, str]:
    """Multi-signal stop check. Returns (met, why_not).

    met=True iff: findings >= min_findings_to_stop, distinct sources >=
    min_unique_sources, and mean confidence >= min_avg_confidence.
    """
    n = len(findings)
    if n < cfg.researcher_min_findings_to_stop:
        return False, "need_more_findings"
    unique = len({f.source_url for f in findings})
    if unique < cfg.researcher_min_unique_sources:
        return False, "need_more_sources"
    avg_conf = sum(f.confidence for f in findings) / n
    if avg_conf < cfg.researcher_min_avg_confidence:
        return False, "low_confidence"
    return True, ""


def tools_for_researcher() -> list[dict]:
    """Return the 5-tool schema list passed to chat_with_tools."""
    return [
        search_tool.tool_schema(),
        scholar_tool.tool_schema(),
        fetch_tool.tool_schema(),
        findings_tool.tool_schema(),
        findings_tool.uncertainty_tool_schema(),
    ]


async def run_researcher(
    cfg: Config,
    subq: SubQuestion,
    semaphore: asyncio.Semaphore,
    fetch_ctx: FetchContext | None = None,
) -> ResearcherResult:
    """Run one researcher loop. Acquires the semaphore and runs the loop in a thread."""
    async with semaphore:
        return await asyncio.to_thread(_run_sync, cfg, subq, fetch_ctx)


def _run_sync(
    cfg: Config,
    subq: SubQuestion,
    fetch_ctx: FetchContext | None,
) -> ResearcherResult:
    """The synchronous tool-using loop."""
    researcher_id = f"r-{uuid.uuid4().hex[:6]}"
    events.researcher(researcher_id, subq.id, "starting")
    searcher = TavilySearch(cfg.tavily_api_key)
    started_at = time.monotonic()
    tool_counts: dict[str, int] = {}
    trace_controller: TraceController | None = (
        TraceController(cfg) if cfg.trace_enabled else None
    )
    trace_log: list[TraceStep] = []

    system = SYSTEM.format(
        min_findings=cfg.researcher_min_findings_to_stop,
        max_calls=cfg.researcher_max_tool_calls,
    )
    pref = subq.preferred_search
    pref_block = (
        f"\n\nPlanner's preferred search tool for this sub-question: "
        f"{pref} (use as a hint; both web_search and search_papers remain "
        f"available)."
        if pref != "either" else ""
    )
    user = (
        f"Sub-question: {subq.question}\n\n"
        f"Rationale (why this matters in the broader research): {subq.rationale}"
        f"{pref_block}"
    )
    messages: list[dict] = [{"role": "user", "content": user}]
    tools = tools_for_researcher()
    findings: list[Finding] = []
    uncertainty: list[UncertaintyNote] = []
    tool_calls = 0
    last_finding_at = -1

    while tool_calls < cfg.researcher_max_tool_calls:
        if len(findings) >= cfg.researcher_max_findings:
            break

        if trace_controller is not None:
            resp, step = trace_controller.propose(
                cfg.anthropic_api_key, cfg.researcher_model,
                system, messages, tools,
                step_idx=len(trace_log),
                n_findings_saved=len(findings),
            )
            trace_log.append(step)
            if resp.stop_reason == "end_turn":
                tool_calls += step.k_used
            else:
                tool_calls += (step.k_used - 1)
        else:
            resp = chat_with_tools(
                cfg.anthropic_api_key, cfg.researcher_model, system, messages, tools
            )

        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "end_turn":
            break

        findings_before = len(findings)
        tool_results = []
        any_tool_use = False
        for block in resp.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            any_tool_use = True
            tool_calls += 1
            tool_counts[block.name] = tool_counts.get(block.name, 0) + 1
            try:
                result_str = _dispatch_tool(
                    block.name, block.input, searcher, cfg, subq,
                    researcher_id, findings, uncertainty, fetch_ctx,
                )
            except Exception as e:
                result_str = f"[tool error: {e}]"
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_str,
            })

        if not any_tool_use:
            break

        messages.append({"role": "user", "content": tool_results})

        if len(findings) > findings_before:
            last_finding_at = tool_calls

        unproductive = False
        unproductive_reason = ""
        if (
            tool_calls >= cfg.researcher_unproductive_call_limit
            and len(findings) == 0
        ):
            unproductive = True
            unproductive_reason = (
                f"Made {tool_calls} tool calls without extracting concrete "
                f"evidence. Search results, fetched pages, or paper abstracts "
                f"did not yield specific claims supportable as findings."
            )
        elif (
            len(findings) > 0
            and last_finding_at >= 0
            and (tool_calls - last_finding_at) >= cfg.researcher_unproductive_call_limit
        ):
            _, blocker = _meets_multi_signal_stop(findings, cfg)
            unproductive = True
            unproductive_reason = (
                f"Made {tool_calls - last_finding_at} tool calls since the last "
                f"finding (current total {len(findings)}). "
                + (
                    f"Multi-signal soft-stop is blocked by: {blocker}. "
                    if blocker else ""
                ) +
                "Additional budget unlikely to produce more usable evidence."
            )
        if unproductive:
            n = UncertaintyNote(
                subquestion_id=subq.id,
                topic=subq.question,
                reason=(
                    unproductive_reason +
                    " Researcher exited early to free budget for other sub-questions."
                ),
                researcher_id=researcher_id,
            )
            uncertainty.append(n)
            events.researcher(
                researcher_id, subq.id,
                f"early-exit: {tool_calls} calls, {len(findings)} findings — recorded uncertainty",
            )
            break

        met, _ = _meets_multi_signal_stop(findings, cfg)
        if met and tool_calls >= cfg.researcher_max_tool_calls // 2:
            break

    elapsed = time.monotonic() - started_at
    events.researcher(
        researcher_id, subq.id,
        f"done — {len(findings)} findings, {len(uncertainty)} uncertainty notes, "
        f"{tool_calls} tool calls, {elapsed:.1f}s",
    )
    return ResearcherResult(
        findings=findings,
        uncertainty=uncertainty,
        tool_counts=tool_counts,
        elapsed_seconds=elapsed,
        trace_log=trace_log,
    )


def _dispatch_tool(
    name: str,
    args: dict,
    searcher: TavilySearch,
    cfg: Config,
    subq: SubQuestion,
    researcher_id: str,
    findings: list[Finding],
    uncertainty: list[UncertaintyNote],
    fetch_ctx: FetchContext | None,
) -> str:
    """Dispatch one tool_use call. Returns the string for the tool_result."""
    if name == "web_search":
        query = args["query"]
        results = searcher.search(query, cfg.search_results_per_query)
        events.researcher(
            researcher_id, subq.id,
            f"web_search: '{query[:60]}' → {len(results)} results",
        )
        if not results:
            return "[no results]"
        return "\n\n".join(
            f"[{i}] {r.title}\nURL: {r.url}\n{r.snippet}"
            for i, r in enumerate(results)
        )

    if name == "search_papers":
        query = args["query"]
        papers = search_papers(
            query, cfg.search_results_per_query,
            max_retries=cfg.scholar_max_retries,
            backoff_seconds=cfg.scholar_backoff_seconds,
            unavailable_window_seconds=cfg.scholar_unavailable_window_seconds,
        )
        events.researcher(
            researcher_id, subq.id,
            f"search_papers: '{query[:60]}' → {len(papers)} papers",
        )
        if not papers:
            return "[no papers found]"
        return "\n\n".join(
            f"[{i}] {p.title}\n"
            f"  authors: {', '.join(p.authors[:4])}"
            f"{' et al.' if len(p.authors) > 4 else ''}\n"
            f"  venue: {p.venue or '?'} ({p.year or '?'})  "
            f"citations: {p.citation_count}\n"
            f"  url: {p.url}\n"
            f"  pdf: {p.pdf_url or '(no open PDF)'}\n"
            f"  abstract: {p.abstract[:300]}{'...' if len(p.abstract) > 300 else ''}"
            for i, p in enumerate(papers)
        )

    if name == "fetch_url":
        url = args["url"]
        result = fetch_url(url, cfg.fetch_max_chars, ctx=fetch_ctx)
        events.researcher(
            researcher_id, subq.id,
            f"fetch: {url[:50]}... [{result.status}]"
            + (f" ({len(result.text)} chars)" if result.is_usable else "")
        )

        if not result.is_usable:
            return (
                f"[fetch status={result.status}] {result.error or '(no detail)'}"
            )

        return (
            f"[fetch status=ok]\n"
            f"TITLE: {result.title}\n\n{result.text}"
        )

    if name == "save_finding":
        evidence_str = args["evidence"]
        source_url = args["source_url"]
        domain = (urlparse(source_url).hostname or "").lower()
        content_hash = hashlib.sha256(evidence_str.encode("utf-8")).hexdigest()
        f = Finding(
            subquestion_id=subq.id,
            claim=args["claim"],
            evidence=evidence_str,
            source_url=source_url,
            source_title=args.get("source_title", ""),
            confidence=float(args["confidence"]),
            researcher_id=researcher_id,
            fetched_at=datetime.now(timezone.utc),
            content_hash=content_hash,
            domain=domain,
            source_quality=compute_source_quality(source_url),
        )
        findings.append(f)
        events.researcher(
            researcher_id, subq.id,
            f"saved finding #{len(findings)} (conf {f.confidence:.2f}): "
            f"{f.claim[:80]}",
        )
        return (
            f"Saved finding #{len(findings)}. "
            f"You have {len(findings)} findings so far "
            f"(stop threshold: {cfg.researcher_min_findings_to_stop})."
        )

    if name == "note_uncertainty":
        n = UncertaintyNote(
            subquestion_id=subq.id,
            topic=args["topic"],
            reason=args["reason"],
            researcher_id=researcher_id,
        )
        uncertainty.append(n)
        events.researcher(
            researcher_id, subq.id,
            f"⚠ uncertainty noted: {n.topic[:60]} — {n.reason[:60]}",
        )
        return (
            f"Recorded uncertainty about: {n.topic}. "
            "This will be surfaced in the final report's caveats. "
            "Continue with other aspects if there are any, or wrap up."
        )

    return f"[unknown tool: {name}]"

