"""Pretty-print a saved ResearchRun (or BaselineRun) JSON.

Usage:
    python -m src.inspect path/to/run.json
    python -m src.inspect run.json --section findings
    python -m src.inspect run.json --claim 3
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

from .state import BaselineRun, ResearchRun


app = typer.Typer(add_completion=False, no_args_is_help=True)
console = Console()


@app.command()
def main(
    path: Path = typer.Argument(..., exists=True, help="Path to a saved run JSON"),
    section: str = typer.Option(
        None, "--section", "-s",
        help="Only show one section: prompt | decomp | findings | report | critic | metrics | metadata",
    ),
    claim: int = typer.Option(
        None, "--claim", "-c",
        help="Drill into a single claim by index (shows full evidence trail)",
    ),
):
    """Inspect a saved research run."""
    raw = json.loads(path.read_text())

    if "report" in raw and isinstance(raw["report"], dict) and "final_report_text" in raw["report"]:
        rr = BaselineRun.model_validate(raw)
        _show_baseline(rr, section)
        return

    rr = ResearchRun.model_validate(raw)

    if claim is not None:
        _drill_into_claim(rr, claim)
        return

    if section:
        _show_one(rr, section)
        return

    _show_prompt(rr)
    _show_decomposition(rr)
    _show_findings(rr)
    _show_report(rr)
    _show_critic_history(rr)
    _show_metrics(rr)
    _show_metadata(rr)


# ── Section renderers ─────────────────────────────────────────────────────


def _show_prompt(rr: ResearchRun):
    """Print the original prompt."""
    console.print(Rule("PROMPT", style="cyan"))
    console.print(rr.user_prompt)
    console.print()


def _show_decomposition(rr: ResearchRun):
    """Render the planner's output: id, question, rationale per sub-question."""
    console.print(Rule(
        f"DECOMPOSITION  ({len(rr.decomposition.subquestions)} sub-questions)",
        style="yellow",
    ))
    for sq in rr.decomposition.subquestions:
        console.print(f"  [bold yellow][{sq.id}][/bold yellow] {sq.question}")
        if sq.rationale:
            console.print(f"      [dim]rationale: {sq.rationale}[/dim]")
    console.print()


def _show_findings(rr: ResearchRun):
    """Render every finding + uncertainty note, grouped by sub-question."""
    n_findings = len(rr.findings.findings)
    n_sources = len(rr.findings.all_sources())
    n_uncertainty = len(rr.findings.uncertainty_notes)
    console.print(Rule(
        f"FINDINGS  ({n_findings} total, {n_sources} unique sources, "
        f"{n_uncertainty} uncertainty notes)",
        style="blue",
    ))

    by_sq: dict[str, list[tuple[int, object]]] = {}
    for i, f in enumerate(rr.findings.findings):
        by_sq.setdefault(f.subquestion_id, []).append((i, f))

    for sq in rr.decomposition.subquestions:
        items = by_sq.get(sq.id, [])
        notes = rr.findings.uncertainty_for(sq.id)
        if not items and not notes:
            console.print(f"  [bold blue][{sq.id}][/bold blue] {sq.question}")
            console.print(f"      [red](no findings)[/red]")
            continue
        console.print(f"  [bold blue][{sq.id}][/bold blue] {sq.question}  "
                      f"[dim]({len(items)} findings, {len(notes)} uncertainty)[/dim]")
        for idx, f in items:
            console.print(
                f"      [bold]#{idx}[/bold] [dim](conf {f.confidence:.2f}, "
                f"by {f.researcher_id})[/dim]"
            )
            console.print(f"        claim:    {f.claim}")
            console.print(f"        evidence: [dim]\"{_truncate(f.evidence, 200)}\"[/dim]")
            console.print(f"        source:   [link]{f.source_url}[/link]")
        for n in notes:
            console.print(
                f"      [yellow]⚠ uncertainty[/yellow] [dim](by {n.researcher_id})[/dim]"
            )
            console.print(f"        topic:  {n.topic}")
            console.print(f"        reason: [dim]{n.reason}[/dim]")
        console.print()


def _show_report(rr: ResearchRun):
    """Render the writer's output: summary, claims with their finding refs,
    contradictions, caveats."""
    if not rr.report:
        console.print("[red](no report)[/red]")
        return
    console.print(Rule(
        f"REPORT  ({len(rr.report.claims)} claims)",
        style="green",
    ))
    console.print(Panel(rr.report.summary, title="Summary", border_style="green"))
    console.print()
    for ci, claim in enumerate(rr.report.claims):
        console.print(
            f"  [bold green]Claim #{ci}[/bold green] "
            f"[dim](conf {claim.confidence:.2f})[/dim]"
        )
        console.print(f"    {claim.claim}")
        if claim.supporting_finding_indices:
            console.print(f"    [dim]← supported by findings:[/dim]")
            for fi in claim.supporting_finding_indices:
                if 0 <= fi < len(rr.findings.findings):
                    f = rr.findings.findings[fi]
                    console.print(f"      [dim]#{fi}[/dim] {_truncate(f.claim, 90)}")
        else:
            console.print(f"    [red](no supporting findings cited)[/red]")
        console.print()
    if rr.report.contradictions_surfaced:
        console.print(Rule("Contradictions surfaced", style="yellow"))
        for c in rr.report.contradictions_surfaced:
            console.print(
                f"  • [bold yellow]{c.severity}[/bold yellow] "
                f"[dim]{c.type}[/dim]  findings={c.finding_ids}"
            )
            console.print(f"    {c.description}")
        console.print()
    if rr.report.caveats:
        console.print(Rule("Caveats", style="yellow"))
        for c in rr.report.caveats:
            console.print(f"  • {c}")
        console.print()

    if rr.final_output:
        fo = rr.final_output
        console.print(Rule(
            f"Final output (format: {fo.format_detected})", style="cyan",
        ))
        if fo.notes:
            console.print(f"  [dim]{fo.notes}[/dim]")
            console.print()
        console.print(fo.content)
        console.print()
        if fo.conditions:
            console.print(Rule("Condition audit", style="cyan"))
            for cc in fo.conditions:
                color = {
                    "satisfied": "green", "partial": "yellow",
                    "not_satisfied": "red", "not_applicable": "dim",
                }.get(cc.status, "white")
                console.print(
                    f"  [{color}]{cc.status:>14}[/{color}]  {cc.condition}"
                )
                if cc.evidence:
                    console.print(f"                  [dim]evidence:[/dim] {cc.evidence}")
                if cc.notes:
                    console.print(f"                  [dim]notes:[/dim] {cc.notes}")
            console.print()
        if fo.output_file_path:
            console.print(f"  [dim]Markdown saved to:[/dim] {fo.output_file_path}")
            console.print()


def _show_critic_history(rr: ResearchRun):
    """Render every critic round and its issues."""
    if not rr.critic_history:
        return
    console.print(Rule(
        f"CRITIC HISTORY  ({len(rr.critic_history)} round(s))",
        style="red",
    ))
    for ri, cr in enumerate(rr.critic_history):
        status = "[green]APPROVED ✓[/green]" if cr.approved else \
                 f"[red]{len(cr.issues)} issue(s)[/red]"
        console.print(f"  Round {ri + 1}: {status}")
        for issue in cr.issues:
            console.print(
                f"    • claim #{issue.claim_index} [yellow]({issue.issue_type})[/yellow]: "
                f"{issue.explanation}"
            )
    console.print()


def _show_metrics(rr: ResearchRun):
    """Render the run metrics in a dense, scannable list."""
    if not rr.metrics:
        return
    m = rr.metrics
    console.print(Rule("METRICS", style="cyan"))
    console.print(f"  coverage:        {m.coverage_rate:.0%}  "
                  f"({m.subquestions_with_findings}/{m.total_subquestions} sub-questions)")
    console.print(f"  grounding:       {m.final_grounding_rate:.0%}")
    console.print(f"  total findings:  {m.total_findings}")
    console.print(f"  unique sources:  {m.unique_sources}")
    console.print(f"  revision rounds: {m.revision_rounds}")
    console.print(f"  elapsed:         {m.elapsed_seconds:.1f}s")

    if m.total_input_tokens or m.total_output_tokens:
        console.print(
            f"  tokens:          {m.total_input_tokens:,} in / "
            f"{m.total_output_tokens:,} out"
        )
        console.print(f"  cost:            ${m.total_cost_usd:.4f}")
    if m.researcher_latency_max:
        console.print(
            f"  researcher lat.: max {m.researcher_latency_max:.1f}s, "
            f"avg {m.researcher_latency_avg:.1f}s"
        )
    tm = m.tool_mix
    used = [(n, getattr(tm, n)) for n in
            ("web_search", "search_papers", "fetch_url",
             "save_finding", "note_uncertainty")
            if getattr(tm, n)]
    if used:
        console.print(
            "  tool mix:        " + "  ".join(f"{n}={c}" for n, c in used)
        )
    if m.per_stage:
        console.print()
        console.print(Rule("PER-STAGE", style="cyan"))
        console.print(
            f"  {'stage':<14} {'calls':>5}  "
            f"{'in tok':>7}  {'out tok':>7}  {'cost':>8}  {'sec':>6}"
        )
        for stage in sorted(m.per_stage):
            s = m.per_stage[stage]
            console.print(
                f"  {stage:<14} {s.calls:>5}  "
                f"{s.input_tokens:>7,}  {s.output_tokens:>7,}  "
                f"${s.cost_usd:>6.4f}  {s.elapsed_seconds:>6.1f}"
            )
    console.print()


def _show_metadata(rr: ResearchRun):
    """Render the run metadata envelope."""
    if not rr.metadata:
        return
    md = rr.metadata
    console.print(Rule("METADATA", style="cyan"))
    console.print(f"  commit:   {md.commit_hash or '—'}")
    console.print(f"  branch:   {md.branch or '—'}")
    console.print(f"  captured: {md.captured_at}")
    if md.models:
        console.print("  models:")
        for role, model in md.models.items():
            console.print(f"    {role:<10} {model}")
    if md.config_snapshot:
        console.print("  config:")
        for k, v in md.config_snapshot.items():
            console.print(f"    {k:<32} {v}")
    console.print()


def _drill_into_claim(rr: ResearchRun, ci: int):
    """Drill into one claim: show its full evidence trail."""
    if not rr.report or ci < 0 or ci >= len(rr.report.claims):
        console.print(f"[red]No claim #{ci}[/red]")
        sys.exit(1)
    claim = rr.report.claims[ci]
    console.print(Rule(f"CLAIM #{ci}  (conf {claim.confidence:.2f})", style="green"))
    console.print(f"\n[bold]{claim.claim}[/bold]\n")
    if not claim.supporting_finding_indices:
        console.print("[red]This claim cites NO findings.[/red]")
        return
    console.print(Rule("Supporting findings (verbatim from researchers)", style="blue"))
    for fi in claim.supporting_finding_indices:
        if not (0 <= fi < len(rr.findings.findings)):
            console.print(f"[red]invalid index #{fi}[/red]")
            continue
        f = rr.findings.findings[fi]
        console.print(f"\n  [bold]Finding #{fi}[/bold] [dim](conf {f.confidence:.2f}, "
                      f"sub-q {f.subquestion_id})[/dim]")
        console.print(f"    claim:    {f.claim}")
        console.print(f"    evidence: \"{f.evidence}\"")
        console.print(f"    source:   [link]{f.source_url}[/link]")


def _show_one(rr: ResearchRun, section: str):
    """Section dispatcher for --section. Accepts short aliases ('p', 'd', etc.)."""
    section = section.lower()
    if section in ("prompt", "p"):
        _show_prompt(rr)
    elif section in ("decomp", "decomposition", "d"):
        _show_decomposition(rr)
    elif section in ("findings", "f"):
        _show_findings(rr)
    elif section in ("report", "r"):
        _show_report(rr)
    elif section in ("critic", "c"):
        _show_critic_history(rr)
    elif section in ("metrics", "m"):
        _show_metrics(rr)
    elif section in ("metadata", "md"):
        _show_metadata(rr)
    else:
        console.print(f"[red]Unknown section: {section}[/red]")
        console.print(
            "Valid: prompt | decomp | findings | report | critic | metrics | "
            "metadata"
        )
        sys.exit(1)


def _show_baseline(br: BaselineRun, section: str | None):
    """Render a BaselineRun: decomposition, summaries, prose report, metrics."""
    console.print(Rule("BASELINE RUN (GPT-Researcher-style)", style="magenta"))
    console.print(f"\n[bold]Prompt:[/bold] {br.user_prompt}\n")
    if section is None or section in ("decomp", "d"):
        console.print(Rule("DECOMPOSITION", style="yellow"))
        for sq in br.decomposition.subquestions:
            console.print(f"  [{sq.id}] {sq.question}")
        console.print()
    if section is None or section in ("summaries", "s"):
        console.print(Rule(
            f"PER-SUB-QUESTION SUMMARIES  ({len(br.report.summaries)})",
            style="blue",
        ))
        for s in br.report.summaries:
            console.print(f"\n  [bold blue][{s.subquestion_id}][/bold blue] "
                          f"{s.subquestion_text}")
            console.print(f"    [dim]sources: {', '.join(s.sources_consulted) or '(none)'}[/dim]")
            console.print(f"    {s.summary_text}")
        console.print()
    if section is None or section in ("report", "r"):
        console.print(Rule("FINAL REPORT (free-form prose)", style="magenta"))
        console.print(br.report.final_report_text)
        console.print()
    console.print(Rule("METRICS", style="cyan"))
    console.print(f"  sub-questions: {len(br.decomposition.subquestions)}")
    console.print(f"  summaries:     {len(br.report.summaries)}")
    console.print(f"  unique sources: {len(br.report.all_sources)}")
    console.print(f"  elapsed:       {br.elapsed_seconds:.1f}s")


def _truncate(s: str, n: int) -> str:
    """Truncate s to n chars, appending '…' if truncated."""
    s = s.strip()
    return s if len(s) <= n else s[:n - 1] + "…"


if __name__ == "__main__":
    app()
