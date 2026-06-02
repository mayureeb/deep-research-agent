"""GPT-Researcher-style baseline pipeline.

Planner → parallel researchers (web_search + fetch_url) → publisher.
Free-form prose output; no structured claims, no critic, no verification.
Used by eval/baseline_compare.py for head-to-head comparison.
"""
from __future__ import annotations

import asyncio
import time

from .. import events
from ..config import Config
from ..llm import chat_json, chat_text, chat_with_tools
from ..state import (
    BaselineReport,
    BaselineRun,
    BaselineSummary,
    Decomposition,
    SubQuestion,
)
from ..tools.fetch import fetch as fetch_url, FetchContext
from ..tools.search import TavilySearch
from ..tools import search as search_tool
from ..tools import fetch as fetch_tool
from .orchestrator import DECOMPOSE_SYSTEM, _enforce_subq_count


RESEARCHER_SYSTEM = """You are a research sub-agent in a baseline pipeline. You are assigned ONE sub-question.

Process:
1. Issue a web_search for your sub-question.
2. fetch_url on 2-4 promising results.
3. Write a SINGLE free-form summary paragraph (4-8 sentences) covering what the sources say about your sub-question.

Style:
- Mention sources by URL inline if useful (no formal citation format).
- It's fine to weave findings together in prose.
- Do NOT output a structured list or claims-with-evidence — write flowing prose.
- Keep the summary self-contained.

Stop when you've written your summary paragraph as your final text response."""


PUBLISHER_SYSTEM = """You are the publisher of a research report. You receive a list of free-form summaries (one per sub-question) and produce a unified prose research report.

Style:
- Flowing prose, not bullet lists.
- Cover all sub-questions but synthesize across them when natural.
- End with a "Sources" section listing the URLs consulted.
- Aim for 600-1200 words.
- Do NOT use structured "claim + evidence" formatting. Write a normal report."""


def _researcher_tools() -> list[dict]:
    """Two-tool kit for baseline researchers (deliberately less than the
    full system's 5)."""
    return [search_tool.tool_schema(), fetch_tool.tool_schema()]


async def run_baseline(cfg: Config, prompt: str) -> BaselineRun:
    """Run the baseline pipeline on one prompt.

    Steps mirror orchestrator.run_research but with the simpler primitives:
      ① decompose (same prompt, same model)
      ② parallel baseline-researchers (free-form summary each)
      ③ publisher concatenates into prose report

    No reconciliation, no critic, no revision. The simpler shape is the
    point — it's what we're comparing against.
    """
    started = time.time()

    events.baseline("decomposing prompt...")
    decomp = chat_json(
        cfg.anthropic_api_key, cfg.writer_model, DECOMPOSE_SYSTEM,
        f"Research prompt:\n\n{prompt}", Decomposition,
        max_retries=cfg.decompose_max_retries,
    )
    decomp = _enforce_subq_count(decomp, cfg)
    events.baseline(f"decomposed into {len(decomp.subquestions)} sub-questions")

    events.baseline(
        f"spawning {len(decomp.subquestions)} baseline researchers..."
    )
    # Per-run FetchContext shared across baseline researchers, mirroring
    # the orchestrator so per-domain blocking is identical across both pipelines.
    sem = asyncio.Semaphore(cfg.parallel_researchers)
    fetch_ctx = FetchContext()
    tasks = [_run_baseline_researcher(cfg, sq, sem, fetch_ctx) for sq in decomp.subquestions]
    summaries: list[BaselineSummary] = []
    # Same graceful-degradation pattern as the full system: one researcher's
    # exception doesn't kill the run.
    for result in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(result, Exception):
            events.baseline(f"  ⚠ researcher raised: {result}")
            continue
        summaries.append(result)

    events.baseline("publisher synthesizing free-form report...")
    final_report = _publish(cfg, prompt, summaries)
    # Dedupe sources across all summaries for the all_sources field.
    all_sources = sorted({u for s in summaries for u in s.sources_consulted})
    elapsed = time.time() - started
    events.baseline(
        f"done — {len(summaries)} summaries, {len(all_sources)} sources, "
        f"{elapsed:.1f}s"
    )

    return BaselineRun(
        user_prompt=prompt,
        decomposition=decomp,
        report=BaselineReport(
            user_prompt=prompt,
            final_report_text=final_report,
            summaries=summaries,
            all_sources=all_sources,
        ),
        elapsed_seconds=elapsed,
    )


async def _run_baseline_researcher(
    cfg: Config, subq: SubQuestion, sem: asyncio.Semaphore,
    fetch_ctx: FetchContext | None = None,
) -> BaselineSummary:
    """Async wrapper. Same semaphore + thread-pool pattern as the full-
    system researcher. Threads the FetchContext through so per-domain
    blocking is shared across baseline researchers."""
    async with sem:
        return await asyncio.to_thread(_baseline_researcher_sync, cfg, subq, fetch_ctx)


def _baseline_researcher_sync(
    cfg: Config, subq: SubQuestion, fetch_ctx: FetchContext | None = None,
) -> BaselineSummary:
    """Synchronous tool-using loop for one baseline researcher.

    Notable differences vs the full-system researcher's loop:
      * No save_finding / note_uncertainty side effects — output is the
        final text turn (the "summary paragraph").
      * Captures `raw_source_texts` so the grounding-freeform metric can
        check extracted claims against the actual source text the
        baseline saw. Without this we couldn't compute a fair grounding
        rate on baseline output.
      * Tighter tool-call cap (12 vs 30) — baseline is supposed to be
        lightweight; free-form summaries don't need as many sources.
    """
    searcher = TavilySearch(cfg.tavily_api_key)
    user = (
        f"Sub-question: {subq.question}\n\n"
        f"Rationale: {subq.rationale}"
    )
    messages: list[dict] = [{"role": "user", "content": user}]
    tools = _researcher_tools()
    sources_consulted: list[str] = []
    raw_source_texts: list[str] = []
    tool_calls = 0
    summary_text = ""

    # Use a tighter cap than the structured researcher — baseline is supposed
    # to be lighter-weight, and free-form summaries don't need as many sources.
    max_calls = min(cfg.researcher_max_tool_calls, 12)

    while tool_calls < max_calls:
        resp = chat_with_tools(
            cfg.anthropic_api_key, cfg.researcher_model,
            RESEARCHER_SYSTEM, messages, tools,
        )
        messages.append({"role": "assistant", "content": resp.content})

        # End-turn: the model produced its summary paragraph. Extract it
        # from the first text block and exit.
        if resp.stop_reason == "end_turn":
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    summary_text = block.text
                    break
            break

        tool_results = []
        any_tool = False
        for block in resp.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            any_tool = True
            tool_calls += 1
            try:
                if block.name == "web_search":
                    results = searcher.search(
                        block.input["query"], cfg.search_results_per_query,
                    )
                    result_str = "\n\n".join(
                        f"[{i}] {r.title}\nURL: {r.url}\n{r.snippet}"
                        for i, r in enumerate(results)
                    ) or "[no results]"
                elif block.name == "fetch_url":
                    result = fetch_url(block.input["url"], cfg.fetch_max_chars,
                                        ctx=fetch_ctx)
                    if not result.is_usable:
                        result_str = (
                            f"[fetch status={result.status}] "
                            f"{result.error or '(no detail)'}"
                        )
                    else:
                        sources_consulted.append(result.url)
                        raw_source_texts.append(result.text)
                        result_str = (
                            f"[fetch status=ok]\n"
                            f"TITLE: {result.title}\n\n{result.text}"
                        )
                else:
                    result_str = f"[unknown tool: {block.name}]"
            except Exception as e:
                result_str = f"[tool error: {e}]"
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_str,
            })

        if not any_tool:
            break
        messages.append({"role": "user", "content": tool_results})

    return BaselineSummary(
        subquestion_id=subq.id,
        subquestion_text=subq.question,
        # Defensive default if the model never produced a final text turn
        # (e.g., hit max_calls in the middle of a tool call).
        summary_text=summary_text or "(no summary produced)",
        # dict.fromkeys preserves order while deduping (set would lose order).
        sources_consulted=list(dict.fromkeys(sources_consulted)),
        raw_source_texts=raw_source_texts,
    )


def _publish(cfg: Config, prompt: str, summaries: list[BaselineSummary]) -> str:
    """Run the publisher LLM call.

    Concatenates per-sub-question summaries into a single prose report.
    Uses chat_text (not chat_json) — the output is free-form, not
    structured. Uses writer_model so the prose-quality match is comparable
    to the full system's writer.
    """
    summaries_text = "\n\n---\n\n".join(
        f"SUB-QUESTION: {s.subquestion_text}\n\nSUMMARY:\n{s.summary_text}\n\n"
        f"SOURCES: {', '.join(s.sources_consulted) or '(none)'}"
        for s in summaries
    )
    user = (
        f"USER PROMPT:\n{prompt}\n\n"
        f"SUB-QUESTION SUMMARIES:\n\n{summaries_text}\n\n"
        f"Write the unified research report."
    )
    return chat_text(
        cfg.anthropic_api_key, cfg.writer_model,
        PUBLISHER_SYSTEM, [{"role": "user", "content": user}],
        max_tokens=16384,
    )
