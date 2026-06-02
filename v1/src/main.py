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
    no_verifier: bool = typer.Option(
        False, "--no-verifier",
        help="Verifier ablation: critic still runs but skips the re-fetch verifier",
    ),
    baseline: bool = typer.Option(
        False, "--baseline",
        help="Run the GPT-Researcher-style baseline instead of the full system",
    ),
    no_metrics_md: bool = typer.Option(
        False, "--no-metrics-md",
        help="Skip the per-run metrics markdown (also skips the judge-model calls).",
    ),
    with_plan_judge: bool = typer.Option(
        False, "--with-plan-judge",
        help="Add plan-judge to metrics.md (~1 LLM call per run on cfg.judge_model).",
    ),
    with_kae: bool = typer.Option(
        False, "--with-kae",
        help="Add KAE keypoint-aligned eval (~30 LLM calls per run on cfg.kae_judge_model).",
    ),
    with_ace: bool = typer.Option(
        False, "--with-ace",
        help="Add ACE adaptive-checklist eval (~6-11 LLM calls per run on cfg.judge_model).",
    ),
    ablation: str = typer.Option(
        None, "--ablation",
        help="Ablation target: 'critic', 'verifier', or 'trace'. Each prompt "
             "is run TWICE (OFF / ON) with paired outputs and a summary table. "
             "Mutually exclusive with --baseline / --no-critic / --no-verifier / --trace.",
    ),
    trace: bool = typer.Option(
        False, "--trace",
        help="Enable TrACE adaptive compute on the researcher tool loop. "
             "Mutually exclusive with --ablation.",
    ),
    trace_k_max: int = typer.Option(
        4, "--trace-k-max",
        help="Max rollouts per researcher decision step (TrACE-K). Default 4.",
    ),
):
    """Run the deep research agent on one or more PROMPTS."""
    events.set_verbose(verbose)
    cfg = config_module.load()

    if trace_k_max != 4 and not trace and ablation != "trace":
        console.print(
            "[yellow]--trace-k-max is only meaningful with --trace or "
            "--ablation trace; ignoring.[/yellow]"
        )

    if ablation is not None:
        if ablation not in ("critic", "verifier", "trace"):
            console.print(
                f"[red]Unknown --ablation {ablation!r}; expected "
                "'critic', 'verifier', or 'trace'.[/red]"
            )
            raise typer.Exit(2)
        conflicts: list[str] = []
        if baseline: conflicts.append("--baseline")
        if no_critic: conflicts.append("--no-critic")
        if no_verifier: conflicts.append("--no-verifier")
        if trace: conflicts.append("--trace")
        if conflicts:
            console.print(
                f"[red]--ablation is mutually exclusive with {' / '.join(conflicts)}.[/red]"
            )
            raise typer.Exit(2)

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
    elif out and ablation is not None:
        out_dir_for_json = Path(out)
        out_dir_for_json.mkdir(parents=True, exist_ok=True)

    if ablation is not None:
        succeeded, failed = _run_ablation_loop(
            cfg, all_prompts, out_dir_for_json,
            ablation=ablation,
            trace_k_max=trace_k_max,
            with_plan_judge=with_plan_judge,
            with_kae=with_kae,
            with_ace=with_ace,
        )
    elif baseline:
        succeeded, failed = _run_baseline_loop(cfg, all_prompts, out, out_dir_for_json)
    else:
        if no_critic:
            cfg = _disable_critic(cfg)
        if no_verifier:
            cfg = _disable_verifier(cfg)
        if trace:
            cfg = _enable_trace(cfg, trace_k_max)
        succeeded, failed = _run_full_loop(
            cfg, all_prompts, out, out_dir_for_json,
            no_metrics_md=no_metrics_md,
            with_plan_judge=with_plan_judge,
            with_kae=with_kae,
            with_ace=with_ace,
        )

    if len(all_prompts) > 1 or ablation is not None:
        _print_batch_summary(len(all_prompts), succeeded, failed)


# ── Loops ─────────────────────────────────────────────────────────────────


def _run_full_loop(
    cfg: Config,
    prompts_list: list[str],
    out: str | None,
    out_dir_for_json: Path | None,
    *,
    no_metrics_md: bool,
    with_plan_judge: bool,
    with_kae: bool,
    with_ace: bool,
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
            _write_run_metrics(
                cfg, rr,
                with_plan_judge=with_plan_judge,
                with_kae=with_kae,
                with_ace=with_ace,
            )

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


# ── Ablation loop (--ablation critic|verifier|trace) ─────────────────────


def _run_ablation_loop(
    cfg: Config,
    prompts_list: list[str],
    out_dir_for_json: Path | None,
    *,
    ablation: str,
    trace_k_max: int,
    with_plan_judge: bool,
    with_kae: bool,
    with_ace: bool,
) -> tuple[int, list[tuple[str, str]]]:
    """Sequential A/B loop. Each prompt runs OFF then ON arms with paired outputs."""
    cfg_off, cfg_on, label = _ablation_configs(cfg, ablation, trace_k_max)
    console.print(Panel.fit(
        f"[bold]Ablation:[/bold] {label}", border_style="bold blue",
    ))

    succeeded = 0
    failed: list[tuple[str, str]] = []
    rows: list[dict[str, Any]] = []

    for i, prompt in enumerate(prompts_list, start=1):
        _print_run_banner(i, len(prompts_list), prompt, baseline=False)
        row: dict[str, Any] = {"prompt": prompt, "off": None, "on": None}

        # OFF arm (control).
        console.print(f"[dim]── arm: OFF ({ablation}) ──[/dim]")
        off_result = _run_one_arm(
            cfg_off, prompt, arm="off",
            with_plan_judge=with_plan_judge,
            with_kae=with_kae, with_ace=with_ace,
        )
        if off_result is None:
            failed.append((f"{prompt}  [off]", "orchestrator raised"))
        else:
            row["off"] = off_result
            _maybe_save_json(off_result["rr"], i, prompt, str(out_dir_for_json) if out_dir_for_json else None,
                             out_dir_for_json, multi=True, arm_suffix="__off")

        # ON arm (treatment).
        console.print(f"[dim]── arm: ON ({ablation}) ──[/dim]")
        on_result = _run_one_arm(
            cfg_on, prompt, arm="on",
            with_plan_judge=with_plan_judge,
            with_kae=with_kae, with_ace=with_ace,
        )
        if on_result is None:
            failed.append((f"{prompt}  [on]", "orchestrator raised"))
        else:
            row["on"] = on_result
            _maybe_save_json(on_result["rr"], i, prompt, str(out_dir_for_json) if out_dir_for_json else None,
                             out_dir_for_json, multi=True, arm_suffix="__on")

        # Print this prompt's headline numbers side-by-side. Only count
        # the prompt as "succeeded" if both arms produced metrics; one-
        # arm failures are surfaced in the failed-tally but the row is
        # still appended (with the failed cell set to None) so the
        # summary table records what we DO know.
        rows.append(row)
        if row["off"] is not None and row["on"] is not None:
            succeeded += 1
            _print_ablation_row(prompt, row, ablation)

    # End-of-batch summary.
    if rows:
        try:
            path = _write_ablation_summary(
                cfg, rows, ablation=ablation, label=label,
            )
            console.print(f"\n[bold]Ablation summary written to:[/bold] {path}")
        except Exception as e:
            console.print(f"[red]ablation_summary.md write failed:[/red] {e}")

    return succeeded, failed


def _ablation_configs(
    cfg: Config, ablation: str, trace_k_max: int,
) -> tuple[Config, Config, str]:
    """Build (cfg_off, cfg_on, label) for the named ablation.

    Mirrors the contract used by `eval/ablation.py` so a bench-suite
    --ablation result and an arbitrary-prompt --ablation result can
    be cross-referenced one-to-one.
    """
    if ablation == "critic":
        cfg_off = replace(cfg, max_revisions=0)
        cfg_on = cfg
        return cfg_off, cfg_on, "critic ON vs OFF"
    if ablation == "verifier":
        cfg_off = replace(cfg, verifier_enabled=False)
        cfg_on = cfg
        return cfg_off, cfg_on, "verifier ON vs OFF"
    if ablation == "trace":
        cfg_off = replace(cfg, trace_enabled=False)
        cfg_on = replace(cfg, trace_enabled=True, trace_k_max=trace_k_max)
        return cfg_off, cfg_on, f"TrACE-{trace_k_max} ON vs OFF"
    # _validate_run already guards this; defensive default.
    raise ValueError(f"Unknown ablation: {ablation!r}")


def _run_one_arm(
    cfg: Config,
    prompt: str,
    *,
    arm: str,  # "off" | "on"
    with_plan_judge: bool,
    with_kae: bool,
    with_ace: bool,
) -> dict[str, Any] | None:
    """Run one arm of an ablation. Returns dict {"rr", "eval"} or None on failure."""
    suffix = f"__{arm}"
    try:
        rr: ResearchRun = asyncio.run(orchestrator.run_research(cfg, prompt))
    except Exception as e:
        console.print(f"  [red]Arm {arm} orchestrator failed:[/red] {e}")
        return None

    if rr.final_output and rr.final_output.output_file_path:
        try:
            new_final = _rename_with_arm_suffix(
                Path(rr.final_output.output_file_path), suffix,
            )
            rr.final_output.output_file_path = str(new_final)
        except Exception as e:
            console.print(f"  [yellow]Could not rename final.md for arm {arm}:[/yellow] {e}")

    eval_results = _write_run_metrics(
        cfg, rr,
        with_plan_judge=with_plan_judge,
        with_kae=with_kae, with_ace=with_ace,
    )
    return {"rr": rr, "eval": eval_results}


def _rename_with_arm_suffix(path: Path, suffix: str) -> Path:
    """Insert `suffix` before the extension and rename `path` on disk. Idempotent."""
    if path.stem.endswith(suffix):
        return path
    new_path = path.with_name(path.stem + suffix + path.suffix)
    path.rename(new_path)
    return new_path


def _print_ablation_row(prompt: str, row: dict, ablation: str) -> None:
    """Compact per-prompt OFF→ON delta line, printed after both arms run."""
    off, on = row.get("off"), row.get("on")
    if not off or not on:
        return
    g_off = _safe_get(off["eval"], "grounding", "rate", default=None)
    g_on = _safe_get(on["eval"], "grounding", "rate", default=None)
    f_off = _safe_get(off["eval"], "fabrication", "rate", default=None)
    f_on = _safe_get(on["eval"], "fabrication", "rate", default=None)
    cost_off = off["rr"].metrics.total_cost_usd if off["rr"].metrics else 0.0
    cost_on = on["rr"].metrics.total_cost_usd if on["rr"].metrics else 0.0
    line = (
        f"  [bold]{ablation}[/bold]:  "
        f"grounding {_pct(g_off)} → {_pct(g_on)} "
        f"({_signed_pct(_delta(g_on, g_off))})  |  "
        f"fabrication {_pct(f_off)} → {_pct(f_on)} "
        f"({_signed_pct(_delta(f_on, f_off))})  |  "
        f"cost ${cost_off:.4f} → ${cost_on:.4f}"
    )
    console.print(line)


# ── Ablation summary markdown writer ─────────────────────────────────────


def _write_ablation_summary(
    cfg: Config,
    rows: list[dict[str, Any]],
    *,
    ablation: str,
    label: str,
) -> Path:
    """Write `ablation_<name>_summary.md` to `cfg.exporter_md_dir`."""
    out_dir = Path(cfg.exporter_md_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"ablation_{ablation}_summary.md"
    body = _render_ablation_summary(rows, ablation=ablation, label=label)
    out_path.write_text(body, encoding="utf-8")
    return out_path


def _render_ablation_summary(
    rows: list[dict[str, Any]],
    *,
    ablation: str,
    label: str,
) -> str:
    """Pure-text renderer for `ablation_<name>_summary.md`."""
    lines: list[str] = []
    lines.append(f"# Ablation summary — {label}\n")
    lines.append(
        f"> One row per prompt. Each Tier-1 metric is shown OFF → ON with "
        f"the delta (Δ = ON − OFF) for the **{ablation}** component.\n"
    )
    lines.append("")

    headers = [
        "prompt",
        "g (off)", "g (on)", "Δg",
        "fab (off)", "fab (on)", "Δfab",
        "cov (off)", "cov (on)", "Δcov",
        "judge (off)", "judge (on)", "Δjudge",
        "$ off", "$ on", "Δ$",
        "t off (s)", "t on (s)",
    ]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")

    for row in rows:
        cells = _ablation_row_cells(row)
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")

    paired = [r for r in rows if r.get("off") and r.get("on")]
    if paired:
        lines.append("### Aggregate (mean Δ across both-arms-succeeded prompts)\n")
        lines.append("")
        agg_lines = _aggregate_lines(paired)
        for ln in agg_lines:
            lines.append(f"- {ln}")
        lines.append("")

    lines.append("### Per-prompt outputs\n")
    for i, row in enumerate(rows, start=1):
        prompt_short = _truncate_prompt(row.get("prompt", ""), 80)
        lines.append(f"**{i}. {prompt_short}**")
        for arm_key in ("off", "on"):
            arm = row.get(arm_key)
            if not arm:
                lines.append(f"  - {arm_key.upper()}: _(arm failed)_")
                continue
            rr = arm["rr"]
            f_path = (
                rr.final_output.output_file_path
                if rr.final_output is not None else None
            )
            m_path = getattr(rr, "metrics_file_path", None)
            f_link = f"[final]({_rel(f_path)})" if f_path else "(no final)"
            m_link = f"[metrics]({_rel(m_path)})" if m_path else "(no metrics)"
            lines.append(f"  - {arm_key.upper()}: {f_link}  |  {m_link}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _ablation_row_cells(row: dict[str, Any]) -> list[str]:
    """Per-row cells for the summary table."""
    off, on = row.get("off"), row.get("on")
    prompt_short = _truncate_prompt(row.get("prompt", ""), 60).replace("|", "\\|")

    def metric(arm: dict | None, *path: str) -> float | None:
        if not arm:
            return None
        return _safe_get(arm["eval"], *path, default=None)

    def cost(arm: dict | None) -> float | None:
        if not arm or not arm["rr"].metrics:
            return None
        return arm["rr"].metrics.total_cost_usd

    def elapsed(arm: dict | None) -> float | None:
        if not arm or not arm["rr"].metrics:
            return None
        return arm["rr"].metrics.elapsed_seconds

    g_off, g_on = metric(off, "grounding", "rate"), metric(on, "grounding", "rate")
    f_off, f_on = metric(off, "fabrication", "rate"), metric(on, "fabrication", "rate")
    c_off, c_on = metric(off, "coverage", "rate"), metric(on, "coverage", "rate")
    j_off, j_on = metric(off, "judge", "overall"), metric(on, "judge", "overall")
    co_off, co_on = cost(off), cost(on)
    t_off, t_on = elapsed(off), elapsed(on)

    return [
        prompt_short,
        _pct(g_off), _pct(g_on), _signed_pct(_delta(g_on, g_off)),
        _pct(f_off), _pct(f_on), _signed_pct(_delta(f_on, f_off)),
        _pct(c_off), _pct(c_on), _signed_pct(_delta(c_on, c_off)),
        _f2(j_off), _f2(j_on), _signed_f2(_delta(j_on, j_off)),
        _dollar(co_off), _dollar(co_on), _signed_dollar(_delta(co_on, co_off)),
        _f1(t_off), _f1(t_on),
    ]


def _aggregate_lines(paired: list[dict]) -> list[str]:
    """Mean delta per metric across rows where both arms succeeded."""
    metrics = [
        ("grounding", ("grounding", "rate"), _signed_pct),
        ("fabrication", ("fabrication", "rate"), _signed_pct),
        ("coverage", ("coverage", "rate"), _signed_pct),
        ("judge.overall", ("judge", "overall"), _signed_f2),
    ]
    out: list[str] = []
    for name, path, fmt in metrics:
        deltas = []
        for r in paired:
            v_off = _safe_get(r["off"]["eval"], *path, default=None)
            v_on = _safe_get(r["on"]["eval"], *path, default=None)
            d = _delta(v_on, v_off)
            if d is not None:
                deltas.append(d)
        if deltas:
            mean = sum(deltas) / len(deltas)
            out.append(f"mean Δ{name}: **{fmt(mean)}**  (n={len(deltas)})")

    cost_deltas = [
        _delta(
            r["on"]["rr"].metrics.total_cost_usd if r["on"]["rr"].metrics else None,
            r["off"]["rr"].metrics.total_cost_usd if r["off"]["rr"].metrics else None,
        ) for r in paired
    ]
    cost_deltas = [d for d in cost_deltas if d is not None]
    if cost_deltas:
        out.append(
            f"mean Δcost: **{_signed_dollar(sum(cost_deltas) / len(cost_deltas))}**  "
            f"(n={len(cost_deltas)})"
        )
    return out


# ── Formatting helpers used by the summary renderer ──────────────────────


def _safe_get(d: Any, *path: str, default: Any = None) -> Any:
    """Walk a chain of key lookups on a nested dict / pydantic object."""
    cur = d
    for k in path:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            cur = getattr(cur, k, None)
    return cur if cur is not None else default


def _delta(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return a - b


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.0%}"


def _signed_pct(x: float | None) -> str:
    return "—" if x is None else f"{x:+.0%}"


def _f1(x: float | None) -> str:
    return "—" if x is None else f"{x:.1f}"


def _f2(x: float | None) -> str:
    return "—" if x is None else f"{x:.2f}"


def _signed_f2(x: float | None) -> str:
    return "—" if x is None else f"{x:+.2f}"


def _dollar(x: float | None) -> str:
    return "—" if x is None else f"${x:.4f}"


def _signed_dollar(x: float | None) -> str:
    return "—" if x is None else (f"+${x:.4f}" if x >= 0 else f"-${-x:.4f}")


def _truncate_prompt(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _rel(p: str | None) -> str:
    """Render a path relative to `cfg.exporter_md_dir` for markdown cross-links."""
    if not p:
        return ""
    try:
        return "./" + Path(p).name
    except Exception:
        return p


# ── Per-run eval + metrics.md ────────────────────────────────────────────


def _write_run_metrics(
    cfg: Config,
    rr: ResearchRun,
    *,
    with_plan_judge: bool,
    with_kae: bool,
    with_ace: bool,
) -> dict[str, Any]:
    """Run the per-run eval suite and write the metrics.md sibling of final.md."""
    eval_results: dict[str, Any] = {}

    if rr.report:
        try:
            from eval.metrics.grounding import grounding_rate
            eval_results["grounding"] = grounding_rate(cfg, rr.report, rr.findings)
        except Exception as e:
            eval_results["grounding"] = {"error": str(e)}

        try:
            g = eval_results.get("grounding")
            if g and "error" not in g:
                eval_results["calibration"] = _calibration_from_grounding(rr.report, g)
            else:
                eval_results["calibration"] = {"error": "skipped: grounding failed"}
        except Exception as e:
            eval_results["calibration"] = {"error": str(e)}

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

    try:
        from eval.metrics.fabrication_rate import fabrication_rate
        eval_results["fabrication"] = asyncio.run(fabrication_rate(cfg, rr))
    except Exception as e:
        eval_results["fabrication"] = {"error": str(e)}

    try:
        from eval.metrics.category_signals import (
            contradiction_signals, honesty_signals,
        )
        eval_results["contradictions"] = contradiction_signals(rr.report)
        eval_results["honesty"] = honesty_signals(rr)
    except Exception as e:
        eval_results["contradictions"] = {"error": str(e)}
        eval_results["honesty"] = {"error": str(e)}

    try:
        from eval.metrics.source_quality_agg import source_quality_summary
        eval_results["source_quality"] = source_quality_summary(rr.report, rr.findings)
    except Exception as e:
        eval_results["source_quality"] = {"error": str(e)}

    try:
        from eval.metrics.researcher_signals import researcher_signals
        eval_results["researcher_signals"] = researcher_signals(rr)
    except Exception as e:
        eval_results["researcher_signals"] = {"error": str(e)}

    try:
        from eval.metrics.citation_density import citation_density
        eval_results["citation_density"] = citation_density(rr.report, rr.findings)
    except Exception as e:
        eval_results["citation_density"] = {"error": str(e)}

    if with_plan_judge:
        try:
            from eval.metrics.plan_judge import plan_judge
            eval_results["plan_judge"] = plan_judge(cfg, rr.user_prompt, rr.decomposition)
        except Exception as e:
            eval_results["plan_judge"] = {"error": str(e)}

    if with_kae and rr.report:
        try:
            from eval.metrics.kae import kae_score
            eval_results["kae"] = kae_score(cfg, rr.report, rr.findings)
        except Exception as e:
            eval_results["kae"] = {"error": str(e)}

    if with_ace:
        try:
            from eval.metrics.ace import ace_score
            eval_results["ace"] = ace_score(cfg, rr)
        except Exception as e:
            eval_results["ace"] = {"error": str(e)}

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

    return eval_results


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


# ── I/O helpers ──────────────────────────────────────────────────────────


def _read_prompts_file(path: Path) -> list[str]:
    """Read prompts from a text file: one per line, blank lines and `#` comments skipped."""
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
    arm_suffix: str = "",
) -> None:
    """Persist one run's full JSON if --out was provided."""
    if not out:
        return
    if multi:
        assert out_dir_for_json is not None
        slug = re.sub(r"[^a-z0-9]+", "_", prompt.lower())[:40].strip("_")
        json_path = out_dir_for_json / f"run_{idx:02d}_{slug}{arm_suffix}.json"
    else:
        json_path = Path(out)
        if arm_suffix:
            json_path = json_path.with_name(
                json_path.stem + arm_suffix + json_path.suffix,
            )
        json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(record.model_dump_json(indent=2))
    console.print(f"\n[dim]Saved run record to {json_path}[/dim]")


# ── Banners + summaries ───────────────────────────────────────────────────


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


def _enable_trace(cfg, trace_k_max: int):
    """Return a new Config with TrACE adaptive compute enabled."""
    return replace(cfg, trace_enabled=True, trace_k_max=trace_k_max)


def _disable_critic(cfg):
    """Critic ablation: returns a new Config with max_revisions=0."""
    return replace(cfg, max_revisions=0)


def _disable_verifier(cfg):
    """Verifier ablation: returns a new Config with verifier_enabled=False."""
    return replace(cfg, verifier_enabled=False)


def _print_report(rr: ResearchRun) -> None:
    """Render a ResearchRun to the terminal."""
    if not rr.report:
        console.print("[red]No report produced.[/red]")
        return

    console.print()
    console.print(Panel(rr.report.summary, title="Summary", border_style="green"))
    console.print()

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
            "\n\n".join(
                f"• [{c.severity}/{c.type}, findings={c.finding_ids}] "
                f"{c.description}"
                for c in rr.report.contradictions_surfaced
            ),
            title="Contradictions surfaced", border_style="yellow",
        ))

    if rr.report.caveats:
        # In v0 these are caveats the WRITER added. v1 adds a caveats-fallback
        # that surfaces unresolved critic issues here too.
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
        if fo.output_file_path:
            console.print(f"  [dim]Markdown saved to:[/dim] {fo.output_file_path}")

    if rr.metrics:
        m = rr.metrics
        lines = [
            f"sub-questions: {m.subquestions_with_findings}/{m.total_subquestions} covered  |  "
            f"findings: {m.total_findings}  |  unique sources: {m.unique_sources}",
            f"revision rounds: {m.revision_rounds}  |  "
            f"coverage: {m.coverage_rate:.1%}  |  grounding: {m.final_grounding_rate:.1%}",
            f"elapsed: {m.elapsed_seconds:.1f}s",
        ]
        if m.total_input_tokens or m.total_output_tokens:
            lines.append(
                f"tokens: {m.total_input_tokens:,} in / {m.total_output_tokens:,} out  |  "
                f"cost: ${m.total_cost_usd:.4f}"
            )
        if m.researcher_latency_max:
            lines.append(
                f"researcher latency: max {m.researcher_latency_max:.1f}s, "
                f"avg {m.researcher_latency_avg:.1f}s"
            )
        tm = m.tool_mix
        used_tools = [
            (n, getattr(tm, n)) for n in
            ("web_search", "search_papers", "fetch_url",
             "save_finding", "note_uncertainty")
            if getattr(tm, n)
        ]
        if used_tools:
            tool_str = "  ".join(f"{n}={c}" for n, c in used_tools)
            lines.append(f"tool mix: {tool_str}")
        if m.per_stage:
            stage_lines = []
            for stage in sorted(m.per_stage):
                s = m.per_stage[stage]
                stage_lines.append(
                    f"  {stage:<14} {s.calls:>3} calls  {s.input_tokens:>7,}↑/"
                    f"{s.output_tokens:>6,}↓  ${s.cost_usd:>6.4f}  "
                    f"{s.elapsed_seconds:>5.1f}s"
                )
            lines.append("per-stage:")
            lines.extend(stage_lines)
        console.print(Panel(
            "\n".join(lines),
            title="Run metrics", border_style="blue",
        ))

    if rr.metadata:
        md = rr.metadata
        commit_short = (md.commit_hash[:8] or "—") if md.commit_hash else "—"
        models_str = "  ".join(f"{r}={m}" for r, m in md.models.items())
        console.print(Panel(
            f"commit: {commit_short}  |  branch: {md.branch or '—'}\n{models_str}",
            title="Run metadata", border_style="dim",
        ))


if __name__ == "__main__":
    app()
