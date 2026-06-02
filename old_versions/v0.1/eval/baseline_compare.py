"""Side-by-side comparison of the GPT-Researcher-style baseline against the
full system.

Same model, tools, parallelism, and decomposition prompt on both sides.
Grounding uses grounding.py for the structured side and grounding_freeform.py
for the baseline; coverage counts sub-questions with ≥1 finding (structured)
or a non-empty summary (baseline); judge is wrapped uniformly via a
synthesized ResearchRun for the baseline (see _judge_baseline).

Usage:
    python -m eval.baseline_compare                # all prompts
    python -m eval.baseline_compare --only easy-1  # one prompt
    python -m eval.baseline_compare --out cmp.json
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from src import config as config_module
from src.agent.baseline import run_baseline
from src.agent.orchestrator import run_research
from src.state import BaselineRun, ResearchRun
from .prompts import PROMPTS
from .metrics.aggregates import calibration_slope, f_score, fmt_slope
from .metrics.coverage import coverage_rate
from .metrics.grounding import grounding_rate
from .metrics.grounding_freeform import grounding_rate_freeform
from .metrics.judge import llm_judge


app = typer.Typer(add_completion=False)
console = Console()


@app.command()
def main(
    only: str = typer.Option(None, help="Run a single prompt by id"),
    out: str = typer.Option("comparison_results.json", help="Output path"),
):
    """Run the head-to-head comparison.

    Persists each run's JSON to examples/outputs/{id}_ours.json and
    {id}_baseline.json for drilldown via inspect.py.
    """
    cfg = config_module.load()
    selected = [p for p in PROMPTS if p.id == only] if only else PROMPTS
    if not selected:
        console.print(f"[red]No prompt with id {only}[/red]")
        raise typer.Exit(1)

    rows = []
    for bp in selected:
        console.print(f"\n[bold cyan]{bp.id}[/bold cyan] ({bp.category})")

        console.print("  ▸ running OURS (full system)...")
        t0 = time.time()
        ours: ResearchRun = asyncio.run(run_research(cfg, bp.prompt))
        ours_t = time.time() - t0

        console.print("  ▸ running BASELINE (GPT-Researcher-style)...")
        t0 = time.time()
        base: BaselineRun = asyncio.run(run_baseline(cfg, bp.prompt))
        base_t = time.time() - t0

        # Each scorer is independent; one side's failure won't break the row.
        console.print("  ▸ scoring metrics on both...")
        ours_metrics = _score_ours(cfg, ours)
        base_metrics = _score_baseline(cfg, base)
        ours_judge = llm_judge(cfg, ours, rubric=bp.rubric)
        base_judge = _judge_baseline(cfg, base, bp)

        rows.append({
            "prompt_id": bp.id,
            "category": bp.category,
            "ours": {**ours_metrics, "elapsed_s": ours_t,
                     "judge": ours_judge},
            "baseline": {**base_metrics, "elapsed_s": base_t,
                         "judge": base_judge},
            "delta_grounding": ours_metrics["grounding"] - base_metrics["grounding"],
            "delta_coverage": ours_metrics["coverage"] - base_metrics["coverage"],
            "delta_f1": ours_metrics["f1"] - base_metrics["f1"],
            "delta_judge_overall": _delta(
                ours_judge.get("overall"), base_judge.get("overall"),
            ),
        })

        # Persist each run for forensic inspection.
        Path("examples/outputs").mkdir(parents=True, exist_ok=True)
        Path(f"examples/outputs/{bp.id}_ours.json").write_text(
            ours.model_dump_json(indent=2)
        )
        Path(f"examples/outputs/{bp.id}_baseline.json").write_text(
            base.model_dump_json(indent=2)
        )

    Path(out).write_text(json.dumps(rows, indent=2, default=str))
    _print_table(rows)
    console.print(f"\n[dim]Wrote {out}[/dim]")


def _score_ours(cfg, rr: ResearchRun) -> dict:
    """Compute metrics for the structured side. Defensive against rr.report=None."""
    if not rr.report:
        return {"grounding": 0.0, "coverage": 0.0, "f1": 0.0,
                "n_claims": 0, "n_sources": 0,
                "calibration": {"slope": None, "spread": 0.0, "n": 0,
                                "interpretable": False}}
    g = grounding_rate(cfg, rr.report, rr.findings)
    c = coverage_rate(rr)
    cal = calibration_slope(rr.report, g["details"])
    return {
        "grounding": g["rate"],
        "coverage": c["rate"],
        "f1": f_score(g["rate"], c["rate"]),
        "n_claims": len(rr.report.claims),
        "n_sources": len(rr.findings.all_sources()),
        "calibration": cal,
    }


def _score_baseline(cfg, br: BaselineRun) -> dict:
    """Compute metrics for the baseline side.

    Grounding via grounding_freeform; coverage = % sub-questions with a
    non-empty summary; calibration is N/A (no per-claim confidence labels).
    """
    g = grounding_rate_freeform(cfg, br)
    sq_with_summary = sum(
        1 for s in br.report.summaries
        if s.summary_text and s.summary_text != "(no summary produced)"
    )
    coverage = sq_with_summary / max(1, len(br.decomposition.subquestions))
    return {
        "grounding": g["rate"],
        "coverage": coverage,
        "f1": f_score(g["rate"], coverage),
        "n_claims": g["n_claims"],
        "n_sources": len(br.report.all_sources),
        # Baseline has no per-claim confidence labels — calibration is N/A.
        "calibration": {"slope": None, "spread": 0.0, "n": 0,
                        "interpretable": False},
    }


def _judge_baseline(cfg, br: BaselineRun, bp) -> dict:
    """Wrap the baseline output in a synthesized ResearchRun so the judge
    interface is uniform.

    Each baseline summary becomes one synthetic ReportClaim with
    confidence=0.5 and no supporting indices; the baseline's
    final_report_text becomes report.summary (truncated to 1500 chars).
    Same prompt, rubric, and model on both sides.
    """
    from src.state import (
        ResearchRun, FindingsStore, Report, ReportClaim, Decomposition,
    )
    synthetic_claims = [
        ReportClaim(claim=s.summary_text[:500], confidence=0.5,
                    supporting_finding_indices=[])
        for s in br.report.summaries
    ]
    fake_run = ResearchRun(
        user_prompt=br.user_prompt,
        decomposition=br.decomposition,
        findings=FindingsStore(),
        report=Report(
            user_prompt=br.user_prompt,
            summary=br.report.final_report_text[:1500],
            claims=synthetic_claims,
        ),
    )
    return llm_judge(cfg, fake_run, rubric=bp.rubric)


def _delta(a, b):
    """Compute a - b, returning None if either is None."""
    if a is None or b is None:
        return None
    return a - b


def _print_table(rows: list[dict]):
    """Render the comparison as a wide Rich table."""
    table = Table(title="OURS vs. GPT-Researcher-style baseline")
    table.add_column("id"); table.add_column("cat")
    table.add_column("g O"); table.add_column("g B")
    table.add_column("c O"); table.add_column("c B")
    table.add_column("F1 O"); table.add_column("F1 B"); table.add_column("Δ F1")
    table.add_column("calib O")
    table.add_column("judge O"); table.add_column("judge B")
    table.add_column("t O"); table.add_column("t B")

    for r in rows:
        oj = r["ours"]["judge"].get("overall") if isinstance(r["ours"]["judge"], dict) else None
        bj = r["baseline"]["judge"].get("overall") if isinstance(r["baseline"]["judge"], dict) else None
        table.add_row(
            r["prompt_id"], r["category"],
            f"{r['ours']['grounding']:.0%}",
            f"{r['baseline']['grounding']:.0%}",
            f"{r['ours']['coverage']:.0%}",
            f"{r['baseline']['coverage']:.0%}",
            f"{r['ours']['f1']:.0%}",
            f"{r['baseline']['f1']:.0%}",
            f"{r['delta_f1']:+.0%}",
            fmt_slope(r['ours']['calibration']),
            f"{oj:.1f}" if oj is not None else "—",
            f"{bj:.1f}" if bj is not None else "—",
            f"{r['ours']['elapsed_s']:.0f}s",
            f"{r['baseline']['elapsed_s']:.0f}s",
        )
    console.print(table)
    console.print(
        "\n[dim]Legend:[/dim] g=grounding, c=coverage, F1=harmonic mean, "
        "calib=slope of confidence vs grounded (≈1 well-calibrated, "
        "≈0 meaningless, 'flat'=labels collapsed)."
    )


if __name__ == "__main__":
    app()
