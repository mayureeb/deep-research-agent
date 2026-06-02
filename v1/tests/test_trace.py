"""Pure-unit tests for the TrACE controller and canonicaliser.

Mocks chat_with_tools_parallel + chat_with_tools to assert the
controller picks the right plurality, expands when it should, and
produces correct telemetry.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any
from unittest.mock import patch

import pytest

from src.config import Config
from src.agent.trace import (
    TraceController,
    TraceStep,
    canonicalize_action,
    canonicalize_response,
    END_TURN_CANONICAL,
    _canon_search_query,
    _canon_url,
    _canon_claim_text,
    _plurality,
)


# ---------------------------------------------------------------------------
# Shared fixtures.
# ---------------------------------------------------------------------------


def _cfg(**overrides) -> Config:
    """Construct a test Config with TrACE enabled and given overrides."""
    base = Config(
        anthropic_api_key="test-key",
        tavily_api_key="test-tavily",
        trace_enabled=True,
        trace_k_init=2,
        trace_k_max=4,
        trace_tau_high=0.75,
        trace_temperature=0.7,
    )
    return replace(base, **overrides)


@dataclass
class _FakeBlock:
    """Minimal stand-in for an Anthropic content block."""
    type: str
    name: str = ""
    input: dict | None = None
    text: str = ""
    id: str = "block-id"


@dataclass
class _FakeResponse:
    """Minimal stand-in for an anthropic.types.Message."""
    content: list[_FakeBlock]
    stop_reason: str = "tool_use"


def _tool_use(name: str, args: dict) -> _FakeResponse:
    """Build a fake response with a single tool_use block."""
    return _FakeResponse(content=[_FakeBlock(type="tool_use", name=name, input=args)])


def _end_turn() -> _FakeResponse:
    """Build a fake end_turn response (no tool_use blocks)."""
    return _FakeResponse(content=[_FakeBlock(type="text", text="done")], stop_reason="end_turn")


# ---------------------------------------------------------------------------
# Canonicaliser unit tests.
# ---------------------------------------------------------------------------


def test_canon_search_query_collapses_surface_form():
    """Stopword-only differences should collapse — that's the design.

    Note: the canonicaliser intentionally does NOT collapse content-word
    differences ("X overview" vs "X tutorial" stay distinct). The
    stopword list (the / a / is / what / etc.) is what gets collapsed.
    """
    a = _canon_search_query("What is chain of thought prompting?")
    b = _canon_search_query("chain of thought prompting")
    c = _canon_search_query("chain-of-thought prompting!")
    assert a == b == c


def test_canon_search_query_distinguishes_topics():
    """Different topics must NOT collapse."""
    a = _canon_search_query("chain of thought prompting")
    b = _canon_search_query("retrieval augmented generation")
    assert a != b


def test_canon_search_query_sorted_tokens():
    """Token order shouldn't matter — 'X vs Y' == 'Y vs X'."""
    a = _canon_search_query("python vs rust")
    b = _canon_search_query("rust vs python")
    assert a == b


def test_canon_url_collapses_scheme_and_www():
    """http/https + www. should collapse onto the same canonical key."""
    a = _canon_url("https://www.example.com/page")
    b = _canon_url("http://example.com/page")
    c = _canon_url("https://example.com/page/")  # trailing slash
    assert a == b == c


def test_canon_url_distinguishes_paths():
    """Different paths under the same host must NOT collapse."""
    assert _canon_url("https://example.com/a") != _canon_url("https://example.com/b")


def test_canon_url_query_keys_sorted():
    """Query-string key order shouldn't matter; values still distinguish."""
    a = _canon_url("https://example.com/p?b=2&a=1")
    b = _canon_url("https://example.com/p?a=1&b=2")
    c = _canon_url("https://example.com/p?a=1&b=3")  # different value
    assert a == b
    assert a != c


def test_canon_claim_text_word_order_independent():
    """Token-sort means word-order changes collapse, given identical content tokens.

    Limitation: morphological variation ("causes" vs "caused") does NOT
    collapse — the canonicaliser is intentionally cheap (no stemming, no
    embedding). Same content tokens reordered DO collapse, which catches
    the most common LLM-paraphrase pattern (rearranged subject/verb).
    """
    a = _canon_claim_text("python is fast")
    b = _canon_claim_text("fast python")
    assert a == b


def test_canon_claim_text_distinct_claims():
    """Lexically distinct claims must NOT collapse."""
    a = _canon_claim_text("python interpreter is fast")
    b = _canon_claim_text("rust compiler is faster")
    assert a != b


def test_canonicalize_action_save_finding_keys_on_url_and_claim():
    """Same claim + same source = same key; different source = different key.

    We use claims with multi-character distinguishing tokens because the
    canonicaliser drops 1-character tokens (so "X is true" / "Y is true"
    would collapse on the surviving "true" token alone).
    """
    args1 = {"claim": "python interpreter is fast", "source_url": "https://example.com/p", "evidence": "..."}
    args2 = {"claim": "python interpreter is fast", "source_url": "https://example.com/p", "evidence": "different evidence wording"}
    args3 = {"claim": "python interpreter is fast", "source_url": "https://other.com/p", "evidence": "..."}
    args4 = {"claim": "rust compiler is faster", "source_url": "https://example.com/p", "evidence": "..."}
    k1 = canonicalize_action("save_finding", args1)
    k2 = canonicalize_action("save_finding", args2)
    k3 = canonicalize_action("save_finding", args3)
    k4 = canonicalize_action("save_finding", args4)
    assert k1 == k2  # different evidence wording, same canonical save
    assert k1 != k3  # different source
    assert k1 != k4  # different claim


def test_canonicalize_action_unknown_tool_fallback():
    """Unknown tools fall through to a stable key."""
    k1 = canonicalize_action("future_tool", {"a": 1, "b": 2})
    k2 = canonicalize_action("future_tool", {"b": 2, "a": 1})
    assert k1 == k2  # arg order shouldn't matter
    assert k1.startswith("unknown:future_tool:")


def test_canonicalize_response_end_turn():
    """A response with no tool_use blocks canonicalises to END_TURN_CANONICAL."""
    canon, tool = canonicalize_response(_end_turn())
    assert canon == END_TURN_CANONICAL
    assert tool == "end_turn"


def test_canonicalize_response_tool_use():
    """A tool_use response uses its first tool_use block."""
    resp = _tool_use("web_search", {"query": "claude api"})
    canon, tool = canonicalize_response(resp)
    assert tool == "web_search"
    assert canon.startswith("search:")


# ---------------------------------------------------------------------------
# Plurality helper.
# ---------------------------------------------------------------------------


def test_plurality_unanimous():
    """Three identical keys → α=1.0."""
    alpha, key = _plurality(["A", "A", "A"])
    assert alpha == 1.0
    assert key == "A"


def test_plurality_majority():
    """Two of three the same → α=0.667."""
    alpha, key = _plurality(["A", "A", "B"])
    assert pytest.approx(alpha, rel=1e-3) == 2 / 3
    assert key == "A"


def test_plurality_tie_breaks_by_insertion_order():
    """Tie at plurality → first-inserted wins (deterministic)."""
    alpha, key = _plurality(["A", "B"])
    assert alpha == 0.5
    assert key == "A"


def test_plurality_empty():
    """Empty list returns the documented (0.0, '') default."""
    assert _plurality([]) == (0.0, "")


# ---------------------------------------------------------------------------
# TraceController.propose end-to-end (mocked LLM).
# ---------------------------------------------------------------------------


def test_propose_high_agreement_commits_at_kinit():
    """All kinit candidates agree → α=1.0, commits at kinit, no expansion."""
    cfg = _cfg(trace_k_init=2, trace_k_max=4, trace_tau_high=0.75)
    ctrl = TraceController(cfg)

    # Both rollouts pick the same canonical action (same query).
    parallel_returns = [
        _tool_use("web_search", {"query": "test query"}),
        _tool_use("web_search", {"query": "test  query"}),  # whitespace differs
    ]
    with patch("src.llm.chat_with_tools_parallel", return_value=parallel_returns) as parallel_mock, \
         patch("src.llm.chat_with_tools") as expand_mock:
        chosen, step = ctrl.propose(
            api_key="k", model="m", system="s",
            messages=[{"role": "user", "content": "go"}],
            tools=[],
            step_idx=0,
        )

    parallel_mock.assert_called_once()
    expand_mock.assert_not_called()  # no expansion
    assert step.k_used == 2
    assert step.alpha == 1.0
    assert step.agreement_high is True
    assert step.chosen_tool == "web_search"
    assert step.step_idx == 0
    # Commit returns the FIRST response in the rollout list (deterministic
    # tie-break).
    assert chosen is parallel_returns[0]


def test_propose_low_agreement_expands_to_kmax():
    """All rollouts disagree → expands one-at-a-time up to kmax."""
    cfg = _cfg(trace_k_init=2, trace_k_max=4, trace_tau_high=0.75)
    ctrl = TraceController(cfg)

    # Initial 2 disagree (α=0.5 < τ_high=0.75 → expand).
    parallel_returns = [
        _tool_use("web_search", {"query": "alpha"}),
        _tool_use("web_search", {"query": "beta"}),
    ]
    # Each expansion adds another distinct query — never reaches τ_high.
    expansions = [
        _tool_use("web_search", {"query": "gamma"}),
        _tool_use("web_search", {"query": "delta"}),
    ]
    with patch("src.llm.chat_with_tools_parallel", return_value=parallel_returns), \
         patch("src.llm.chat_with_tools", side_effect=expansions) as expand_mock:
        chosen, step = ctrl.propose(
            api_key="k", model="m", system="s",
            messages=[{"role": "user", "content": "go"}],
            tools=[],
            step_idx=3,
        )

    assert expand_mock.call_count == 2  # expanded twice (2 → 3 → 4)
    assert step.k_used == 4  # kmax
    # 4 distinct keys → plurality is the first one (each has count 1, ties
    # broken by insertion order).
    assert step.alpha == 0.25
    assert step.agreement_high is False
    assert chosen is parallel_returns[0]


def test_propose_expansion_clears_threshold():
    """Initial disagreement → one expansion produces majority → commits at k=3."""
    cfg = _cfg(trace_k_init=2, trace_k_max=4, trace_tau_high=0.66)
    ctrl = TraceController(cfg)

    # Initial 2 disagree (α=0.5).
    parallel_returns = [
        _tool_use("fetch_url", {"url": "https://a.com/x"}),
        _tool_use("fetch_url", {"url": "https://b.com/x"}),
    ]
    # First expansion lands on the same canonical as the FIRST candidate
    # → 2/3 = 0.667 ≥ 0.66 τ_high → commit.
    expansions = [_tool_use("fetch_url", {"url": "https://a.com/x"})]
    with patch("src.llm.chat_with_tools_parallel", return_value=parallel_returns), \
         patch("src.llm.chat_with_tools", side_effect=expansions) as expand_mock:
        chosen, step = ctrl.propose(
            api_key="k", model="m", system="s",
            messages=[{"role": "user", "content": "go"}],
            tools=[],
            step_idx=1,
        )

    expand_mock.assert_called_once()
    assert step.k_used == 3
    assert pytest.approx(step.alpha, rel=1e-3) == 2 / 3
    assert step.agreement_high is True
    # Plurality is the 'a.com' canonical; first response with that key
    # is parallel_returns[0].
    assert chosen is parallel_returns[0]


def test_propose_end_turn_acts_as_action():
    """end_turn rollouts can win plurality alongside tool_use rollouts."""
    cfg = _cfg(trace_k_init=2, trace_k_max=2, trace_tau_high=0.75)
    ctrl = TraceController(cfg)

    parallel_returns = [_end_turn(), _end_turn()]
    with patch("src.llm.chat_with_tools_parallel", return_value=parallel_returns), \
         patch("src.llm.chat_with_tools") as expand_mock:
        chosen, step = ctrl.propose(
            api_key="k", model="m", system="s",
            messages=[{"role": "user", "content": "go"}],
            tools=[],
            step_idx=0,
        )

    expand_mock.assert_not_called()
    assert step.k_used == 2
    assert step.alpha == 1.0
    assert step.agreement_high is True
    assert step.chosen_tool == "end_turn"
    assert step.chosen_canonical == END_TURN_CANONICAL


def test_propose_picks_first_plurality_response():
    """When [A, B, B] candidates, chosen response is the FIRST B (idx 1)."""
    cfg = _cfg(trace_k_init=2, trace_k_max=4, trace_tau_high=0.66)
    ctrl = TraceController(cfg)

    # Initial 2: A then B, α=0.5, expand.
    parallel_returns = [
        _tool_use("web_search", {"query": "alpha"}),
        _tool_use("web_search", {"query": "beta"}),
    ]
    # Expansion: another B → counts: A=1, B=2, plurality=B at α=2/3.
    expansions = [_tool_use("web_search", {"query": "beta"})]
    with patch("src.llm.chat_with_tools_parallel", return_value=parallel_returns), \
         patch("src.llm.chat_with_tools", side_effect=expansions):
        chosen, step = ctrl.propose(
            api_key="k", model="m", system="s",
            messages=[{"role": "user", "content": "go"}],
            tools=[],
            step_idx=0,
        )

    # Plurality is "beta"; first response with that key is index 1.
    assert chosen is parallel_returns[1]
    assert step.chosen_tool == "web_search"
