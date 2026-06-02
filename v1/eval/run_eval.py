"""Run the eval suite over the curated prompts and write a JSON report.

For each benchmark prompt: runs the full pipeline, computes baseline
metrics (grounding, coverage, F1, calibration, LLM-as-judge with rubric)
plus opt-in paper-inspired metrics (--with-taxonomy / --with-kae /
--with-ace / --with-plan-judge). The reproducibility mode runs one
prompt N times and computes claim-Jaccard + per-metric stability stats.
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
from src import events
from src.agent.orchestrator import run_research
from src.agent.taxonomy_writer import write_taxonomy
from .prompts import PROMPTS
from .metrics.ace import ace_score
from .metrics.aggregates import calibration_slope, f_score, fmt_slope
from .metrics.category_signals import (
    contradiction_aggregate, contradiction_signals,
    honesty_aggregate, honesty_signals,
)
from .metrics.citation_density import citation_density
from .metrics.grounding import grounding_rate
from .metrics.coverage import coverage_rate
from .metrics.calibration import calibration_curve
from .metrics.hierarchy import hierarchy_summary
from .metrics.judge import llm_judge
from .metrics.kae import kae_score
from .metrics.plan_judge import plan_judge
from .metrics.reproducibility import (
    claim_jaccard, metric_stats,
    shape_selection_stats,
    subquestion_count_stats, subquestion_jaccard, subquestion_overlap,
    unique_source_stats,
)
from .metrics.researcher_signals import researcher_signals
from .metrics.source_quality_agg import source_quality_summary
from .metrics.unique_sources import unique_sources


app = typer.Typer(add_completion=False)
console = Console()


@app.command()
def main(
    only: str = typer.Option(None, help="Run a single prompt by id"),
    out: str = typer.Option("eval_results.json", help="Output JSON path"),
    reproducibility: str = typer.Option(
        None,
        help="Run this prompt id N times (default 3, set with --runs) and "
             "compute Jaccard + per-metric mean/stdev/min/max across the runs.",
    ),
    runs: int = typer.Option(
        3, "--runs",
        help="Number of repetitions for --reproducibility mode (>=2). "
             "Ignored when --reproducibility is not set.",
    ),
    with_taxonomy: bool = typer.Option(
        False, "--with-taxonomy",
        help="Build hierarchical taxonomy + hierarchy diagnostics "
             "(~1 extra LLM call/prompt).",
    ),
    with_kae: bool = typer.Option(
        False, "--with-kae",
        help="Compute KAE (KSR/KCR/KOR triplet) — DeepResearch Arena §3.3. "
             "~30 LLM calls/prompt on cfg.kae_judge_model (Haiku by default). "
             "Override the model via the KAE_JUDGE_MODEL env var.",
    ),
    with_ace: bool = typer.Option(
        False, "--with-ace",
        help="Compute ACE (two-stage adaptive checklist) — DeepResearch "
             "Arena §3.4. ~6-11 LLM calls/prompt on cfg.judge_model.",
    ),
    with_plan_judge: bool = typer.Option(
        False, "--with-plan-judge",
        help="Run the LLM plan judge (combined surface coverage + intent "
             "alignment scores). 1 LLM call/prompt on cfg.judge_model.",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="Stream live orchestrator events to stderr while each prompt runs.",
    ),
):
    """Run the eval suite (or a single-prompt subset, or a reproducibility run)."""
    cfg = config_module.load()
    if verbose:
        events.set_verbose(True)
    selected = (
        [p for p in PROMPTS if p.id == only] if only else PROMPTS
    )
    if not selected:
        console.print(f"[red]No prompt with id {only}[/red]")
        raise typer.Exit(1)

    if reproducibility:
        _run_reproducibility(
            cfg, reproducibility, out, runs=runs,
            with_taxonomy=with_taxonomy, with_kae=with_kae, with_ace=with_ace,
            with_plan_judge=with_plan_judge,
        )
        return

    results = []
    for bp in selected:
        console.print(f"\n[bold cyan]Running {bp.id} ({bp.category})...[/bold cyan]")
        t0 = time.time()
        rr = asyncio.run(run_research(cfg, bp.prompt))
        elapsed = time.time() - t0

        console.print(f"  generated {len(rr.report.claims if rr.report else [])} claims "
                      f"in {elapsed:.1f}s — scoring metrics...")

        if with_taxonomy and rr.report and rr.report.claims:
            console.print("  ▸ building taxonomy...")
            rr.taxonomy = write_taxonomy(
                cfg, bp.prompt, rr.decomposition, rr.report, rr.findings,
            )

        ground = grounding_rate(cfg, rr.report, rr.findings) if rr.report else {"rate": 0.0, "details": []}
        cov = coverage_rate(rr)
        calib_curve = calibration_curve(cfg, rr.report, rr.findings) if rr.report else {}
        calib_slope = calibration_slope(rr.report, ground.get("details", []))
        judge = llm_judge(cfg, rr, rubric=bp.rubric)
        f1 = f_score(ground["rate"], cov["rate"])

        kae_data = None
        if with_kae and rr.report:
            console.print("  ▸ scoring KAE...")
            kae_obj = kae_score(cfg, rr.report, rr.findings)
            if rr.metrics is not None:
                rr.metrics.kae = kae_obj
            kae_data = kae_obj.model_dump()

        ace_data = None
        if with_ace:
            console.print("  ▸ scoring ACE...")
            ace_obj = ace_score(cfg, rr)
            if rr.metrics is not None:
                rr.metrics.ace = ace_obj
            ace_data = ace_obj.model_dump()

        hier_data = hierarchy_summary(rr.taxonomy) if with_taxonomy else None

        cite_density = citation_density(rr.report, rr.findings)
        src_q_agg = source_quality_summary(rr.report, rr.findings)
        contra_signals = contradiction_signals(rr.report)
        honesty = honesty_signals(rr)
        sq_overlap = subquestion_overlap(rr.decomposition)
        researcher_sig = researcher_signals(rr)
        unique_src = unique_sources(rr.report, rr.findings)

        plan_judge_data = None
        if with_plan_judge:
            console.print("  ▸ scoring plan judge...")
            plan_judge_obj = plan_judge(cfg, bp.prompt, rr.decomposition)
            if rr.metrics is not None:
                rr.metrics.plan_judge = plan_judge_obj
            plan_judge_data = plan_judge_obj.model_dump()

        m = rr.metrics
        step6 = _extract_step6_headline(rr) if m else {}
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
            "kae": kae_data,
            "ace": ace_data,
            "hierarchy": hier_data,
            "citation_density": cite_density,
            "source_quality_agg": src_q_agg,
            "contradictions": contra_signals,
            "honesty": honesty,
            "subq_overlap": sq_overlap,
            "researcher_signals": researcher_sig,
            "unique_sources": unique_src,
            "plan_judge": plan_judge_data,
            "run_metrics": m.model_dump() if m else None,
            **step6,
        })

        run_path = Path("examples/outputs") / f"{bp.id}.json"
        run_path.parent.mkdir(parents=True, exist_ok=True)
        run_path.write_text(rr.model_dump_json(indent=2))

    Path(out).write_text(json.dumps(results, indent=2, default=str))
    _print_summary(
        results,
        with_taxonomy=with_taxonomy, with_kae=with_kae, with_ace=with_ace,
        with_plan_judge=with_plan_judge,
    )
    _print_quality_signals_table(results)
    _print_category_aggregates(results)
    console.print(f"\n[dim]Wrote results to {out}[/dim]")


def _run_reproducibility(
    cfg,
    prompt_id: str,
    out: str,
    *,
    runs: int = 3,
    with_taxonomy: bool = False,
    with_kae: bool = False,
    with_ace: bool = False,
    with_plan_judge: bool = False,
):
    """Run one prompt N times → per-run metrics + Jaccards + per-metric variance."""
    bp = next((p for p in PROMPTS if p.id == prompt_id), None)
    if not bp:
        console.print(f"[red]Unknown prompt id {prompt_id}[/red]")
        raise typer.Exit(1)
    if runs < 2:
        console.print(
            f"[red]--runs must be >= 2 to compute pairwise Jaccard / "
            f"variance (got {runs})[/red]"
        )
        raise typer.Exit(1)

    console.print(f"[bold]Reproducibility run: {prompt_id} x {runs}[/bold]")
    rrs = []
    per_run_metrics: list[dict] = []
    for i in range(runs):
        console.print(f"  run {i+1}/{runs}...")
        t0 = time.time()
        rr = asyncio.run(run_research(cfg, bp.prompt))
        elapsed = time.time() - t0

        if with_taxonomy and rr.report and rr.report.claims:
            rr.taxonomy = write_taxonomy(
                cfg, bp.prompt, rr.decomposition, rr.report, rr.findings,
            )

        ground = (
            grounding_rate(cfg, rr.report, rr.findings)
            if rr.report else {"rate": 0.0, "details": []}
        )
        cov = coverage_rate(rr)
        calib_slope = calibration_slope(rr.report, ground.get("details", []))
        judge = llm_judge(cfg, rr, rubric=bp.rubric)
        f1 = f_score(ground["rate"], cov["rate"])

        kae_obj = None
        if with_kae and rr.report:
            kae_obj = kae_score(cfg, rr.report, rr.findings)
            if rr.metrics is not None:
                rr.metrics.kae = kae_obj
        ace_obj = None
        if with_ace:
            ace_obj = ace_score(cfg, rr)
            if rr.metrics is not None:
                rr.metrics.ace = ace_obj
        hier = hierarchy_summary(rr.taxonomy) if with_taxonomy else None

        cite_density_d = citation_density(rr.report, rr.findings)
        src_q_d = source_quality_summary(rr.report, rr.findings)
        contra_d = contradiction_signals(rr.report)
        honesty_d = honesty_signals(rr)
        sq_overlap_d = subquestion_overlap(rr.decomposition)
        researcher_d = researcher_signals(rr)
        unique_d = unique_sources(rr.report, rr.findings)

        plan_judge_obj = None
        if with_plan_judge:
            plan_judge_obj = plan_judge(cfg, bp.prompt, rr.decomposition)
            if rr.metrics is not None:
                rr.metrics.plan_judge = plan_judge_obj

        rrs.append(rr)
        per_run_metrics.append({
            "run_index": i,
            "elapsed_seconds": elapsed,
            "n_claims": len(rr.report.claims) if rr.report else 0,
            "n_subquestions": len(rr.decomposition.subquestions),
            "grounding_rate": ground["rate"],
            "coverage_rate": cov["rate"],
            "f1": f1,
            "calibration_slope": calib_slope.get("slope"),
            "judge_overall": (
                judge.get("overall") if isinstance(judge, dict) else None
            ),
            "kae_ksr": kae_obj.ksr if kae_obj else None,
            "kae_kcr": kae_obj.kcr if kae_obj else None,
            "kae_kor": kae_obj.kor if kae_obj else None,
            "ace_overall": ace_obj.overall if ace_obj else None,
            "hierarchy_n_leaves": hier.get("n_leaves") if hier else None,
            "hierarchy_avg_per_leaf": hier.get("avg_per_leaf") if hier else None,
            "hierarchy_over_segmented": hier.get("over_segmented") if hier else None,
            "citation_avg_per_claim":   cite_density_d["avg_per_claim"],
            "citation_load_bearing":    cite_density_d["load_bearing_fraction"],
            "citation_max_src_share":   cite_density_d["max_source_share"],
            "source_quality_mean_cited": src_q_d["mean_cited_quality"],
            "contradictions_n_surfaced": contra_d["n_surfaced"],
            "honesty_n_notes":          honesty_d["n_uncertainty_notes"],
            "honesty_n_caveats":        honesty_d["n_caveats"],
            "subq_overlap_mean":        sq_overlap_d["mean_pairwise_jaccard"],
            "n_researchers_with_findings":     researcher_d["n_researchers_with_findings"],
            "n_researchers_with_uncertainty":  researcher_d["n_researchers_with_uncertainty"],
            "n_subquestions_without_findings": researcher_d["n_subquestions_without_findings"],
            "total_findings":                  researcher_d["total_findings"],
            "total_uncertainty_notes":         researcher_d["total_uncertainty_notes"],
            "n_unique_urls_cited":             unique_d["n_unique_urls_cited"],
            "n_unique_domains_cited":          unique_d["n_unique_domains_cited"],
            "n_unique_urls_harvested":         unique_d["n_unique_urls_harvested"],
            "n_unique_domains_harvested":      unique_d["n_unique_domains_harvested"],
            "shape_confidence":                rr.shape.confidence if rr.shape else None,
            "plan_surface_coverage":    plan_judge_obj.surface_coverage if plan_judge_obj else None,
            "plan_intent_alignment":    plan_judge_obj.intent_alignment if plan_judge_obj else None,
            "cost_usd_total": (
                rr.metrics.total_cost_usd if rr.metrics else None
            ),
            "tokens_input_total": (
                rr.metrics.total_input_tokens if rr.metrics else None
            ),
            "tokens_output_total": (
                rr.metrics.total_output_tokens if rr.metrics else None
            ),
        })

    jac = claim_jaccard(rrs)
    sq_jac = subquestion_jaccard(rrs)
    stats = metric_stats(per_run_metrics)
    shape_sel = shape_selection_stats(rrs)
    sq_count_sel = subquestion_count_stats(rrs)
    unique_sel = unique_source_stats(rrs)

    _print_reproducibility_summary(
        per_run_metrics, stats, jac, sq_jac, shape_sel,
        sq_count_sel, unique_sel,
        with_taxonomy=with_taxonomy, with_kae=with_kae, with_ace=with_ace,
    )

    Path(out).write_text(json.dumps({
        "prompt_id": prompt_id,
        "n_runs": runs,
        "claim_jaccard": jac,
        "subquestion_jaccard": sq_jac,
        "shape_selection": shape_sel,
        "subquestion_count_selection": sq_count_sel,
        "unique_sources": unique_sel,
        "per_run_metrics": per_run_metrics,
        "per_metric_stats": stats,
        "runs": [rr.model_dump(mode="json") for rr in rrs],
    }, indent=2, default=str))
    console.print(f"\n[dim]Wrote results to {out}[/dim]")


def _extract_step6_headline(rr) -> dict:
    """Promote instrumentation headline numbers (shape, cost, tokens,
    latency, tool-mix totals, commit metadata) into a flat dict for the
    eval summary."""
    m = rr.metrics
    out = {
        "shape": rr.shape.shape if rr.shape else None,
        "cost_usd_total": m.total_cost_usd,
        "tokens_input_total": m.total_input_tokens,
        "tokens_output_total": m.total_output_tokens,
        "researcher_latency_max_s": m.researcher_latency_max,
        "researcher_latency_avg_s": m.researcher_latency_avg,
        "tool_mix_total": (
            m.tool_mix.web_search + m.tool_mix.search_papers +
            m.tool_mix.fetch_url + m.tool_mix.save_finding +
            m.tool_mix.note_uncertainty
        ),
        "commit_hash": rr.metadata.commit_hash if rr.metadata else "",
        "branch": rr.metadata.branch if rr.metadata else "",
    }
    return out


def _print_summary(
    results: list[dict],
    *,
    with_taxonomy: bool = False,
    with_kae: bool = False,
    with_ace: bool = False,
    with_plan_judge: bool = False,
):
    """Render the eval summary as a Rich table."""
    table = Table(title="Eval summary")
    table.add_column("id"); table.add_column("cat"); table.add_column("shape")
    table.add_column("claims")
    table.add_column("ground"); table.add_column("cover"); table.add_column("F1")
    table.add_column("calib"); table.add_column("judge")
    if with_kae:
        table.add_column("KSR"); table.add_column("KCR"); table.add_column("KOR")
    if with_ace:
        table.add_column("ACE")
    if with_taxonomy:
        table.add_column("leaves"); table.add_column("avg/leaf"); table.add_column("over-seg?")
    if with_plan_judge:
        table.add_column("plan-cov"); table.add_column("plan-int")
    table.add_column("cost"); table.add_column("tools")

    for r in results:
        judge_overall = r["judge"].get("overall") if isinstance(r["judge"], dict) else None
        shape = r.get("shape") or "—"
        cost = r.get("cost_usd_total")
        tool_total = r.get("tool_mix_total")
        cost_cell = f"${cost:.3f}" if cost is not None else "—"
        tools_cell = (
            f"{tool_total}" if tool_total is not None else "—"
        )
        row = [
            r["prompt_id"], r["category"], shape, str(r["n_claims"]),
            f"{r['grounding_rate']:.0%}",
            f"{r['coverage_rate']:.0%}",
            f"{r['f1']:.0%}",
            fmt_slope(r['calibration_slope']),
            f"{judge_overall:.1f}" if judge_overall is not None else "—",
        ]
        if with_kae:
            kae = r.get("kae")
            row += [
                f"{kae['ksr']:.0%}" if kae else "—",
                f"{kae['kcr']:.0%}" if kae else "—",
                f"{kae['kor']:.0%}" if kae else "—",
            ]
        if with_ace:
            ace = r.get("ace")
            row += [f"{ace['overall']:.2f}" if ace else "—"]
        if with_taxonomy:
            hier = r.get("hierarchy") or {}
            n_leaves = hier.get("n_leaves", 0)
            row += [
                str(n_leaves) if n_leaves else "—",
                f"{hier['avg_per_leaf']:.1f}" if n_leaves else "—",
                ("[red]yes[/red]" if hier.get("over_segmented")
                 else "[green]no[/green]") if n_leaves else "—",
            ]
        if with_plan_judge:
            pj = r.get("plan_judge") or {}
            row += [
                f"{pj['surface_coverage']:.2f}" if pj else "—",
                f"{pj['intent_alignment']:.2f}" if pj else "—",
            ]
        row += [cost_cell, tools_cell]
        table.add_row(*row)
    console.print(table)

    legend = (
        "\n[dim]F1 = harmonic mean of grounding & coverage. "
        "calib slope ≈ 1: well-calibrated; ≈ 0: meaningless labels; "
        "'flat' = the model collapsed to one confidence value. "
        "cost = total LLM USD across all stages. tools = #tool calls."
    )
    if with_kae:
        legend += (
            " KSR/KCR/KOR = Keypoint Supported / Conflict / Omission Rate "
            "(DeepResearch Arena)."
        )
    if with_ace:
        legend += " ACE = Adaptive Checklist Evaluation overall (0–1, two-stage judge)."
    if with_taxonomy:
        legend += (
            " avg/leaf = consolidation signal (>2 healthy, <1.5 over-segmented). "
            "over-seg = TaxoBench-inspired flag."
        )
    if with_plan_judge:
        legend += (
            " plan-cov = surface coverage (1.0 = decomposition covers every "
            "axis the prompt asks about). plan-int = intent alignment (1.0 = "
            "every sub-question is on-topic)."
        )
    legend += "[/dim]"
    console.print(legend)


def _print_quality_signals_table(results: list[dict]) -> None:
    """Render the quality-signal sub-table — one row per prompt."""
    table = Table(title="Quality signals")
    table.add_column("id"); table.add_column("cat")
    table.add_column("cite/claim")
    table.add_column("cite-load")
    table.add_column("max-src-share")
    table.add_column("src-q (cited)")
    table.add_column("src-q (all)")
    table.add_column("contras")
    table.add_column("uncert")
    table.add_column("caveats")
    table.add_column("u→c?")
    table.add_column("sq overlap")
    table.add_column("uniq URLs")
    table.add_column("uniq dom")

    for r in results:
        cd = r.get("citation_density") or {}
        sq = r.get("source_quality_agg") or {}
        ct = r.get("contradictions") or {}
        hn = r.get("honesty") or {}
        ov = r.get("subq_overlap") or {}
        un = r.get("unique_sources") or {}
        table.add_row(
            r["prompt_id"], r["category"],
            f"{cd.get('avg_per_claim', 0):.1f}",
            f"{cd.get('load_bearing_fraction', 0):.0%}",
            f"{cd.get('max_source_share', 0):.0%}",
            f"{sq.get('mean_cited_quality', 0):.2f}",
            f"{sq.get('mean_all_quality', 0):.2f}",
            str(ct.get("n_surfaced", 0)),
            str(hn.get("n_uncertainty_notes", 0)),
            str(hn.get("n_caveats", 0)),
            "✓" if hn.get("caveats_reference_uncertainty") else "—",
            f"{ov.get('mean_pairwise_jaccard', 0):.2f}",
            f"{un.get('n_unique_urls_cited', 0)}/{un.get('n_unique_urls_harvested', 0)}",
            f"{un.get('n_unique_domains_cited', 0)}/{un.get('n_unique_domains_harvested', 0)}",
        )
    console.print(table)
    console.print(
        "[dim]cite/claim = avg citations per claim; cite-load = % of harvested "
        "findings that got cited; max-src-share = single source's max share of "
        "all citations; src-q = mean Finding.source_quality (cited only vs. "
        "all harvested); contras / uncert / caveats = counts on the report; "
        "u→c? = does any report.caveat substring-match an UncertaintyNote.topic; "
        "sq overlap = within-run pairwise sub-question text Jaccard "
        "(>0.4 ⇒ planner duplicating itself); "
        "uniq URLs/dom = distinct cited / harvested (cited slash harvested).[/dim]"
    )

    rs_table = Table(title="Researcher signals")
    rs_table.add_column("id"); rs_table.add_column("cat")
    rs_table.add_column("#sub-qs")
    rs_table.add_column("sub-qs w/o find")
    rs_table.add_column("res w/find")
    rs_table.add_column("res w/uncert")
    rs_table.add_column("res w/both")
    rs_table.add_column("total findings")
    rs_table.add_column("total uncert")
    rs_table.add_column("max/res")

    for r in results:
        rs = r.get("researcher_signals") or {}
        rs_table.add_row(
            r["prompt_id"], r["category"],
            str(rs.get("n_subquestions", 0)),
            str(rs.get("n_subquestions_without_findings", 0)),
            str(rs.get("n_researchers_with_findings", 0)),
            str(rs.get("n_researchers_with_uncertainty", 0)),
            str(rs.get("n_researchers_with_both", 0)),
            str(rs.get("total_findings", 0)),
            str(rs.get("total_uncertainty_notes", 0)),
            str(rs.get("max_findings_one_researcher", 0)),
        )
    console.print(rs_table)
    console.print(
        "[dim]#sub-qs = planner output; sub-qs w/o find = sub-qs whose "
        "researcher returned 0 findings (best observable proxy for silent "
        "researcher failure); res w/find / w/uncert / w/both = distinct "
        "researcher_ids; max/res = max findings any one researcher produced "
        "(high vs mean ⇒ concentration).[/dim]"
    )


def _print_category_aggregates(results: list[dict]) -> None:
    """Print the two category-design aggregate lines: did the
    contradictory category actually surface contradictions, did the
    sparse category actually emit honesty signals.

    Computed at print time from the per-prompt rows. No-ops cleanly when
    the eval ran zero prompts in either category (e.g., `--only easy-1`).
    """
    contra = contradiction_aggregate(results)
    honesty = honesty_aggregate(results)

    console.print("")
    if contra["category_total"]:
        rate = contra["category_match_rate"]
        cell = f"[green]{rate:.0%}[/green]" if rate == 1.0 else (
            f"[yellow]{rate:.0%}[/yellow]" if rate >= 0.5 else f"[red]{rate:.0%}[/red]"
        )
        console.print(
            f"Contradiction surfacing rate (contradictory category): "
            f"{contra['category_with_contradictions']}/{contra['category_total']} "
            f"prompts ({cell})"
        )
    if honesty["category_total"]:
        rate = honesty["category_match_rate"]
        cell = f"[green]{rate:.0%}[/green]" if rate == 1.0 else (
            f"[yellow]{rate:.0%}[/yellow]" if rate >= 0.5 else f"[red]{rate:.0%}[/red]"
        )
        console.print(
            f"Honesty surfacing rate (sparse category):              "
            f"{honesty['category_with_signal']}/{honesty['category_total']} "
            f"prompts ({cell})"
        )


# ---------------------------------------------------------------------------
# Reproducibility-mode pretty-printer.
# ---------------------------------------------------------------------------


_REPRO_FMT: list[tuple[str, str, str]] = [
    ("grounding_rate",          "grounding",        "{:.1%}"),
    ("coverage_rate",           "coverage",         "{:.1%}"),
    ("f1",                      "F1",               "{:.1%}"),
    ("judge_overall",           "judge",            "{:.2f}"),
    ("calibration_slope",       "calib slope",      "{:+.2f}"),
    ("n_claims",                "#claims",          "{:.1f}"),
    ("n_subquestions",          "#sub-questions",   "{:.1f}"),
    ("citation_avg_per_claim",  "cite/claim",       "{:.2f}"),
    ("citation_load_bearing",   "cite-load",        "{:.1%}"),
    ("citation_max_src_share",  "max-src-share",    "{:.1%}"),
    ("source_quality_mean_cited", "src q (cited)",  "{:.2f}"),
    ("contradictions_n_surfaced", "contras",        "{:.1f}"),
    ("honesty_n_notes",         "uncert notes",     "{:.1f}"),
    ("honesty_n_caveats",       "caveats",          "{:.1f}"),
    ("subq_overlap_mean",       "sq overlap",       "{:.2f}"),
    ("n_researchers_with_findings",     "researchers w/find",     "{:.1f}"),
    ("n_researchers_with_uncertainty",  "researchers w/uncert",   "{:.1f}"),
    ("n_subquestions_without_findings", "sub-qs w/o findings",    "{:.1f}"),
    ("total_findings",                  "total findings",         "{:.1f}"),
    ("total_uncertainty_notes",         "total uncert notes",     "{:.1f}"),
    ("n_unique_urls_cited",             "unique URLs cited",      "{:.1f}"),
    ("n_unique_domains_cited",          "unique domains cited",   "{:.1f}"),
    ("n_unique_urls_harvested",         "unique URLs harvested",  "{:.1f}"),
    ("n_unique_domains_harvested",      "unique domains harv",    "{:.1f}"),
    ("shape_confidence",                "shape confidence",       "{:.2f}"),
    ("plan_surface_coverage",   "plan-cov",         "{:.2f}"),
    ("plan_intent_alignment",   "plan-int",         "{:.2f}"),
    ("kae_ksr",                 "KSR",               "{:.1%}"),
    ("kae_kcr",                 "KCR",               "{:.1%}"),
    ("kae_kor",                 "KOR",               "{:.1%}"),
    ("ace_overall",             "ACE",               "{:.2f}"),
    ("hierarchy_n_leaves",      "tax leaves",        "{:.1f}"),
    ("hierarchy_avg_per_leaf",  "tax avg/leaf",      "{:.2f}"),
    ("elapsed_seconds",         "elapsed (s)",       "{:.1f}"),
    ("cost_usd_total",          "cost ($)",          "{:.4f}"),
    ("tokens_input_total",      "tokens in",         "{:,.0f}"),
    ("tokens_output_total",     "tokens out",        "{:,.0f}"),
]


def _print_reproducibility_summary(
    per_run: list[dict],
    stats: dict,
    jac: dict,
    sq_jac: dict,
    shape_sel: dict,
    sq_count_sel: dict,
    unique_sel: dict,
    *,
    with_taxonomy: bool,
    with_kae: bool,
    with_ace: bool,
):
    """Render the reproducibility summary."""
    n_runs = len(per_run)
    table = Table(title=f"Per-metric variance across {n_runs} runs")
    table.add_column("metric")
    table.add_column("mean", justify="right")
    table.add_column("stdev", justify="right")
    table.add_column("min", justify="right")
    table.add_column("max", justify="right")
    table.add_column("n", justify="right")

    for key, label, fmt in _REPRO_FMT:
        if key not in stats:
            continue
        s = stats[key]
        if "fraction_true" in s:
            table.add_row(label, f"{s['fraction_true']:.0%} true",
                          "—", "—", "—", str(s['n']))
            continue
        table.add_row(
            label,
            fmt.format(s['mean']),
            fmt.format(s['stdev']),
            fmt.format(s['min']),
            fmt.format(s['max']),
            str(s['n']),
        )

    if with_taxonomy and "hierarchy_over_segmented" in stats:
        s = stats["hierarchy_over_segmented"]
        table.add_row(
            "tax over-seg?",
            f"{s['fraction_true']:.0%} of runs",
            "—", "—", "—", str(s['n']),
        )

    console.print(table)

    if shape_sel["n_runs_with_shape"]:
        modal_color = (
            "green" if shape_sel["modal_share"] == 1.0
            else "yellow" if shape_sel["modal_share"] >= 0.6
            else "red"
        )
        dist_str = ", ".join(
            f"{shape}={count}"
            for shape, count in sorted(
                shape_sel["distribution"].items(),
                key=lambda kv: -kv[1],
            )
        )
        console.print(
            f"\nShape selection: [{modal_color}]"
            f"{shape_sel['modal_shape']}[/{modal_color}] "
            f"chosen in {shape_sel['modal_share']:.0%} of runs "
            f"({dist_str}); classifier confidence "
            f"{shape_sel['mean_confidence']:.2f} "
            f"± {shape_sel['stdev_confidence']:.2f}"
        )
    else:
        console.print(
            "\n[dim]Shape selection: classifier never populated rr.shape "
            "(disabled or fallback fired).[/dim]"
        )

    if shape_sel.get("confidence_by_shape"):
        for shape, cs in sorted(
            shape_sel["confidence_by_shape"].items(),
            key=lambda kv: -kv[1]["n"],
        ):
            console.print(
                f"  └ when picked {shape}: confidence "
                f"{cs['mean']:.2f} ± {cs['stdev']:.2f} "
                f"(min {cs['min']:.2f}, max {cs['max']:.2f}, n={cs['n']})"
            )

    if sq_count_sel["n_runs"]:
        modal_count = sq_count_sel["modal_count"]
        modal_share = sq_count_sel["modal_share"]
        sq_color = (
            "green" if modal_share == 1.0
            else "yellow" if modal_share >= 0.6
            else "red"
        )
        sq_dist = ", ".join(
            f"{count}sq={n}"
            for count, n in sorted(
                sq_count_sel["distribution"].items(), key=lambda kv: -kv[1],
            )
        )
        console.print(
            f"\nSub-question count: modal=[{sq_color}]{modal_count}[/{sq_color}] "
            f"({modal_share:.0%} of runs); distribution {{{sq_dist}}}; "
            f"mean {sq_count_sel['mean']:.1f} ± {sq_count_sel['stdev']:.1f}"
        )

    cited_url_jac = unique_sel.get("cited_url_jaccard", {})
    cited_dom_jac = unique_sel.get("cited_domain_jaccard", {})
    harv_url_jac = unique_sel.get("harvested_url_jaccard", {})
    harv_dom_jac = unique_sel.get("harvested_domain_jaccard", {})
    if cited_url_jac:
        def _color(mj: float) -> str:
            return (
                "green" if mj >= 0.7
                else "yellow" if mj >= 0.4
                else "red"
            )
        console.print(
            f"\nUnique-source set Jaccard across runs:"
            f"\n  cited URLs:        "
            f"[{_color(cited_url_jac['mean_jaccard'])}]"
            f"{cited_url_jac['mean_jaccard']:.2f}"
            f"[/{_color(cited_url_jac['mean_jaccard'])}]"
            f"\n  cited domains:     "
            f"[{_color(cited_dom_jac['mean_jaccard'])}]"
            f"{cited_dom_jac['mean_jaccard']:.2f}"
            f"[/{_color(cited_dom_jac['mean_jaccard'])}]"
            f"\n  harvested URLs:    "
            f"[{_color(harv_url_jac['mean_jaccard'])}]"
            f"{harv_url_jac['mean_jaccard']:.2f}"
            f"[/{_color(harv_url_jac['mean_jaccard'])}]"
            f"\n  harvested domains: "
            f"[{_color(harv_dom_jac['mean_jaccard'])}]"
            f"{harv_dom_jac['mean_jaccard']:.2f}"
            f"[/{_color(harv_dom_jac['mean_jaccard'])}]"
        )

    console.print(
        f"\nMean pairwise claim Jaccard:        {jac['mean_jaccard']:.3f}"
    )
    console.print(
        f"Mean pairwise sub-question Jaccard: {sq_jac['mean_jaccard']:.3f}"
    )
    if sq_jac['mean_jaccard'] < 0.6 and jac['mean_jaccard'] < 0.6:
        console.print(
            "[yellow]Both Jaccards low: instability likely starts at the "
            "planner. Try lowering planner temperature.[/yellow]"
        )
    elif sq_jac['mean_jaccard'] >= 0.7 and jac['mean_jaccard'] < 0.5:
        console.print(
            "[yellow]Plan stable but claims unstable: instability is "
            "downstream (researcher source discovery / writer synthesis), "
            "NOT the planner.[/yellow]"
        )

    legend = (
        "\n[dim]stdev = population stdev across the runs. "
        "Jaccards measure SET overlap on outputs; the per-metric stats "
        "measure NUMERIC stability.[/dim]"
    )
    console.print(legend)


if __name__ == "__main__":
    app()
