"""Researcher sub-agent: tool-using loop scoped to ONE sub-question.

The orchestrator spawns N of these in parallel under a semaphore. Each
researcher owns a fresh context window, has 5 tools (web_search,
search_papers, fetch_url, save_finding, note_uncertainty), and is bounded
by per-researcher caps on tool calls and findings plus an early-exit on
unproductive runs.

Tool-loop shape:
  while not stop:
    response = chat_with_tools(...)
    if response is "end_turn": break
    for each tool_use block:
      dispatch tool, build tool_result
    append assistant + tool_result messages
    check stop conditions
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
from ..tools import findings as findings_tool
from ..tools import search as search_tool
from ..tools import fetch as fetch_tool
from ..tools import scholar as scholar_tool
from ..tools.search import TavilySearch
from ..tools.fetch import fetch as fetch_url, FetchContext
from ..tools.scholar import search_papers
from .confidence import compute_source_quality


@dataclass
class ResearcherResult:
    """One researcher's full output bundle.

    Attributes:
        findings: Same list this loop's save_finding calls produced.
        uncertainty: Same list note_uncertainty calls produced.
        tool_counts: Dict[tool_name, count] across the loop's _dispatch_tool
            calls. Aggregated into the run's ToolMix by the orchestrator.
        elapsed_seconds: Wall-clock duration of _run_sync.
    """
    findings: list[Finding] = field(default_factory=list)
    uncertainty: list[UncertaintyNote] = field(default_factory=list)
    tool_counts: dict[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0


# Researcher system prompt. {min_findings} and {max_calls} are filled in
# from Config so the researcher targets the actual numbers.
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
3. IMMEDIATELY after that fetch returns, your next action MUST be either:
     - save_finding — ONLY if the fetch result starts with `[fetch status=ok]` AND the page contains a quotable claim that answers ANY part of the sub-question, OR
     - note_uncertainty — if the fetch result has any non-ok status (paywall, blocked, not_found, rate_limited, non_html, timeout, error), OR if it's `ok` but the body has no usable claim.
   NEVER save_finding from a non-`ok` fetch — paywall / bot-block bodies are bait, not evidence.
   Do NOT call web_search, search_papers, or fetch_url again until you have saved or noted uncertainty about the page you just read.
4. Repeat from step 1 with a new query or source until you have enough findings.

Compound sub-questions: if the question has multiple clauses (e.g. "X and Y", "methods AND scalability"), save SEPARATE findings for each clause as you encounter them. Do NOT wait for one source that covers everything — that source usually does not exist.

Stop when: you have at least {min_findings} solid findings, OR you've made {max_calls} tool calls.

Quality bar:
- A finding may answer only PART of the sub-question — that's fine and expected.
- Each finding must be grounded in a verbatim quote from the source you just read.
- Prefer DIVERSE sources — don't save 3 findings from the same article.
- Be HONEST about confidence. If a source hedges, your finding should hedge.
- If the literature disagrees, save findings from BOTH sides.
- An honest note_uncertainty is BETTER than fabricating a finding.

When done, respond with a final text message summarizing what you found (not a tool call)."""


def tools_for_researcher() -> list[dict]:
    """Return the 5-tool schema list passed to chat_with_tools.

    Search tools come first, fetch in the middle, output tools last —
    the LLM tends to consider tools in the order they appear.
    """
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
    """Run one researcher loop. Returns a ResearcherResult.

    Args:
        cfg: Loaded Config.
        subq: The sub-question this researcher owns.
        semaphore: Shared semaphore capped at cfg.parallel_researchers.
        fetch_ctx: Per-run FetchContext shared across all researchers so
            per-domain failure budget is shared. Optional.

    Acquires the semaphore, then runs the synchronous tool loop in a thread
    so multiple researchers can proceed concurrently against the sync SDK.
    """
    async with semaphore:
        return await asyncio.to_thread(_run_sync, cfg, subq, fetch_ctx)


def _run_sync(
    cfg: Config,
    subq: SubQuestion,
    fetch_ctx: FetchContext | None = None,
) -> ResearcherResult:
    """The synchronous tool-using loop.

    Stop conditions, in order:
      1. tool_calls >= max_tool_calls            (hard cap)
      2. len(findings) >= max_findings           (bag full)
      3. response.stop_reason == "end_turn"      (LLM decided it's done)
      4. no tool_use blocks in this turn         (defensive)
      5. unproductive_call_limit hit with 0 findings  (early-exit)
      6. min_findings_to_stop AND >= half budget used (soft wrap up)

    The unproductive-call early-exit records an UncertaintyNote and
    releases the semaphore slot so the next researcher can proceed.
    """
    researcher_id = f"r-{uuid.uuid4().hex[:6]}"
    events.researcher(researcher_id, subq.id, "starting")
    searcher = TavilySearch(cfg.tavily_api_key)
    started = time.monotonic()

    system = SYSTEM.format(
        min_findings=cfg.researcher_min_findings_to_stop,
        max_calls=cfg.researcher_max_tool_calls,
    )
    user = (
        f"Sub-question: {subq.question}\n\n"
        f"Rationale (why this matters in the broader research): {subq.rationale}"
    )
    messages: list[dict] = [{"role": "user", "content": user}]
    tools = tools_for_researcher()
    findings: list[Finding] = []
    uncertainty: list[UncertaintyNote] = []
    tool_calls = 0
    tool_counts: dict[str, int] = {}

    while tool_calls < cfg.researcher_max_tool_calls:
        if len(findings) >= cfg.researcher_max_findings:
            break

        resp = chat_with_tools(
            cfg.anthropic_api_key, cfg.researcher_model, system, messages, tools
        )

        # Anthropic's tool-use protocol requires the assistant turn to
        # remain in the context.
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "end_turn":
            break

        # A single response can contain multiple tool_use blocks (parallel
        # tool calling).
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
                # One bad tool call shouldn't kill the loop; surface the
                # error string so the model can recover.
                result_str = f"[tool error: {e}]"
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_str,
            })

        # Defensive: no tool_use and not end_turn — break rather than loop forever.
        if not any_tool_use:
            break

        messages.append({"role": "user", "content": tool_results})

        # Early-exit: N tool calls with zero findings means this researcher
        # is unlikely to produce anything; record uncertainty and release
        # the semaphore slot.
        if (
            tool_calls >= cfg.researcher_unproductive_call_limit
            and len(findings) == 0
        ):
            n = UncertaintyNote(
                subquestion_id=subq.id,
                topic=subq.question,
                reason=(
                    f"Made {tool_calls} tool calls without extracting concrete "
                    f"evidence. Search results, fetched pages, or paper abstracts "
                    f"did not yield specific claims supportable as findings. "
                    f"Researcher exited early to free budget for other sub-questions."
                ),
                researcher_id=researcher_id,
            )
            uncertainty.append(n)
            events.researcher(
                researcher_id, subq.id,
                f"early-exit: {tool_calls} calls, 0 findings — recorded uncertainty",
            )
            break

        # Soft-stop: enough findings AND past the halfway budget mark.
        if len(findings) >= cfg.researcher_min_findings_to_stop:
            if tool_calls >= cfg.researcher_max_tool_calls // 2:
                break

    elapsed = time.monotonic() - started
    events.researcher(
        researcher_id, subq.id,
        f"done — {len(findings)} findings, {len(uncertainty)} uncertainty notes, "
        f"{tool_calls} tool calls ({elapsed:.1f}s)",
    )
    return ResearcherResult(
        findings=findings,
        uncertainty=uncertainty,
        tool_counts=tool_counts,
        elapsed_seconds=elapsed,
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
    """Dispatch one tool_use call. Returns the string for the tool_result.

    Search/fetch tools format results into text the model sees on its
    next turn; save_finding / note_uncertainty append to the local lists
    and return a confirmation string.
    """
    if name == "web_search":
        query = args["query"]
        results = searcher.search(query, cfg.search_results_per_query)
        events.researcher(
            researcher_id, subq.id,
            f"web_search: '{query[:60]}' → {len(results)} results",
        )
        if not results:
            return "[no results]"
        # Numbered format lets the researcher reference results in its reasoning.
        return "\n\n".join(
            f"[{i}] {r.title}\nURL: {r.url}\n{r.snippet}"
            for i, r in enumerate(results)
        )

    if name == "search_papers":
        query = args["query"]
        # Bounded retry on 429 then sticky back-off.
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
        # citation_count and venue signal influence; pdf_url tells the
        # researcher whether it can fetch_url for full text.
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

        # Status-tagged error string lets the researcher act on the prompt's
        # per-status guidance. Don't return paywall bodies — they're bait.
        if not result.is_usable:
            return (
                f"[fetch status={result.status}] {result.error or '(no detail)'}"
            )

        # Leading status line so the model sees "this is real content"
        # without parsing the body.
        return (
            f"[fetch status=ok]\n"
            f"TITLE: {result.title}\n\n{result.text}"
        )

    if name == "save_finding":
        # researcher_id and subquestion_id are injected here — not the LLM's
        # job to track. Side-channel signals (source_quality, content_hash,
        # domain) are computed at save time so the tool args stay minimal.
        source_url = args["source_url"]
        evidence_str = args["evidence"]
        self_reported = float(args["confidence"])
        sq = compute_source_quality(source_url)
        weighted_conf = max(0.0, min(1.0, self_reported * sq))
        content_hash = hashlib.sha256(evidence_str.encode("utf-8")).hexdigest()
        domain = (urlparse(source_url).hostname or "").lower()
        now = datetime.now(timezone.utc)
        f = Finding(
            subquestion_id=subq.id,
            claim=args["claim"],
            evidence=evidence_str,
            source_url=source_url,
            source_title=args.get("source_title", ""),
            confidence=weighted_conf,
            self_reported_confidence=self_reported,
            source_quality=sq,
            researcher_id=researcher_id,
            saved_at=now,
            fetched_at=now,
            content_hash=content_hash,
            domain=domain,
        )
        findings.append(f)
        events.researcher(
            researcher_id, subq.id,
            f"saved finding #{len(findings)} "
            f"(conf {f.confidence:.2f} = self {self_reported:.2f} × sq {sq:.2f}): "
            f"{f.claim[:80]}",
        )
        # Running count + threshold so the researcher can decide whether to stop.
        return (
            f"Saved finding #{len(findings)}. "
            f"You have {len(findings)} findings so far "
            f"(stop threshold: {cfg.researcher_min_findings_to_stop})."
        )

    if name == "note_uncertainty":
        # Same id-injection pattern as save_finding.
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

    # Unknown tool name. Handle gracefully rather than crashing the loop.
    return f"[unknown tool: {name}]"
