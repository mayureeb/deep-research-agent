"""Ablation runner: critic ON vs OFF, verifier ON vs OFF, TrACE ON vs OFF.

Runs the same prompts twice (with and without the targeted component) and
prints metric deltas. Selected by ``--mode {critic,verifier,trace}``.
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
    out: str = typer.Option(None, help="Output path (default: <mode>_results.json)"),
    mode: str = typer.Option(
        "critic",
        help="Ablation target: 'critic' (max_revisions=0 vs default), "
             "'verifier' (cfg.verifier_enabled=False vs True), "
             "or 'trace' (cfg.trace_enabled=True vs False)",
    ),
    with_trace8: bool = typer.Option(
        False,
        "--with-trace8",
        help="For --mode trace: use TrACE-8 (kmax=8) instead of TrACE-4 "
             "in the WITH cell.",
    ),
):
    """Run an ON vs OFF ablation across all prompts (or one)."""
    if mode not in ("critic", "verifier", "trace"):
        console.print(
            f"[red]Unknown mode {mode}; expected 'critic', 'verifier', or 'trace'.[/red]"
        )
        raise typer.Exit(1)
    if with_trace8 and mode != "trace":
        console.print(
            "[red]--with-trace8 only valid with --mode trace.[/red]"
        )
        raise typer.Exit(1)
    cfg = config_module.load()

    if mode == "critic":
        cfg_with = cfg
        cfg_off = replace(cfg, max_revisions=0)
        with_label, without_label = "with_critic", "without_critic"
        title = "Ablation: critic ON vs OFF"
    elif mode == "verifier":
        cfg_with = cfg
        cfg_off = replace(cfg, verifier_enabled=False)
        with_label, without_label = "with_verifier", "without_verifier"
        title = "Ablation: verifier ON vs OFF"
    else:
        k_max = 8 if with_trace8 else 4
        cfg_with = replace(cfg, trace_enabled=True, trace_k_max=k_max)
        cfg_off = replace(cfg, trace_enabled=False)
        with_label, without_label = "with_trace", "without_trace"
        title = f"Ablation: TrACE-{k_max} ON vs OFF"

    if out is None:
        out = f"ablation_{mode}_results.json"

    selected = [p for p in PROMPTS if p.id == only] if only else PROMPTS
    if not selected:
        console.print(f"[red]No prompt with id {only}[/red]")
        raise typer.Exit(1)

    rows = []
    for bp in selected:
        console.print(f"\n[bold]{bp.id}[/bold] — running WITH {mode}...")
        t0 = time.time()
        rr_with = asyncio.run(run_research(cfg_with, bp.prompt))
        with_t = time.time() - t0
        with_g = grounding_rate(cfg, rr_with.report, rr_with.findings) if rr_with.report else {"rate": 0.0, "details": []}
        with_cov = coverage_rate(rr_with)["rate"]
        with_cal = calibration_slope(rr_with.report, with_g.get("details", []))

        console.print(f"[bold]{bp.id}[/bold] — running WITHOUT {mode}...")
        t0 = time.time()
        rr_without = asyncio.run(run_research(cfg_off, bp.prompt))
        without_t = time.time() - t0
        without_g = grounding_rate(cfg, rr_without.report, rr_without.findings) if rr_without.report else {"rate": 0.0, "details": []}
        without_cov = coverage_rate(rr_without)["rate"]
        without_cal = calibration_slope(rr_without.report, without_g.get("details", []))

        with_cost = rr_with.metrics.total_cost_usd if rr_with.metrics else 0.0
        without_cost = rr_without.metrics.total_cost_usd if rr_without.metrics else 0.0

        with_block = {
            "grounding": with_g["rate"], "coverage": with_cov,
            "f1": f_score(with_g["rate"], with_cov),
            "elapsed_s": with_t,
            "claims": len(rr_with.report.claims if rr_with.report else []),
            "calibration": with_cal,
            "cost_usd": with_cost,
        }
        without_block = {
            "grounding": without_g["rate"], "coverage": without_cov,
            "f1": f_score(without_g["rate"], without_cov),
            "elapsed_s": without_t,
            "claims": len(rr_without.report.claims if rr_without.report else []),
            "calibration": without_cal,
            "cost_usd": without_cost,
        }
        if mode == "trace":
            ts = rr_with.metrics.trace_stats if rr_with.metrics else None
            with_block["trace_stats"] = ts.model_dump() if ts is not None else None

        rows.append({
            "prompt_id": bp.id,
            "mode": mode,
            with_label: with_block,
            without_label: without_block,
            "delta_grounding": with_g["rate"] - without_g["rate"],
            "delta_coverage": with_cov - without_cov,
            "delta_f1": f_score(with_g["rate"], with_cov) - f_score(without_g["rate"], without_cov),
            "delta_cost_usd": with_cost - without_cost,
        })

    Path(out).write_text(json.dumps(rows, indent=2, default=str))

    table = Table(title=title)
    table.add_column("id")
    table.add_column("g on"); table.add_column("g off"); table.add_column("Δg")
    table.add_column("c on"); table.add_column("c off"); table.add_column("Δc")
    table.add_column("F1 on"); table.add_column("F1 off"); table.add_column("ΔF1")
    table.add_column("calib on"); table.add_column("calib off")
    table.add_column("t on"); table.add_column("t off")
    table.add_column("$ on"); table.add_column("$ off"); table.add_column("Δ$")
    if mode == "trace":
        table.add_column("mean_k"); table.add_column("α")
    for r in rows:
        on = r[with_label]; off = r[without_label]
        row_cells = [
            r["prompt_id"],
            f"{on['grounding']:.0%}", f"{off['grounding']:.0%}",
            f"{r['delta_grounding']:+.0%}",
            f"{on['coverage']:.0%}", f"{off['coverage']:.0%}",
            f"{r['delta_coverage']:+.0%}",
            f"{on['f1']:.0%}", f"{off['f1']:.0%}",
            f"{r['delta_f1']:+.0%}",
            fmt_slope(on['calibration']), fmt_slope(off['calibration']),
            f"{on['elapsed_s']:.0f}s", f"{off['elapsed_s']:.0f}s",
            f"${on['cost_usd']:.3f}", f"${off['cost_usd']:.3f}",
            f"{r['delta_cost_usd']:+.3f}",
        ]
        if mode == "trace":
            ts = on.get("trace_stats") or {}
            row_cells.append(f"{ts.get('mean_k', 0):.2f}")
            row_cells.append(f"{ts.get('mean_alpha', 0):.2f}")
        table.add_row(*row_cells)
    console.print(table)
    note = (
        f"\n[dim]g=grounding, c=coverage, F1=harmonic mean of g & c, "
        f"calib=slope of confidence vs grounded ('flat'=labels collapsed), "
        f"$=total_cost_usd from RunMetrics."
    )
    if mode == "trace":
        note += (
            " For TrACE: mean_k is total_k/steps (kinit floor, kmax ceiling); "
            "α is the average plurality fraction at commit."
        )
    note += "[/dim]"
    console.print(note)
    console.print(f"\n[dim]Wrote {out}[/dim]")


if __name__ == "__main__":
    app()
