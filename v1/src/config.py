"""Centralized config. All knobs live here, loaded from env with sane defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    """Immutable bundle of every configurable knob in the system."""
    anthropic_api_key: str
    tavily_api_key: str

    researcher_model: str = "claude-haiku-4-5"
    writer_model: str = "claude-sonnet-4-6"
    critic_model: str = "claude-sonnet-4-6"
    judge_model: str = "claude-opus-4-7"
    kae_judge_model: str = "claude-haiku-4-5"

    min_subquestions: int = 3
    max_subquestions: int = 7
    decompose_max_retries: int = 2

    researcher_max_tool_calls: int = 30
    researcher_max_findings: int = 6
    researcher_min_findings_to_stop: int = 3
    researcher_min_unique_sources: int = 2
    researcher_min_avg_confidence: float = 0.55
    researcher_unproductive_call_limit: int = 10

    search_results_per_query: int = 8
    fetch_max_chars: int = 12_000

    # Semantic Scholar free tier rate-limits at ~1 req/sec; once 429'd it
    # typically holds for minutes. Retry briefly, then disable for the run.
    scholar_max_retries: int = 3
    scholar_backoff_seconds: float = 1.0
    scholar_unavailable_window_seconds: int = 120

    max_revisions: int = 2
    verifier_enabled: bool = True

    parallel_researchers: int = 2

    exporter_md_dir: str = "examples/outputs"

    trace_enabled: bool = False
    trace_k_init: int = 2
    trace_k_max: int = 4
    trace_tau_high: float = 0.75
    trace_temperature: float = 0.7


def _parse_bool(s: str) -> bool:
    """Parse common truthy strings ('1', 'true', 'yes', 'on') from env vars."""
    return s.strip().lower() in ("1", "true", "yes", "on")


def load() -> Config:
    """Load Config from process environment.

    Raises:
        RuntimeError: if either ANTHROPIC_API_KEY or TAVILY_API_KEY is missing.
    """
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    tavily_key = os.environ.get("TAVILY_API_KEY", "")
    if not anthropic_key:
        raise RuntimeError("ANTHROPIC_API_KEY missing — set it in .env")
    if not tavily_key:
        raise RuntimeError("TAVILY_API_KEY missing — set it in .env")
    return Config(
        anthropic_api_key=anthropic_key,
        tavily_api_key=tavily_key,
        researcher_model=os.environ.get("RESEARCHER_MODEL", "claude-sonnet-4-6"),
        writer_model=os.environ.get("WRITER_MODEL", "claude-sonnet-4-6"),
        critic_model=os.environ.get("CRITIC_MODEL", "claude-sonnet-4-6"),
        judge_model=os.environ.get("JUDGE_MODEL", "claude-opus-4-7"),
        kae_judge_model=os.environ.get("KAE_JUDGE_MODEL", "claude-haiku-4-5"),
        exporter_md_dir=os.environ.get("EXPORTER_MD_DIR", "examples/outputs"),
        trace_enabled=_parse_bool(os.environ.get("TRACE_ENABLED", "")),
        trace_k_init=int(os.environ.get("TRACE_K_INIT", "2")),
        trace_k_max=int(os.environ.get("TRACE_K_MAX", "4")),
        trace_tau_high=float(os.environ.get("TRACE_TAU_HIGH", "0.75")),
        trace_temperature=float(os.environ.get("TRACE_TEMPERATURE", "0.7")),
    )
