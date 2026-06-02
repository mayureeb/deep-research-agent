"""Centralized config loaded from env with defaults."""
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

    # Decomposition bounds; planner is asked for 3-7 and clamped if it strays.
    min_subquestions: int = 3
    max_subquestions: int = 7
    decompose_max_retries: int = 2

    # Per-researcher containment.
    researcher_max_tool_calls: int = 30
    researcher_max_findings: int = 6
    researcher_min_findings_to_stop: int = 3
    # Force a note_uncertainty + exit if N tool calls produced zero findings,
    # so other researchers in the semaphore queue aren't starved.
    researcher_unproductive_call_limit: int = 10

    search_results_per_query: int = 8
    fetch_max_chars: int = 12_000

    # Semantic Scholar resilience: short retry on transient 429s, then mark
    # the tool unavailable for the rest of the run.
    scholar_max_retries: int = 3
    scholar_backoff_seconds: float = 1.0
    scholar_unavailable_window_seconds: int = 120

    max_revisions: int = 1

    parallel_researchers: int = 2  # asyncio.Semaphore cap

    # Directory the post-critic exporter writes its markdown rendering of
    # FinalOutput.content into. Override via EXPORTER_MD_DIR env var.
    exporter_md_dir: str = "examples/outputs"


def load() -> Config:
    """Load Config from process environment.

    Raises:
        RuntimeError: if ANTHROPIC_API_KEY or TAVILY_API_KEY is missing.
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
        exporter_md_dir=os.environ.get("EXPORTER_MD_DIR", "examples/outputs"),
    )
