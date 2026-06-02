"""Empirical sweep of decomposition-bounds policy choices.

For each (min, max) configuration in BOUNDS_CONFIGS, calls the planner
once per benchmark prompt (decompose-only, no fan-out). Records the count
emitted, whether floor/ceiling was hit, and pairwise text similarity
within the plan (low = clean partition; high = padding).

Usage:
    python -m eval.decomp_bounds --out decomp_bounds.json
"""
from __future__ import annotations

import asyncio
import json
import statistics
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from src import config as config_module
from src.llm import chat_json
from src.state import Decomposition

from .prompts import PROMPTS


console = Console()
app = typer.Typer(add_completion=False)


# Same wording as orchestrator.DECOMPOSE_SYSTEM but with bounds parameterized.
DECOMPOSE_TEMPLATE = """You are the lead planner for a deep research investigation.

Given a research prompt, decompose it into {lo}–{hi} sub-questions that, taken together, would let a researcher answer the prompt comprehensively.

Good decompositions:
- Each sub-question is concrete and answerable from sources (not "what is the meaning of X?").
- Sub-questions partition the topic — minimal overlap.
- For contested or contradictory topics, include sub-questions that probe the disagreement explicitly.
- For sparse/emerging topics, include a sub-question on "what evidence is missing or weak?"
- Cover BOTH the substance of the question AND the meta-level (methodology, evaluation, dissent) when relevant.

Do NOT generate a sub-question that is just "summarize X" — that's the writer's job.

Each sub-question gets:
- A unique short id (e.g., "sq1", "sq2", ...)
- The question itself
- A one-sentence rationale for why this sub-question matters to the overall prompt"""


# Configurations to sweep: (min_subq, max_subq).
BOUNDS_CONFIGS: list[tuple[int, int]] = [
    (2, 5),
    (3, 7),  # current default
    (4, 10),
]


@app.command()
def main(
    out: str = typer.Option("decomp_bounds.json", help="Output JSON path."),
    only: str = typer.Option(
        "", help="Comma-separated prompt ids to restrict to (default: all)."
    ),
):
    """Sweep decomposition (min, max) configurations across the benchmark."""
    cfg = config_module.load()

    selected = _resolve_prompts(only)
    console.print(
        f"[bold]Sweeping {len(BOUNDS_CONFIGS)} bounds configs × "
        f"{len(selected)} prompts = {len(BOUNDS_CONFIGS)*len(selected)} planner calls[/bold]"
    )

    raw_results: list[dict] = []
    for lo, hi in BOUNDS_CONFIGS:
        for bp in selected:
            console.print(f"  ({lo},{hi}) {bp.id}...", end="")
            decomp = _decompose_with_bounds(cfg, bp.prompt, lo, hi)
            count = len(decomp.subquestions)
            hit_floor = count <= lo
            hit_ceiling = count >= hi
            intra_sim = _intra_plan_similarity(decomp)
            raw_results.append({
                "config": {"min": lo, "max": hi},
                "prompt_id": bp.id,
                "category": bp.category,
                "count": count,
                "hit_floor": hit_floor,
                "hit_ceiling": hit_ceiling,
                "intra_plan_similarity": intra_sim,
                "subquestions": [
                    {"id": sq.id, "question": sq.question, "rationale": sq.rationale}
                    for sq in decomp.subquestions
                ],
            })
            console.print(f" → {count} sub-questions (intra-sim {intra_sim:.2f})")

    summary = _summarize(raw_results)
    _print_summary(summary)

    Path(out).write_text(json.dumps({
        "summary": summary,
        "raw": raw_results,
    }, indent=2, default=str))
    console.print(f"\n[dim]Wrote {out}[/dim]")


def _resolve_prompts(only: str) -> list:
    """Filter PROMPTS by the comma-separated `only` flag, or return all."""
    if not only:
        return list(PROMPTS)
    wanted = {p.strip() for p in only.split(",") if p.strip()}
    return [bp for bp in PROMPTS if bp.id in wanted]


def _decompose_with_bounds(cfg, prompt: str, lo: int, hi: int) -> Decomposition:
    """Run ONE planner call with (lo, hi) bounds in the system prompt."""
    system = DECOMPOSE_TEMPLATE.format(lo=lo, hi=hi)
    return chat_json(
        cfg.anthropic_api_key,
        cfg.writer_model,
        system,
        f"Research prompt:\n\n{prompt}",
        Decomposition,
        max_retries=cfg.decompose_max_retries,
    )


def _intra_plan_similarity(decomp: Decomposition) -> float:
    """Mean pairwise token Jaccard over sub-question text within ONE plan.

    Low (≈ 0) = clean partition; high (≥ 0.4) = overlap / padding.
    Returns 0.0 for plans with <2 sub-questions.
    """
    sqs = decomp.subquestions
    if len(sqs) < 2:
        return 0.0
    token_sets = [_tokens(sq.question) for sq in sqs]
    sims = []
    for i in range(len(token_sets)):
        for j in range(i + 1, len(token_sets)):
            a, b = token_sets[i], token_sets[j]
            if not a or not b:
                sims.append(0.0)
            else:
                sims.append(len(a & b) / len(a | b))
    return statistics.mean(sims) if sims else 0.0


_STOP = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "is", "are", "what", "how", "why", "does", "do", "this", "that",
    "by", "from", "as", "at", "be", "it", "its",
})


def _tokens(s: str) -> set[str]:
    """Lowercase + split + drop stop words. Set so Jaccard is order-free."""
    return {t for t in s.lower().split() if t.isalpha() and t not in _STOP}


def _summarize(raw: list[dict]) -> list[dict]:
    """Per-config aggregate: mean count, % floor / ceiling hits, mean intra-sim."""
    by_config: dict[tuple[int, int], list[dict]] = {}
    for r in raw:
        key = (r["config"]["min"], r["config"]["max"])
        by_config.setdefault(key, []).append(r)
    out = []
    for (lo, hi), rows in sorted(by_config.items()):
        out.append({
            "config": {"min": lo, "max": hi},
            "n_prompts": len(rows),
            "mean_count": statistics.mean(r["count"] for r in rows),
            "median_count": statistics.median(r["count"] for r in rows),
            "pct_hit_floor": sum(r["hit_floor"] for r in rows) / len(rows),
            "pct_hit_ceiling": sum(r["hit_ceiling"] for r in rows) / len(rows),
            "mean_intra_sim": statistics.mean(r["intra_plan_similarity"] for r in rows),
        })
    return out


def _print_summary(summary: list[dict]) -> None:
    """Render the per-config summary as a Rich table."""
    table = Table(title="Decomposition-bounds sweep")
    table.add_column("config")
    table.add_column("n", justify="right")
    table.add_column("mean", justify="right")
    table.add_column("median", justify="right")
    table.add_column("% at floor", justify="right")
    table.add_column("% at ceiling", justify="right")
    table.add_column("intra-sim", justify="right")
    for s in summary:
        c = s["config"]
        table.add_row(
            f"({c['min']},{c['max']})",
            str(s["n_prompts"]),
            f"{s['mean_count']:.1f}",
            f"{s['median_count']:.1f}",
            f"{s['pct_hit_floor']:.0%}",
            f"{s['pct_hit_ceiling']:.0%}",
            f"{s['mean_intra_sim']:.2f}",
        )
    console.print(table)


if __name__ == "__main__":
    app()
