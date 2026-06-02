"""Ablation: critic ON vs OFF. Runs each prompt twice and prints metric deltas.

"Critic OFF" sets max_revisions=0 — one critic pass runs but never
revises, so the writer's draft is the final word.

Usage:
    python -m eval.ablation                  # all prompts
    python -m eval.ablation --only easy-1
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
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


app = typer.Typer(add_completion=False)
console = Console()


@app.command()
def main(
    only: str = typer.Option(None, help="Run a single prompt by id"),
    out: str = typer.Option("ablation_results.json", help="Output path"),
):
    """Run the critic ON vs OFF ablation across all prompts (or one)."""
    cfg = config_module.load()
    # replace returns a NEW frozen Config; only max_revisions differs.
    cfg_no_critic = replace(cfg, max_revisions=0)

    selected = [p for p in PROMPTS if p.id == only] if only else PROMPTS
    if not selected:
        console.print(f"[red]No prompt with id {only}[/red]")
        raise typer.Exit(1)

    rows = []
    for bp in selected:
        # WITH-critic run.
        console.print(f"\n[bold]{bp.id}[/bold] — running WITH critic...")
        t0 = time.time()
        rr_with = asyncio.run(run_research(cfg, bp.prompt))
        with_t = time.time() - t0
        with_g = grounding_rate(cfg, rr_with.report, rr_with.findings) if rr_with.report else {"rate": 0.0, "details": []}
        with_cov = coverage_rate(rr_with)["rate"]
        with_cal = calibration_slope(rr_with.report, with_g.get("details", []))

        # WITHOUT-critic run. Same cfg for the grounding scorer keeps
        # the comparison apples-to-apples.
        console.print(f"[bold]{bp.id}[/bold] — running WITHOUT critic...")
        t0 = time.time()
        rr_without = asyncio.run(run_research(cfg_no_critic, bp.prompt))
        without_t = time.time() - t0
        without_g = grounding_rate(cfg, rr_without.report, rr_without.findings) if rr_without.report else {"rate": 0.0, "details": []}
        without_cov = coverage_rate(rr_without)["rate"]
        without_cal = calibration_slope(rr_without.report, without_g.get("details", []))

        rows.append({
            "prompt_id": bp.id,
            "with_critic": {
                "grounding": with_g["rate"], "coverage": with_cov,
                "f1": f_score(with_g["rate"], with_cov),
                "elapsed_s": with_t,
                "claims": len(rr_with.report.claims if rr_with.report else []),
                "calibration": with_cal,
            },
            "without_critic": {
                "grounding": without_g["rate"], "coverage": without_cov,
                "f1": f_score(without_g["rate"], without_cov),
                "elapsed_s": without_t,
                "claims": len(rr_without.report.claims if rr_without.report else []),
                "calibration": without_cal,
            },
            # Deltas: positive Δgrounding = critic helped; negative
            # Δcoverage = critic dropped some claims.
            "delta_grounding": with_g["rate"] - without_g["rate"],
            "delta_coverage": with_cov - without_cov,
            "delta_f1": f_score(with_g["rate"], with_cov) - f_score(without_g["rate"], without_cov),
        })

    Path(out).write_text(json.dumps(rows, indent=2, default=str))

    # ΔF1 is the headline — accounts for both grounding and coverage.
    table = Table(title="Ablation: critic ON vs OFF")
    table.add_column("id")
    table.add_column("g on"); table.add_column("g off"); table.add_column("Δg")
    table.add_column("c on"); table.add_column("c off"); table.add_column("Δc")
    table.add_column("F1 on"); table.add_column("F1 off"); table.add_column("ΔF1")
    table.add_column("calib on"); table.add_column("calib off")
    table.add_column("t on"); table.add_column("t off")
    for r in rows:
        table.add_row(
            r["prompt_id"],
            f"{r['with_critic']['grounding']:.0%}",
            f"{r['without_critic']['grounding']:.0%}",
            f"{r['delta_grounding']:+.0%}",
            f"{r['with_critic']['coverage']:.0%}",
            f"{r['without_critic']['coverage']:.0%}",
            f"{r['delta_coverage']:+.0%}",
            f"{r['with_critic']['f1']:.0%}",
            f"{r['without_critic']['f1']:.0%}",
            f"{r['delta_f1']:+.0%}",
            fmt_slope(r['with_critic']['calibration']),
            fmt_slope(r['without_critic']['calibration']),
            f"{r['with_critic']['elapsed_s']:.0f}s",
            f"{r['without_critic']['elapsed_s']:.0f}s",
        )
    console.print(table)
    console.print(
        "\n[dim]g=grounding, c=coverage, F1=harmonic mean of g & c, "
        "calib=slope of confidence vs grounded ('flat'=labels collapsed).[/dim]"
    )
    console.print(f"\n[dim]Wrote {out}[/dim]")


if __name__ == "__main__":
    app()
