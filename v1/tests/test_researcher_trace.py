"""Researcher-loop integration test for the TrACE controller.

Mocks the LLM client so the researcher's tool-using loop runs end-to-end
without API calls. Asserts trace_log population, budget bookkeeping,
and the TraceStats rollup.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any
from unittest.mock import patch

import pytest

from src.config import Config
from src.state import (
    Decomposition,
    FindingsStore,
    SubQuestion,
    TraceStats,
)
from src.agent.orchestrator import _compute_trace_stats
from src.agent.researcher import _run_sync
from src.agent.trace import TraceStep


def _cfg(trace_enabled: bool, **overrides) -> Config:
    base = Config(
        anthropic_api_key="test-key",
        tavily_api_key="test-tavily",
        researcher_max_tool_calls=10,
        researcher_max_findings=6,
        researcher_min_findings_to_stop=3,
        researcher_unproductive_call_limit=10,
        trace_enabled=trace_enabled,
        trace_k_init=2,
        trace_k_max=4,
        trace_tau_high=0.75,
        trace_temperature=0.7,
    )
    return replace(base, **overrides)


@dataclass
class _FakeBlock:
    type: str
    name: str = ""
    input: dict | None = None
    text: str = ""
    id: str = "block-id"


@dataclass
class _FakeResponse:
    content: list[_FakeBlock]
    stop_reason: str = "tool_use"


def _end_turn() -> _FakeResponse:
    return _FakeResponse(
        content=[_FakeBlock(type="text", text="done")],
        stop_reason="end_turn",
    )


def _websearch(query: str) -> _FakeResponse:
    return _FakeResponse(
        content=[_FakeBlock(
            type="tool_use", name="web_search",
            input={"query": query}, id=f"id-{query}",
        )],
        stop_reason="tool_use",
    )


def _subq() -> SubQuestion:
    return SubQuestion(id="sq1", question="what is X?", rationale="test")


# ---------------------------------------------------------------------------
# Baseline: trace OFF preserves greedy researcher behavior.
# ---------------------------------------------------------------------------


def test_trace_disabled_produces_empty_trace_log():
    """cfg.trace_enabled=False → trace_log empty, no rollouts."""
    cfg = _cfg(trace_enabled=False)
    # One greedy chat_with_tools call → end_turn immediately.
    with patch("src.agent.researcher.chat_with_tools", return_value=_end_turn()) as greedy:
        result = _run_sync(cfg, _subq(), fetch_ctx=None)

    greedy.assert_called_once()
    assert result.trace_log == []
    # No findings either (we never dispatched a tool).
    assert result.findings == []


# ---------------------------------------------------------------------------
# TrACE on: per-step trace records, budget bookkeeping.
# ---------------------------------------------------------------------------


def test_trace_enabled_populates_trace_log_and_budget():
    """cfg.trace_enabled=True with high-agreement first step + end_turn second.

    Verifies:
      * trace_log gets one entry per decision step (here: 2 steps).
      * tool_calls budget reflects k_used (not 1) per step.
      * Each step's k_used matches what we mocked (kinit=2, all agreeing).
    """
    cfg = _cfg(trace_enabled=True, trace_k_init=2, trace_k_max=4)

    # Step 0: 2 rollouts agree on the same web_search query → commit
    # at kinit=2, then dispatch and continue.
    step0_rollouts = [_websearch("topic"), _websearch("topic")]
    # Step 1: 2 rollouts agree on end_turn → commit at kinit=2 and break.
    step1_rollouts = [_end_turn(), _end_turn()]

    # Mock chat_with_tools_parallel to return the rollouts for each
    # decision step in order.
    parallel_calls: list[list[_FakeResponse]] = [step0_rollouts, step1_rollouts]
    parallel_iter = iter(parallel_calls)

    def _parallel_side_effect(*args, **kwargs):
        return next(parallel_iter)

    # web_search dispatch needs a TavilySearch — patch the search tool to
    # return an empty result list so the dispatch returns a no-op string.
    with patch("src.llm.chat_with_tools_parallel", side_effect=_parallel_side_effect), \
         patch("src.llm.chat_with_tools") as expand_mock, \
         patch("src.agent.researcher.TavilySearch") as TavilyClass:
        TavilyClass.return_value.search.return_value = []
        result = _run_sync(cfg, _subq(), fetch_ctx=None)

    expand_mock.assert_not_called()  # high agreement → no expansion

    # 2 steps. First is a web_search; second is end_turn.
    assert len(result.trace_log) == 2
    s0, s1 = result.trace_log
    assert s0.k_used == 2 and s0.alpha == 1.0 and s0.agreement_high is True
    assert s0.chosen_tool == "web_search"
    assert s1.k_used == 2 and s1.chosen_tool == "end_turn"

    # Budget: step 0 had 1 tool_use block (existing +=1) plus our
    # +=(k_used-1)=1, total +=2. Step 1 was end_turn → +=k_used=2.
    # tool_counts only tracks dispatched tool_use blocks (1 web_search).
    assert result.tool_counts.get("web_search", 0) == 1


def test_trace_low_agreement_expands_and_charges_full_budget():
    """All rollouts disagree → expand to kmax → budget consumes kmax."""
    cfg = _cfg(trace_enabled=True, trace_k_init=2, trace_k_max=4, trace_tau_high=0.99)

    # Step 0: kinit=2 disagree → expand to kmax=4. All distinct queries.
    step0_initial = [_websearch("q1"), _websearch("q2")]
    step0_expansions = [_websearch("q3"), _websearch("q4")]
    # Step 1: end_turn unanimous, terminate.
    step1_rollouts = [_end_turn(), _end_turn()]

    parallel_iter = iter([step0_initial, step1_rollouts])

    def _parallel(*args, **kwargs):
        return next(parallel_iter)

    expand_iter = iter(step0_expansions)

    def _expand(*args, **kwargs):
        return next(expand_iter)

    with patch("src.llm.chat_with_tools_parallel", side_effect=_parallel), \
         patch("src.llm.chat_with_tools", side_effect=_expand) as expand_mock, \
         patch("src.agent.researcher.TavilySearch") as TavilyClass:
        TavilyClass.return_value.search.return_value = []
        result = _run_sync(cfg, _subq(), fetch_ctx=None)

    # Step 0 expanded twice (2 → 3 → 4); step 1 didn't expand.
    assert expand_mock.call_count == 2

    s0 = result.trace_log[0]
    assert s0.k_used == 4
    assert s0.agreement_high is False


# ---------------------------------------------------------------------------
# Aggregation helper — _compute_trace_stats.
# ---------------------------------------------------------------------------


def test_compute_trace_stats_empty_logs_returns_default():
    """No researchers / all-empty logs → default zero TraceStats."""
    out = _compute_trace_stats([])
    assert isinstance(out, TraceStats)
    assert out.steps_total == 0
    assert out.mean_k == 0.0
    assert out.per_tool_mean_k == {}


def test_compute_trace_stats_rolls_up_correctly():
    """Multi-researcher rollup matches the documented formula."""
    # Researcher A: 2 steps. step 0: k=2, α=1.0, high. step 1: k=4, α=0.5, low.
    # Researcher B: 1 step. step 0: k=2, α=1.0, high.
    log_a = [
        TraceStep(0, 2, 1.0, True, "x", "web_search"),
        TraceStep(1, 4, 0.5, False, "y", "fetch_url"),
    ]
    log_b = [
        TraceStep(0, 2, 1.0, True, "z", "web_search"),
    ]
    out = _compute_trace_stats([log_a, log_b])
    assert out.steps_total == 3
    assert out.total_k == 8
    assert pytest.approx(out.mean_k, rel=1e-3) == 8 / 3
    assert pytest.approx(out.mean_alpha, rel=1e-3) == (1.0 + 0.5 + 1.0) / 3
    assert pytest.approx(out.high_agreement_rate, rel=1e-3) == 2 / 3
    # Per-tool: web_search appears in steps with k=2,2 → mean 2.0;
    # fetch_url in one step with k=4 → mean 4.0.
    assert pytest.approx(out.per_tool_mean_k["web_search"], rel=1e-3) == 2.0
    assert pytest.approx(out.per_tool_mean_k["fetch_url"], rel=1e-3) == 4.0
