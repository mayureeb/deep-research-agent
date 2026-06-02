"""Run the eval suite over the curated prompts and write a JSON report.

For each benchmark prompt: run the full pipeline, score every metric
(grounding, coverage, F1, calibration curve, calibration slope, LLM judge),
write per-prompt run JSON, and aggregate into a summary.

The reproducibility mode runs the same prompt 3 times and computes
claim-Jaccard across the runs.

Usage:
    python -m eval.run_eval                          # all prompts
    python -m eval.run_eval --only easy-1            # one prompt
    python -m eval.run_eval --reproducibility easy-1 # 3 runs, claim-jaccard
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
from src.agent.orchestrator import run_research
from .prompts import PROMPTS
from .metrics.aggregates import calibration_slope, f_score, fmt_slope
from .metrics.grounding import grounding_rate
from .metrics.coverage import coverage_rate
from .metrics.calibration import calibration_curve
from .metrics.judge import llm_judge
from .metrics.reproducibility import claim_jaccard, subquestion_jaccard


app = typer.Typer(add_completion=False)
console = Console()


@app.command()
def main(
    only: str = typer.Option(None, help="Run a single prompt by id"),
    out: str = typer.Option("eval_results.json", help="Output JSON path"),
    reproducibility: str = typer.Option(
        None, help="Run this prompt id 3 times and compute Jaccard"
    ),
):
    """Run the eval suite, a single-prompt subset, or a reproducibility run."""
    cfg = config_module.load()
    selected = (
        [p for p in PROMPTS if p.id == only] if only else PROMPTS
    )
    if not selected:
        console.print(f"[red]No prompt with id {only}[/red]")
        raise typer.Exit(1)

    if reproducibility:
        _run_reproducibility(cfg, reproducibility, out)
        return

    results = []
    for bp in selected:
        console.print(f"\n[bold cyan]Running {bp.id} ({bp.category})...[/bold cyan]")
        t0 = time.time()
        # Sequential per prompt — parallel prompts would hit rate limits.
        rr = asyncio.run(run_research(cfg, bp.prompt))
        elapsed = time.time() - t0

        console.print(f"  generated {len(rr.report.claims if rr.report else [])} claims "
                      f"in {elapsed:.1f}s — scoring metrics...")

        # Each metric is independent; failures surface in its output.
        ground = grounding_rate(cfg, rr.report, rr.findings) if rr.report else {"rate": 0.0, "details": []}
        cov = coverage_rate(rr)
        # curve = bucketed confidence vs grounded; slope = best-fit line.
        calib_curve = calibration_curve(cfg, rr.report, rr.findings) if rr.report else {}
        calib_slope = calibration_slope(rr.report, ground.get("details", []))
        judge = llm_judge(cfg, rr, rubric=bp.rubric)
        f1 = f_score(ground["rate"], cov["rate"])

        results.append({
            "prompt_id": bp.id,
            "category": bp.category,
            "elapsed_seconds": elapsed,
            "n_claims": len(rr.report.claims) if rr.report else 0,
            "grounding_rate": ground["rate"],
            "coverage_rate": cov["rate"],
            "f1": f1,
            "missed_subquestions": cov["missed"],
            "calibration_curve": calib_curve,
            "calibration_slope": calib_slope,
            "judge": judge,
            "run_metrics": rr.metrics.model_dump() if rr.metrics else None,
        })

        # Persist the full ResearchRun so inspect.py can drill in later.
        run_path = Path("examples/outputs") / f"{bp.id}.json"
        run_path.parent.mkdir(parents=True, exist_ok=True)
        run_path.write_text(rr.model_dump_json(indent=2))

    Path(out).write_text(json.dumps(results, indent=2, default=str))
    _print_summary(results)
    console.print(f"\n[dim]Wrote results to {out}[/dim]")


def _run_reproducibility(cfg, prompt_id: str, out: str):
    """Run one prompt 3x and compute pairwise claim- and sub-question-Jaccard.

    Output contains the Jaccard summary and all 3 ResearchRuns for manual
    comparison.
    """
    bp = next((p for p in PROMPTS if p.id == prompt_id), None)
    if not bp:
        console.print(f"[red]Unknown prompt id {prompt_id}[/red]")
        raise typer.Exit(1)
    console.print(f"[bold]Reproducibility run: {prompt_id} x 3[/bold]")
    runs = []
    for i in range(3):
        console.print(f"  run {i+1}/3...")
        runs.append(asyncio.run(run_research(cfg, bp.prompt)))
    jac = claim_jaccard(runs)
    sq_jac = subquestion_jaccard(runs)
    console.print(f"\nMean pairwise claim Jaccard:        {jac['mean_jaccard']:.3f}")
    console.print(f"Mean pairwise sub-question Jaccard: {sq_jac['mean_jaccard']:.3f}")
    # Diagnostics: low both → planner instability; stable plan + unstable
    # claims → instability is downstream of the planner.
    if sq_jac['mean_jaccard'] < 0.6 and jac['mean_jaccard'] < 0.6:
        console.print(
            "[yellow]Both Jaccards are low; planner randomness is a likely "
            "upstream contributor.[/yellow]"
        )
    elif sq_jac['mean_jaccard'] >= 0.7 and jac['mean_jaccard'] < 0.5:
        console.print(
            "[yellow]Plan is stable but claims aren't — instability is downstream "
            "of the planner (researcher / writer randomness).[/yellow]"
        )
    Path(out).write_text(json.dumps({
        "prompt_id": prompt_id,
        "claim_jaccard": jac,
        "subquestion_jaccard": sq_jac,
        "runs": [rr.model_dump(mode="json") for rr in runs],
    }, indent=2, default=str))


def _print_summary(results: list[dict]):
    """Render the eval summary as a Rich table."""
    table = Table(title="Eval summary")
    table.add_column("id"); table.add_column("cat"); table.add_column("claims")
    table.add_column("ground"); table.add_column("cover"); table.add_column("F1")
    table.add_column("calib"); table.add_column("judge")
    for r in results:
        judge_overall = r["judge"].get("overall") if isinstance(r["judge"], dict) else None
        table.add_row(
            r["prompt_id"], r["category"], str(r["n_claims"]),
            f"{r['grounding_rate']:.0%}",
            f"{r['coverage_rate']:.0%}",
            f"{r['f1']:.0%}",
            fmt_slope(r['calibration_slope']),
            f"{judge_overall:.1f}" if judge_overall is not None else "—",
        )
    console.print(table)
    console.print(
        "\n[dim]F1 = harmonic mean of grounding & coverage. "
        "calib slope ≈ 1: well-calibrated; ≈ 0: meaningless labels; "
        "'flat' = the model collapsed to one confidence value.[/dim]"
    )


if __name__ == "__main__":
    app()
