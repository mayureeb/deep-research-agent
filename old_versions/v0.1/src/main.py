"""CLI entrypoint for the deep research agent."""
from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel

from . import config as config_module
from . import events
from .agent import orchestrator
from .agent.baseline import run_baseline
from .config import Config
from .metrics_md import write_metrics_markdown
from .state import BaselineRun, ResearchRun


app = typer.Typer(add_completion=False, no_args_is_help=True)
console = Console()


@app.command()
def run(
    prompts: list[str] = typer.Argument(
        None,
        help="One or more research prompts (positional, space-separated). "
             "Merged with --prompts-file if both are given.",
    ),
    prompts_file: Path = typer.Option(
        None, "--prompts-file", "-f",
        exists=True, dir_okay=False,
        help="Read additional prompts from this file (one per line; blank "
             "lines and lines starting with # are skipped).",
    ),
    out: str = typer.Option(
        None, "--out", "-o",
        help="Save full run JSON. With multiple prompts this becomes a directory.",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="Stream a live event log of every step (decompose, search, fetch, save, write, critique)",
    ),
    no_critic: bool = typer.Option(
        False, "--no-critic", help="Critic ablation: skip the verification loop"
    ),
    baseline: bool = typer.Option(
        False, "--baseline",
        help="Run the GPT-Researcher-style baseline instead of the full system",
    ),
    no_metrics_md: bool = typer.Option(
        False, "--no-metrics-md",
        help="Skip the per-run metrics markdown (also skips the judge-model calls).",
    ),
):
    """Run the deep research agent on one or more PROMPTS."""
    events.set_verbose(verbose)
    cfg = config_module.load()

    all_prompts: list[str] = list(prompts or [])
    if prompts_file is not None:
        all_prompts.extend(_read_prompts_file(prompts_file))
    if not all_prompts:
        console.print(
            "[red]No prompts provided.[/red] "
            "Pass them as positional args or via --prompts-file."
        )
        raise typer.Exit(2)

    out_dir_for_json: Path | None = None
    if out and len(all_prompts) > 1:
        out_dir_for_json = Path(out)
        out_dir_for_json.mkdir(parents=True, exist_ok=True)

    if baseline:
        succeeded, failed = _run_baseline_loop(cfg, all_prompts, out, out_dir_for_json)
    else:
        if no_critic:
            cfg = _disable_critic(cfg)
        succeeded, failed = _run_full_loop(
            cfg, all_prompts, out, out_dir_for_json, no_metrics_md=no_metrics_md,
        )

    if len(all_prompts) > 1:
        _print_batch_summary(len(all_prompts), succeeded, failed)


def _run_full_loop(
    cfg: Config,
    prompts_list: list[str],
    out: str | None,
    out_dir_for_json: Path | None,
    *,
    no_metrics_md: bool,
) -> tuple[int, list[tuple[str, str]]]:
    """Sequential full-pipeline loop with continue-on-failure."""
    succeeded = 0
    failed: list[tuple[str, str]] = []
    for i, prompt in enumerate(prompts_list, start=1):
        _print_run_banner(i, len(prompts_list), prompt, baseline=False)
        try:
            rr: ResearchRun = asyncio.run(orchestrator.run_research(cfg, prompt))
        except Exception as e:
            console.print(f"[red]Run {i} failed:[/red] {e}")
            failed.append((prompt, str(e)))
            continue

        if not no_metrics_md:
            _write_run_metrics(cfg, rr)

        _print_report(rr)
        _maybe_save_json(rr, i, prompt, out, out_dir_for_json,
                         multi=len(prompts_list) > 1)
        succeeded += 1
    return succeeded, failed


def _run_baseline_loop(
    cfg: Config,
    prompts_list: list[str],
    out: str | None,
    out_dir_for_json: Path | None,
) -> tuple[int, list[tuple[str, str]]]:
    """Sequential baseline loop."""
    succeeded = 0
    failed: list[tuple[str, str]] = []
    for i, prompt in enumerate(prompts_list, start=1):
        _print_run_banner(i, len(prompts_list), prompt, baseline=True)
        try:
            br: BaselineRun = asyncio.run(run_baseline(cfg, prompt))
        except Exception as e:
            console.print(f"[red]Baseline run {i} failed:[/red] {e}")
            failed.append((prompt, str(e)))
            continue
        _print_baseline(br)
        _maybe_save_json(br, i, prompt, out, out_dir_for_json,
                         multi=len(prompts_list) > 1)
        succeeded += 1
    return succeeded, failed


def _write_run_metrics(cfg: Config, rr: ResearchRun) -> None:
    """Run the per-run eval suite and write metrics.md next to final.md."""
    eval_results: dict[str, Any] = {}

    if rr.report:
        try:
            from eval.metrics.grounding import grounding_rate
            eval_results["grounding"] = grounding_rate(cfg, rr.report, rr.findings)
        except Exception as e:
            eval_results["grounding"] = {"error": str(e)}

        # Reuse the grounding judgments to avoid a second per-claim judge call.
        try:
            g = eval_results.get("grounding")
            if g and "error" not in g:
                eval_results["calibration"] = _calibration_from_grounding(rr.report, g)
            else:
                eval_results["calibration"] = {"error": "skipped: grounding failed"}
        except Exception as e:
            eval_results["calibration"] = {"error": str(e)}

    # Coverage doesn't need a Report; runs even when the writer failed.
    try:
        from eval.metrics.coverage import coverage_rate
        eval_results["coverage"] = coverage_rate(rr)
    except Exception as e:
        eval_results["coverage"] = {"error": str(e)}

    try:
        from eval.metrics.judge import llm_judge
        eval_results["judge"] = llm_judge(cfg, rr)
    except Exception as e:
        eval_results["judge"] = {"error": str(e)}

    paired = (
        rr.final_output.output_file_path
        if rr.final_output is not None else None
    )
    try:
        path = write_metrics_markdown(
            rr,
            eval_results=eval_results,
            out_dir=cfg.exporter_md_dir,
            paired_final_path=paired,
        )
        rr.metrics_file_path = str(path)
        console.print(f"  [dim]Metrics saved to:[/dim] {path}")
    except Exception as e:
        console.print(f"  [red]metrics.md write failed:[/red] {e}")


def _calibration_from_grounding(report, grounding_result: dict) -> dict:
    """Bucket claims by stated confidence using existing grounding judgments."""
    from eval.metrics.calibration import BUCKETS

    grounded_by_idx = {
        d.get("claim_index"): bool(d.get("grounded"))
        for d in grounding_result.get("details", []) or []
    }
    buckets = []
    for lo, hi in BUCKETS:
        in_bucket = [
            (i, c) for i, c in enumerate(report.claims)
            if lo <= c.confidence < hi
        ]
        if not in_bucket:
            buckets.append({
                "range": [lo, hi], "n": 0,
                "grounded_rate": None, "mean_stated_confidence": None,
            })
            continue
        n = len(in_bucket)
        grounded = sum(1 for i, _ in in_bucket if grounded_by_idx.get(i, False))
        mean_conf = sum(c.confidence for _, c in in_bucket) / n
        buckets.append({
            "range": [lo, hi],
            "n": n,
            "grounded_rate": grounded / n,
            "mean_stated_confidence": mean_conf,
        })
    return {"buckets": buckets, "n_claims": len(report.claims)}


def _read_prompts_file(path: Path) -> list[str]:
    """Read prompts from a text file, one per line; skip blanks and `#` comments."""
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _maybe_save_json(
    record: ResearchRun | BaselineRun,
    idx: int,
    prompt: str,
    out: str | None,
    out_dir_for_json: Path | None,
    *,
    multi: bool,
) -> None:
    """Persist one run's full JSON if --out was provided."""
    if not out:
        return
    if multi:
        assert out_dir_for_json is not None
        slug = re.sub(r"[^a-z0-9]+", "_", prompt.lower())[:40].strip("_")
        json_path = out_dir_for_json / f"run_{idx:02d}_{slug}.json"
    else:
        json_path = Path(out)
    json_path.write_text(record.model_dump_json(indent=2))
    console.print(f"\n[dim]Saved run record to {json_path}[/dim]")


def _print_run_banner(idx: int, total: int, prompt: str, *, baseline: bool) -> None:
    """One-line panel before each run starts."""
    prefix = "Researching"
    if baseline:
        prefix = "Researching (BASELINE — GPT-Researcher-style)"
    if total > 1:
        prefix = f"({idx}/{total}) {prefix}"
    border = "magenta" if baseline else "cyan"
    console.print(Panel.fit(
        f"[bold]{prefix}:[/bold] {prompt}", border_style=border,
    ))


def _print_batch_summary(total: int, succeeded: int, failed: list[tuple[str, str]]) -> None:
    """End-of-batch tally for multi-prompt invocations."""
    console.print()
    line = f"[bold]{succeeded}/{total} prompts succeeded[/bold]"
    if failed:
        line += f"  ([red]{len(failed)} failed[/red])"
    console.print(Panel.fit(
        line, border_style="green" if not failed else "yellow",
    ))
    for prompt, err in failed:
        # Truncate so a multi-line exception doesn't flood the terminal.
        p = (prompt[:80] + "…") if len(prompt) > 80 else prompt
        e = (err[:80] + "…") if len(err) > 80 else err
        console.print(f"[red]✗[/red] {p}  [dim]→ {e}[/dim]")


def _print_baseline(br: BaselineRun) -> None:
    """Render a baseline run to the terminal."""
    console.print()
    console.print(Panel(
        br.report.final_report_text, title="Baseline report (free-form)",
        border_style="magenta",
    ))
    console.print(Panel(
        f"sub-questions: {len(br.decomposition.subquestions)}  |  "
        f"summaries: {len(br.report.summaries)}  |  "
        f"unique sources: {len(br.report.all_sources)}\n"
        f"elapsed: {br.elapsed_seconds:.1f}s",
        title="Baseline metrics", border_style="blue",
    ))


def _disable_critic(cfg):
    """Critic ablation: returns a NEW Config with max_revisions=0.

    The orchestrator's revision loop is `for round_num in range(max_revisions
    + 1)`, so max_revisions=0 still runs ONE critic pass but never revises.
    """
    return replace(cfg, max_revisions=0)


def _print_report(rr: ResearchRun) -> None:
    """Render a ResearchRun to the terminal."""
    if not rr.report:
        console.print("[red]No report produced.[/red]")
        return

    console.print()
    console.print(Panel(rr.report.summary, title="Summary", border_style="green"))
    console.print()

    # Resolve supporting_finding_indices to source URLs for display.
    for i, claim in enumerate(rr.report.claims):
        sources = []
        for fi in claim.supporting_finding_indices:
            if 0 <= fi < len(rr.findings.findings):
                sources.append(rr.findings.findings[fi].source_url)
        src_str = "\n".join(f"    • {s}" for s in sources) if sources else "    (no sources)"
        console.print(
            f"[bold]Claim {i+1}[/bold]  [dim](confidence: {claim.confidence:.2f})[/dim]"
        )
        console.print(f"  {claim.claim}")
        console.print(f"  [dim]Sources:[/dim]\n{src_str}")
        console.print()

    if rr.report.contradictions_surfaced:
        console.print(Panel(
            "\n\n".join(f"• {c}" for c in rr.report.contradictions_surfaced),
            title="Contradictions surfaced", border_style="yellow",
        ))

    if rr.report.caveats:
        console.print(Panel(
            "\n".join(f"• {c}" for c in rr.report.caveats),
            title="Caveats", border_style="yellow",
        ))

    if rr.final_output:
        fo = rr.final_output
        unmet = len(fo.unmet_conditions)
        title = f"Final output  ({fo.format_detected})"
        if fo.conditions:
            n_total = len(fo.conditions)
            satisfied = sum(1 for c in fo.conditions if c.status == "satisfied")
            title += f"  ({satisfied}/{n_total} conditions satisfied)"
        console.print(Panel(
            fo.content, title=title,
            border_style="green" if unmet == 0 else "yellow",
        ))
        if unmet:
            console.print(Panel(
                "\n".join(f"• {c}" for c in fo.unmet_conditions),
                title=f"Unmet / partial conditions ({unmet})",
                border_style="red",
            ))
        # None means the markdown write failed — reason is in fo.notes.
        if fo.output_file_path:
            console.print(f"  [dim]Markdown saved to:[/dim] {fo.output_file_path}")

    if rr.metrics:
        m = rr.metrics
        console.print(Panel(
            f"sub-questions: {m.subquestions_with_findings}/{m.total_subquestions} covered  |  "
            f"findings: {m.total_findings}  |  unique sources: {m.unique_sources}\n"
            f"revision rounds: {m.revision_rounds}  |  "
            f"coverage: {m.coverage_rate:.1%}  |  grounding: {m.final_grounding_rate:.1%}\n"
            f"elapsed: {m.elapsed_seconds:.1f}s",
            title="Run metrics", border_style="blue",
        ))


if __name__ == "__main__":
    app()
