"""v0 vs v1 head-to-head fabrication-rate comparison.

Three modes per prompt: ``v0`` (shells out to the v0 CLI), ``v1-no-verifier``
(in-process v1 with verifier disabled), and ``v1`` (full v1). For each
cell, the v1 re-fetch verifier runs over the cited findings to compute
fabrication_rate.

Per-cell ResearchRun JSON is cached to ``examples/outputs/{id}_{mode}.json``
so re-runs are cheap unless ``--no-cache`` is passed.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from src import config as config_module
from src.agent.orchestrator import run_research
from src.state import ResearchRun
from src.tools.fetch import FetchContext
from .prompts import PROMPTS
from .metrics.fabrication_rate import (
    fabrication_rate, summarize_fabrication_results,
)


app = typer.Typer(add_completion=False)
console = Console()


_MODES = ("v0", "v1-no-verifier", "v1")


@app.command()
def main(
    only: str = typer.Option(None, help="Run a single prompt by id"),
    out: str = typer.Option(
        "v0_v1_compare_results.json", help="Output JSON path",
    ),
    v0_dir: str = typer.Option(
        "../v0",
        help="Path to v0 tree (relative or absolute). Default: ../v0",
    ),
    v0_python: str = typer.Option(
        None,
        help="Path to v0's python interpreter. Default: <v0_dir>/.venv/bin/python "
             "if it exists, else 'python'",
    ),
    skip_v0: bool = typer.Option(
        False, "--skip-v0",
        help="Skip the v0 mode entirely. Useful when v0 isn't set up "
             "or when you only want the within-v1 verifier ablation",
    ),
    no_cache: bool = typer.Option(
        False, "--no-cache",
        help="Force re-runs even when a cached examples/outputs/{id}_{mode}.json exists",
    ),
):
    """Run the v0-vs-v1 comparison."""
    cfg = config_module.load()
    selected = [p for p in PROMPTS if p.id == only] if only else PROMPTS
    if not selected:
        console.print(f"[red]No prompt with id {only}[/red]")
        raise typer.Exit(1)

    modes = tuple(m for m in _MODES if not (skip_v0 and m == "v0"))
    v0_python_resolved = _resolve_v0_python(v0_dir, v0_python) if "v0" in modes else None
    if "v0" in modes:
        console.print(f"[dim]v0 python: {v0_python_resolved}[/dim]")
        console.print(f"[dim]v0 dir:    {Path(v0_dir).resolve()}[/dim]")

    Path("examples/outputs").mkdir(parents=True, exist_ok=True)

    rows = []
    for bp in selected:
        console.print(f"\n[bold cyan]{bp.id}[/bold cyan] ({bp.category})")
        prompt_fetch_ctx = FetchContext()
        cell_results: dict[str, dict] = {}
        for mode in modes:
            t0 = time.time()
            console.print(f"  ▸ {mode}: ", end="")
            run = _load_or_run(
                cfg, bp, mode, v0_dir, v0_python_resolved, no_cache,
            )
            elapsed = time.time() - t0
            if run is None:
                console.print(f"[red]FAILED[/red] in {elapsed:.0f}s")
                cell_results[mode] = {
                    "error": "run failed — see stderr",
                    "elapsed_s": elapsed,
                }
                continue
            n_claims = len(run.report.claims) if run.report else 0
            console.print(
                f"{n_claims} claims, {elapsed:.0f}s — scoring fabrication_rate..."
            )
            fab = await_fab(cfg, run, prompt_fetch_ctx)
            cell_results[mode] = {
                "elapsed_s": elapsed,
                "n_claims": n_claims,
                "fabrication_rate": fab["rate"],
                "drift_rate": fab["drift_rate"],
                "approved_total": fab["approved_total"],
                "approved_with_fabricated": fab["approved_with_fabricated"],
                "approved_with_drifted": fab["approved_with_drifted"],
                "verifier_was_rerun": fab["verifier_was_rerun"],
            }
        rows.append({
            "prompt_id": bp.id,
            "category": bp.category,
            **{mode: cell_results.get(mode) for mode in modes},
        })

    summaries = {
        mode: summarize_fabrication_results([
            _to_summary_input(r[mode]) for r in rows
            if r[mode] is not None and "error" not in r[mode]
        ])
        for mode in modes
    }

    Path(out).write_text(json.dumps({
        "rows": rows,
        "summaries": summaries,
    }, indent=2, default=str))

    _print_table(rows, modes)
    _print_headline(summaries, modes)
    console.print(f"\n[dim]Wrote {out}[/dim]")


def await_fab(cfg, run, fetch_ctx):
    """Sync wrapper around the async fabrication_rate metric."""
    return asyncio.run(fabrication_rate(cfg, run, fetch_ctx=fetch_ctx))


def _to_summary_input(cell: dict) -> dict:
    """Reshape a per-cell row into the dict shape ``summarize_fabrication_results`` expects."""
    return {
        "rate": cell.get("fabrication_rate", 0.0),
        "approved_total": cell.get("approved_total", 0),
        "approved_with_fabricated": cell.get("approved_with_fabricated", 0),
        "drift_rate": cell.get("drift_rate", 0.0),
    }


# ---------------------------------------------------------------------------
# Per-cell run resolution: cache lookup → mode-specific runner.
# ---------------------------------------------------------------------------


def _load_or_run(
    cfg, bp, mode: str, v0_dir: str, v0_python: str | None, no_cache: bool,
) -> ResearchRun | None:
    """Resolve one (prompt, mode) cell to a ResearchRun, using the cache when present."""
    cache_path = Path(f"examples/outputs/{bp.id}_{mode}.json")
    if not no_cache and cache_path.exists():
        try:
            return _load_run_json(cache_path, source_mode=mode)
        except Exception as e:
            console.print(f"    [yellow]cache load failed ({e}); re-running[/yellow]")

    if mode == "v0":
        return _run_v0(bp, v0_dir, v0_python, cache_path)
    if mode == "v1-no-verifier":
        cfg_off = replace(cfg, verifier_enabled=False)
        return _run_v1_inprocess(cfg_off, bp.prompt, cache_path)
    if mode == "v1":
        return _run_v1_inprocess(cfg, bp.prompt, cache_path)
    raise ValueError(f"unknown mode {mode}")


def _run_v1_inprocess(cfg, prompt: str, cache_path: Path) -> ResearchRun | None:
    """Run v1 in-process and persist the result for the cache."""
    try:
        run = asyncio.run(run_research(cfg, prompt))
        cache_path.write_text(run.model_dump_json(indent=2))
        return run
    except Exception as e:
        console.print(f"    [red]v1 run raised: {e}[/red]")
        return None


def _run_v0(
    bp, v0_dir: str, v0_python: str | None, cache_path: Path,
) -> ResearchRun | None:
    """Shell out to the v0 CLI to run one prompt."""
    abs_cache = cache_path.resolve()
    cmd = [
        v0_python or "python", "-m", "src.main",
        bp.prompt, "--out", str(abs_cache),
    ]
    try:
        result = subprocess.run(
            cmd, cwd=v0_dir, env=os.environ.copy(),
            capture_output=True, text=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired:
        console.print("    [red]v0 subprocess timed out (10 min)[/red]")
        return None
    except FileNotFoundError as e:
        console.print(f"    [red]v0 python not found: {e}[/red]")
        return None
    if result.returncode != 0:
        tail = "\n".join(result.stderr.splitlines()[-5:])
        console.print(f"    [red]v0 exited {result.returncode}; stderr tail:\n{tail}[/red]")
        return None
    if not abs_cache.exists():
        console.print("    [red]v0 returned 0 but produced no --out file[/red]")
        return None
    try:
        return _load_run_json(abs_cache, source_mode="v0")
    except Exception as e:
        console.print(f"    [red]failed to load v0 output JSON: {e}[/red]")
        return None


def _load_run_json(path: Path, source_mode: str) -> ResearchRun:
    """Load a ResearchRun JSON, adapting v0-shape fields if needed."""
    raw = json.loads(path.read_text())
    if source_mode == "v0":
        _adapt_v0_run_dict(raw)
    return ResearchRun.model_validate(raw)


def _adapt_v0_run_dict(raw: dict) -> None:
    """In-place fixup of a v0 ResearchRun dict to the current shape."""
    report = raw.get("report")
    if isinstance(report, dict) and "contradictions_surfaced" in report:
        report["contradictions_surfaced"] = []


def _resolve_v0_python(v0_dir: str, v0_python_arg: str | None) -> str:
    """Pick a python interpreter for invoking v0."""
    if v0_python_arg:
        return v0_python_arg
    venv_py = Path(v0_dir) / ".venv" / "bin" / "python"
    if venv_py.exists():
        return str(venv_py.resolve())
    return "python"


# ---------------------------------------------------------------------------
# Rendering: per-prompt table + headline.
# ---------------------------------------------------------------------------


def _print_table(rows: list[dict], modes: tuple[str, ...]):
    """One row per prompt; columns per mode."""
    table = Table(title="v0 vs v1 — fabrication rate over critic-approved claims")
    table.add_column("id"); table.add_column("cat")
    for mode in modes:
        table.add_column(f"fab {mode}")
        table.add_column(f"app {mode}")
        table.add_column(f"t {mode}")
    for r in rows:
        cells = [r["prompt_id"], r["category"]]
        for mode in modes:
            c = r.get(mode)
            if c is None or "error" in c:
                cells += ["—", "—", "—"]
                continue
            cells += [
                f"{c['fabrication_rate']:.0%}",
                f"{c['approved_with_fabricated']}/{c['approved_total']}",
                f"{c['elapsed_s']:.0f}s",
            ]
        table.add_row(*cells)
    console.print(table)


def _print_headline(summaries: dict, modes: tuple[str, ...]):
    """Print the weighted fabrication rate across the suite."""
    console.print("\n[bold]Headline (weighted across all prompts):[/bold]")
    for mode in modes:
        s = summaries[mode]
        console.print(
            f"  {mode:<16} fab={s['weighted_rate']:.1%}  "
            f"({s['approved_with_fabricated_sum']}/{s['approved_total_sum']} approved claims)  "
            f"drift={s['drift_macro_rate']:.1%} (macro)"
        )
    if "v0" in modes and "v1" in modes:
        delta = summaries["v0"]["weighted_rate"] - summaries["v1"]["weighted_rate"]
        console.print(
            f"\n[bold green]Δ(v0 → v1): {delta:+.1%}[/bold green]  — "
            f"the fraction of v0's 'grounded' claims that v1 would catch as "
            f"fabricated"
        )
    if "v1-no-verifier" in modes and "v1" in modes:
        delta = summaries["v1-no-verifier"]["weighted_rate"] - summaries["v1"]["weighted_rate"]
        console.print(
            f"[bold]Δ(v1-no-verifier → v1): {delta:+.1%}[/bold]  — "
            f"the verifier's marginal contribution holding everything else "
            f"v1 constant (the within-pipeline ablation)"
        )


if __name__ == "__main__":
    app()
