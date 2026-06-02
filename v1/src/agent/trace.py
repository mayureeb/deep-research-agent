"""TrACE adaptive-compute controller for the researcher tool loop.

Wraps each chat_with_tools turn with adaptive sampling: draw k_init
candidates at temperature τ, compute inter-rollout agreement α, and
either commit at k_init (α >= τ_high) or expand one-at-a-time up to
k_max before committing to the plurality.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING
from urllib.parse import urlparse, parse_qsl, urlencode

if TYPE_CHECKING:
    from ..config import Config


_QUERY_STOPWORDS = frozenset({
    "the", "a", "an", "of", "and", "or", "in", "on", "for", "to",
    "is", "are", "what", "how", "why", "when", "where", "which",
})


def _canon_search_query(query: str) -> str:
    """Canonicalise a web_search / search_papers query: lowercased,
    stopwords stripped, tokens sorted."""
    tokens = re.findall(r"[a-z0-9]+", query.lower())
    tokens = [t for t in tokens if t not in _QUERY_STOPWORDS and len(t) > 1]
    return " ".join(sorted(tokens))


def _canon_url(url: str) -> str:
    """Canonicalise a URL for fetch_url action equivalence.

    Normalises scheme, lowercases host, strips leading 'www.' and trailing
    slash, sorts query keys, drops fragment.
    """
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return f"url:{url.strip().lower()}"
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/")
    if parsed.query:
        items = parse_qsl(parsed.query, keep_blank_values=True)
        items.sort(key=lambda kv: kv[0])
        qs = urlencode(items)
        return f"fetch:{host}{path}?{qs}"
    return f"fetch:{host}{path}"


def _canon_claim_text(text: str) -> str:
    """Canonicalise a claim string for save_finding action equivalence."""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    tokens = [t for t in tokens if t not in _QUERY_STOPWORDS and len(t) > 1]
    canon = " ".join(sorted(tokens))
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:16]


def canonicalize_action(tool_name: str, args: dict) -> str:
    """Map a (tool_name, args) pair to a canonical action key for equality."""
    if tool_name in ("web_search", "search_papers"):
        query = args.get("query", "") or ""
        return f"search:{_canon_search_query(query)}"

    if tool_name == "fetch_url":
        url = args.get("url", "") or ""
        return _canon_url(url)

    if tool_name == "save_finding":
        url = args.get("source_url", "") or ""
        claim = args.get("claim", "") or ""
        return f"save:{_canon_url(url)}:{_canon_claim_text(claim)}"

    if tool_name == "note_uncertainty":
        topic = args.get("topic", "") or ""
        return f"note:{_canon_claim_text(topic)}"

    try:
        sorted_args = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        sorted_args = str(args)
    return f"unknown:{tool_name}:{sorted_args}"


END_TURN_CANONICAL = "__end_turn__"


def canonicalize_response(resp: Any) -> tuple[str, str]:
    """Return (canonical_key, tool_name) for a chat_with_tools response.

    Uses the first tool_use block; end_turn responses return ("__end_turn__",
    "end_turn").
    """
    if resp is None or not getattr(resp, "content", None):
        return END_TURN_CANONICAL, "end_turn"
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            name = getattr(block, "name", "") or ""
            args = getattr(block, "input", {}) or {}
            return canonicalize_action(name, args), name
    return END_TURN_CANONICAL, "end_turn"


@dataclass
class TraceStep:
    """Per-step TrACE telemetry."""
    step_idx: int
    k_used: int
    alpha: float
    agreement_high: bool
    chosen_canonical: str
    chosen_tool: str
    end_turn_override: bool = False
    save_finding_preferred: bool = False


class TraceController:
    """Per-researcher TrACE controller. Not thread-safe; one instance per
    researcher loop."""

    def __init__(self, cfg: "Config") -> None:
        self.k_init = max(1, int(cfg.trace_k_init))
        self.k_max = max(self.k_init, int(cfg.trace_k_max))
        self.tau_high = float(cfg.trace_tau_high)
        self.temperature = float(cfg.trace_temperature)
        self.max_findings = int(cfg.researcher_max_findings)

    def propose(
        self,
        api_key: str,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict],
        step_idx: int,
        n_findings_saved: int = 0,
        max_tokens: int = 16384,
    ) -> tuple[Any, TraceStep]:
        """Run one TrACE-adapted decision step. Returns (chosen_response, TraceStep)."""
        from ..llm import chat_with_tools, chat_with_tools_parallel

        responses: list[Any] = chat_with_tools_parallel(
            api_key, model, system, messages, tools,
            k=self.k_init, temperature=self.temperature, max_tokens=max_tokens,
        )

        canon_pairs: list[tuple[str, str]] = [
            canonicalize_response(r) for r in responses
        ]
        canon_keys: list[str] = [k for k, _ in canon_pairs]
        tool_names: list[str] = [t for _, t in canon_pairs]
        agreement_high = False

        while True:
            alpha, plurality_tool = _plurality(tool_names)
            if alpha >= self.tau_high:
                agreement_high = True
                break
            if len(responses) >= self.k_max:
                break
            extra = chat_with_tools(
                api_key, model, system, messages, tools,
                temperature=self.temperature, max_tokens=max_tokens,
            )
            responses.append(extra)
            extra_canon, extra_tool = canonicalize_response(extra)
            canon_keys.append(extra_canon)
            tool_names.append(extra_tool)
            canon_pairs.append((extra_canon, extra_tool))

        end_turn_override = False
        if (
            plurality_tool == "end_turn"
            and n_findings_saved == 0
            and any(t != "end_turn" for t in tool_names)
        ):
            non_end = [t for t in tool_names if t != "end_turn"]
            _, plurality_tool = _plurality(non_end)
            alpha = tool_names.count(plurality_tool) / len(tool_names)
            agreement_high = False
            end_turn_override = True

        save_finding_preferred = False
        if (
            plurality_tool != "save_finding"
            and "save_finding" in tool_names
            and n_findings_saved < self.max_findings
        ):
            save_count = tool_names.count("save_finding")
            plurality_count = tool_names.count(plurality_tool)
            if save_count >= plurality_count - 1:
                plurality_tool = "save_finding"
                alpha = save_count / len(tool_names)
                agreement_high = False
                save_finding_preferred = True

        chosen_idx = next(
            (i for i, t in enumerate(tool_names) if t == plurality_tool),
            0,
        )
        chosen = responses[chosen_idx]
        chosen_canonical, chosen_tool = canon_pairs[chosen_idx]

        step = TraceStep(
            step_idx=step_idx,
            k_used=len(responses),
            alpha=alpha,
            agreement_high=agreement_high,
            chosen_canonical=chosen_canonical,
            chosen_tool=chosen_tool,
            end_turn_override=end_turn_override,
            save_finding_preferred=save_finding_preferred,
        )
        return chosen, step


def _plurality(keys: list[str]) -> tuple[float, str]:
    """Return (alpha, plurality_key) for a list of canonical action keys."""
    if not keys:
        return 0.0, ""
    counts = Counter(keys)
    plurality_key, plurality_count = counts.most_common(1)[0]
    return plurality_count / len(keys), plurality_key
