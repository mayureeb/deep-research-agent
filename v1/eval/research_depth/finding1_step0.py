"""Plan-instability vs claim-instability correlation study.

For each prompt, runs the pipeline N times and computes pairwise
Jaccards across the runs at each layer of the chain (planner sub-
questions, harvested URLs, researcher findings, writer claims). Then
correlates the layer-Jaccards against grounding / F1 across prompts.

Modes: ``--orchestrate`` runs the prompts (expensive); ``--analyze-only``
re-derives the correlations from cached per-prompt JSONs.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from dataclasses import replace

from src import config as config_module
from src.agent.orchestrator import run_research

from src.state import ResearchRun
from ..metrics.grounding import grounding_rate
from ..metrics.coverage import coverage_rate
from ..metrics.aggregates import f_score
from ..metrics.reproducibility import (
    claim_jaccard,
    subquestion_jaccard,
    claim_token_jaccard,
    subquestion_token_jaccard,
    finding_token_jaccard,
    unique_source_stats,
)
from ..prompts import PROMPTS

app = typer.Typer(add_completion=False)
console = Console()

DATA_DIR = Path(__file__).parent / "data"


@app.command()
def main(
    runs: int = typer.Option(
        3, "--runs",
        help="N repetitions per prompt.",
    ),
    only: list[str] = typer.Option(
        None, "--only",
        help="Restrict to specific prompt ids. Repeat the flag, e.g. "
             "`--only easy-1 --only contradictory-1`. Default: all 8.",
    ),
    orchestrate: bool = typer.Option(
        False, "--orchestrate",
        help="Actually run the LLM pipelines. Default is False so "
             "re-running this script is cheap; flip on for the first "
             "(expensive) sweep.",
    ),
    analyze_only: bool = typer.Option(
        False, "--analyze-only",
        help="Skip orchestration entirely and only load existing "
             "per-prompt JSONs. Useful for re-rendering the table after "
             "tweaking thresholds.",
    ),
    out: str = typer.Option(
        "finding1_step0_results.json",
        help="Where to write the aggregate analysis JSON.",
    ),
    full_pipeline: bool = typer.Option(
        False, "--full-pipeline",
        help="Run the WHOLE pipeline including critic revisions + "
             "verifier. Default is draft-only mode (max_revisions=0, "
             "verifier_enabled=False) which (a) preserves the writer's "
             "first-draft claims — the cleanest test of the planner → "
             "writer causal chain — and (b) saves ~10-25%% per run. "
             "Use --full-pipeline if you want to measure the production "
             "system's stability rather than the planner→writer link.",
    ),
    strict_jaccard: bool = typer.Option(
        False, "--strict-jaccard",
        help="Use whole-string Jaccard (the original v1 metric) as the "
             "study variable instead of token-level best-match. The "
             "string version floors at 0.0 across every prompt because "
             "the LLM rephrases everything; provided here only for "
             "regression / sanity-checking against prior runs.",
    ),
):
    """Run the Finding-1 / Step-0 correlational study.

    Without --orchestrate (default): assumes per-prompt JSONs live under
    eval/research_depth/data/ from a prior --orchestrate run, loads them,
    computes correlations.

    With --orchestrate: runs each selected prompt N times, writes one
    JSON per prompt under eval/research_depth/data/, then computes the
    correlations.
    """
    cfg = config_module.load()
    if not full_pipeline:
        # Draft-only mode: stop the orchestrator at the writer's first
        # claim emission. max_revisions=0 still runs ONE critic call
        # (the loop exits before any revision), but it doesn't touch
        # rr.report.claims — we measure the writer's draft.
        cfg = replace(cfg, max_revisions=0, verifier_enabled=False)
        console.print(
            "[dim]Mode: draft-only (max_revisions=0, verifier_enabled=False). "
            "Pass --full-pipeline to include critic revisions + verifier.[/dim]"
        )
    selected = (
        [p for p in PROMPTS if p.id in only]
        if only
        else PROMPTS
    )
    if not selected:
        console.print(f"[red]No matching prompts for --only={only}[/red]")
        raise typer.Exit(1)
    if analyze_only and orchestrate:
        console.print("[red]--orchestrate and --analyze-only are mutually exclusive[/red]")
        raise typer.Exit(1)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if orchestrate:
        _orchestrate_sweep(cfg, selected, runs)

    rows = _collect_per_prompt_rows(selected, use_strict=strict_jaccard)
    if not rows:
        console.print(
            "[yellow]No per-prompt data on disk. Re-run with --orchestrate "
            "to populate eval/research_depth/data/.[/yellow]"
        )
        raise typer.Exit(1)

    correlations = _compute_correlations(rows)
    _print_results(rows, correlations, use_strict=strict_jaccard)

    Path(out).write_text(json.dumps({
        "n_prompts": len(rows),
        "per_prompt": rows,
        "correlations": correlations,
    }, indent=2, default=str))
    console.print(f"\n[dim]Wrote analysis to {out}[/dim]")


def _orchestrate_sweep(cfg, selected, runs: int) -> None:
    """Run each prompt `runs` times and write a per-prompt JSON to disk."""
    total = len(selected) * runs
    console.print(
        f"[bold cyan]Orchestrating {len(selected)} prompts × {runs} runs "
        f"= {total} full pipeline executions[/bold cyan]"
    )
    for bp in selected:
        outfile = DATA_DIR / f"{bp.id}_repro.json"
        console.print(f"\n[bold]{bp.id}[/bold] ({bp.category}) → {outfile.name}")
        rrs = []
        per_run = []
        for i in range(runs):
            console.print(f"  run {i+1}/{runs}...")
            t0 = time.time()
            rr = asyncio.run(run_research(cfg, bp.prompt))
            elapsed = time.time() - t0
            ground = (
                grounding_rate(cfg, rr.report, rr.findings)
                if rr.report else {"rate": 0.0}
            )
            cov = coverage_rate(rr)
            rrs.append(rr)
            per_run.append({
                "run_index": i,
                "elapsed_seconds": elapsed,
                "n_claims": len(rr.report.claims) if rr.report else 0,
                "n_subquestions": len(rr.decomposition.subquestions),
                "grounding_rate": ground["rate"],
                "coverage_rate": cov["rate"],
                "f1": f_score(ground["rate"], cov["rate"]),
            })
        sq_jac = subquestion_jaccard(rrs)
        cl_jac = claim_jaccard(rrs)
        sq_tok = subquestion_token_jaccard(rrs)
        cl_tok = claim_token_jaccard(rrs)
        find_tok = finding_token_jaccard(rrs)
        src_stats = unique_source_stats(rrs)
        harvested_url_jac = (
            src_stats.get("harvested_url_jaccard", {}).get("mean_jaccard", 0.0)
        )
        outfile.write_text(json.dumps({
            "prompt_id": bp.id,
            "category": bp.category,
            "n_runs": runs,
            "mode": (
                "full_pipeline" if cfg.max_revisions > 0 else "draft_only"
            ),
            "max_revisions": cfg.max_revisions,
            "verifier_enabled": cfg.verifier_enabled,
            "subquestion_jaccard": sq_jac["mean_jaccard"],
            "claim_jaccard": cl_jac["mean_jaccard"],
            "subquestion_jaccard_token": sq_tok["mean_jaccard"],
            "claim_jaccard_token": cl_tok["mean_jaccard"],
            "finding_jaccard_token": find_tok["mean_jaccard"],
            "harvested_url_jaccard": harvested_url_jac,
            "per_run": per_run,
            "runs": [rr.model_dump(mode="json") for rr in rrs],
        }, indent=2, default=str))


def _collect_per_prompt_rows(selected, *, use_strict: bool = False) -> list[dict]:
    """Build one row per prompt from the per-prompt JSONs on disk.

    Token-Jaccard sourcing: prefer pre-computed fields if present;
    otherwise reconstruct ResearchRuns from saved ``runs`` and compute
    on the fly.
    """
    rows = []
    for bp in selected:
        f = DATA_DIR / f"{bp.id}_repro.json"
        if not f.exists():
            continue
        d = json.loads(f.read_text())
        per_run = d.get("per_run", [])
        if not per_run:
            continue

        sq_strict = float(d.get("subquestion_jaccard", 0.0))
        cl_strict = float(d.get("claim_jaccard", 0.0))

        sq_token         = d.get("subquestion_jaccard_token")
        cl_token         = d.get("claim_jaccard_token")
        find_token       = d.get("finding_jaccard_token")
        harvested_url_j  = d.get("harvested_url_jaccard")
        need_recompute = any(
            x is None for x in (sq_token, cl_token, find_token, harvested_url_j)
        )
        if need_recompute:
            run_dicts = d.get("runs", [])
            try:
                rrs = [ResearchRun.model_validate(rd) for rd in run_dicts]
            except Exception:
                rrs = []
            if len(rrs) >= 2:
                if sq_token is None:
                    sq_token = subquestion_token_jaccard(rrs)["mean_jaccard"]
                if cl_token is None:
                    cl_token = claim_token_jaccard(rrs)["mean_jaccard"]
                if find_token is None:
                    find_token = finding_token_jaccard(rrs)["mean_jaccard"]
                if harvested_url_j is None:
                    src_stats = unique_source_stats(rrs)
                    harvested_url_j = (
                        src_stats.get("harvested_url_jaccard", {})
                                 .get("mean_jaccard", 0.0)
                    )
            sq_token        = sq_token        if sq_token        is not None else 0.0
            cl_token        = cl_token        if cl_token        is not None else 0.0
            find_token      = find_token      if find_token      is not None else 0.0
            harvested_url_j = harvested_url_j if harvested_url_j is not None else 0.0

        sq_head, cl_head = (
            (sq_strict, cl_strict) if use_strict
            else (float(sq_token), float(cl_token))
        )

        rows.append({
            "prompt_id": d["prompt_id"],
            "category": d.get("category", ""),
            "n_runs": d["n_runs"],
            "subquestion_jaccard": sq_head,
            "claim_jaccard": cl_head,
            "subquestion_jaccard_strict": sq_strict,
            "claim_jaccard_strict": cl_strict,
            "subquestion_jaccard_token":  float(sq_token),
            "claim_jaccard_token":        float(cl_token),
            "finding_jaccard_token":      float(find_token),
            "harvested_url_jaccard":      float(harvested_url_j),
            "mean_grounding_rate": _mean(r["grounding_rate"] for r in per_run),
            "mean_coverage_rate":  _mean(r["coverage_rate"] for r in per_run),
            "mean_f1":             _mean(r["f1"] for r in per_run),
            "mean_n_claims":       _mean(r["n_claims"] for r in per_run),
        })
    return rows


def _compute_correlations(rows: list[dict]) -> dict:
    """Pearson correlations across the prompts.

    Returns three: (sq_jac, claim_jac), (sq_jac, mean_grounding),
    (sq_jac, mean_f1). With N=8 prompts even |r|=0.7 isn't significant
    at p<0.05 — this is a direction signal, not a hypothesis test.
    """
    if len(rows) < 3:
        # Below 3 the correlation is undefined / degenerate; skip rather
        # than mislead.
        return {
            "n_prompts": len(rows),
            "note": "Need ≥3 prompts to compute Pearson correlation.",
        }

    sq    = [r["subquestion_jaccard"]      for r in rows]
    cl    = [r["claim_jaccard"]             for r in rows]
    gr    = [r["mean_grounding_rate"]       for r in rows]
    f1    = [r["mean_f1"]                   for r in rows]
    fnd   = [r["finding_jaccard_token"]     for r in rows]
    hurl  = [r["harvested_url_jaccard"]     for r in rows]

    def _safe_corr(xs, ys):
        # statistics.correlation throws if either series is constant.
        # That's a possible degenerate case here (e.g. all sq_jac = 1.0
        # if N=2 and identical), so guard.
        try:
            return statistics.correlation(xs, ys)
        except statistics.StatisticsError as e:
            return {"error": str(e)}

    return {
        "n_prompts": len(rows),
        "corr_subqJac_claimJac":         _safe_corr(sq, cl),
        "corr_subqJac_meanGrounding":    _safe_corr(sq, gr),
        "corr_subqJac_meanF1":           _safe_corr(sq, f1),
        "corr_findingJac_claimJac":      _safe_corr(fnd, cl),
        "corr_harvestedUrl_findingJac":  _safe_corr(hurl, fnd),
    }


def _print_results(rows: list[dict], corr: dict, *, use_strict: bool = False) -> None:
    metric_kind = "strict (whole-string)" if use_strict else "token (best-match)"
    console.print(
        f"[dim]Jaccard metric: {metric_kind}. "
        + (
            "Pass --strict-jaccard to use whole-string."
            if not use_strict
            else "Drop --strict-jaccard to use token best-match (default)."
        )
        + "[/dim]"
    )
    table = Table(title=f"Finding 1 / Step 0 — per prompt (N={len(rows)})")
    table.add_column("id"); table.add_column("cat")
    table.add_column(f"sq jac\n[{metric_kind.split()[0]}]", justify="right")
    table.add_column("harvest\nurl jac", justify="right")
    table.add_column("finding\njac [tok]", justify="right")
    table.add_column(f"claim jac\n[{metric_kind.split()[0]}]", justify="right")
    table.add_column("grounding (mean)", justify="right")
    table.add_column("F1 (mean)", justify="right")
    table.add_column("#claims (mean)", justify="right")
    for r in rows:
        table.add_row(
            r["prompt_id"], r["category"],
            f"{r['subquestion_jaccard']:.3f}",
            f"{r['harvested_url_jaccard']:.3f}",
            f"{r['finding_jaccard_token']:.3f}",
            f"{r['claim_jaccard']:.3f}",
            f"{r['mean_grounding_rate']:.1%}",
            f"{r['mean_f1']:.1%}",
            f"{r['mean_n_claims']:.1f}",
        )
    console.print(table)

    # Correlation block + decision rule.
    console.print("\n[bold]Pearson correlations across prompts:[/bold]")

    if corr.get("note"):
        # n_prompts < 3 path — _compute_correlations bailed out and
        # emitted a note instead of corr keys. Surface the note and skip
        # the per-line printing (would be all "n/a" anyway).
        console.print(f"  [yellow]{corr['note']}[/yellow]  (n_prompts={corr.get('n_prompts')})")
        console.print(
            "  [dim]Re-run with `--only` adding more prompt ids "
            "(or drop the --only flags to sweep all 8) to get a "
            "computable correlation.[/dim]"
        )
        return

    def _fmt(x):
        if x is None:
            return "[dim]n/a[/dim]"
        if isinstance(x, dict):
            return f"[red]err: {x.get('error')}[/red]"
        return _color_corr(x)

    console.print(f"  corr(sq_jac,        claim_jac)     = {_fmt(corr.get('corr_subqJac_claimJac'))}")
    console.print(f"  corr(sq_jac,        mean_grounding)= {_fmt(corr.get('corr_subqJac_meanGrounding'))}")
    console.print(f"  corr(sq_jac,        mean_F1)       = {_fmt(corr.get('corr_subqJac_meanF1'))}")
    console.print(f"  corr(finding_jac,   claim_jac)     = {_fmt(corr.get('corr_findingJac_claimJac'))}  [dim]→ writer faithfulness[/dim]")
    console.print(f"  corr(harvested_url, finding_jac)   = {_fmt(corr.get('corr_harvestedUrl_findingJac'))}  [dim]→ extraction faithfulness[/dim]")

    headline = corr.get("corr_subqJac_claimJac")
    if isinstance(headline, (int, float)):
        console.print("\n[bold]Decision rule:[/bold]")
        if abs(headline) < 0.4:
            console.print(
                "  [yellow]|r| < 0.4 — planner instability looks BENIGN.[/yellow] "
                "Plans swing without dragging claim quality. "
                "Don't fix Finding 1; spend the budget elsewhere."
            )
        elif headline > 0.6:
            console.print(
                "  [green]r > 0.6 — plan stability predicts claim stability.[/green] "
                "Finding 1 is real — Moves 1-4 from the plan are on the table."
            )
        elif headline < -0.6:
            console.print(
                "  [red]r < -0.6 — surprising NEGATIVE relationship.[/red] "
                "Stable plans yield UNSTABLE claims. Worth investigating."
            )
        else:
            console.print(
                "  [yellow]Borderline (0.4 ≤ |r| ≤ 0.6).[/yellow] "
                "Either bump N runs / prompts, or eyeball the per-prompt "
                "table for outliers driving the headline."
            )
        console.print(
            "[dim]Caveat: with N=" + str(corr["n_prompts"]) + " prompts, "
            "the 95% CI on Pearson r is wide. Treat this as direction, "
            "not a hypothesis test.[/dim]"
        )


def _color_corr(r: float) -> str:
    color = (
        "green" if abs(r) >= 0.7
        else "yellow" if abs(r) >= 0.4
        else "red"
    )
    return f"[{color}]{r:+.3f}[/{color}]"


def _mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


if __name__ == "__main__":
    app()
