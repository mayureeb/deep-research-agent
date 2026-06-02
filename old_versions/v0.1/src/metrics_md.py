"""Per-run metrics markdown renderer.

Sibling of `agent/exporter.py`: writes a paired metrics.md alongside the
user-facing final.md. Sections: header, evaluation metrics, final-output
audit, and a collapsed operational footnote.

Pure renderer — no LLM calls. Eval calls are owned by main.py. Each
eval result enters as either a success dict or `{"error": "..."}` / None;
failures render inline as "(eval failed: ...)" and never raise.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .state import ResearchRun


def render_metrics_markdown(
    rr: ResearchRun,
    *,
    eval_results: dict[str, Any] | None = None,
    paired_final_path: str | None = None,
) -> str:
    """Render a per-run metrics markdown string.

    Args:
        rr: The ResearchRun. May have None for metrics / metadata /
            final_output (degenerate runs); each section degrades.
        eval_results: dict with optional keys "grounding", "calibration",
            "coverage", "judge". Each value is either the eval module's
            success-dict or `{"error": "..."}` or None. Missing keys
            render as "(not run)".
        paired_final_path: Path to the run's final.md if known. Rendered
            as a relative cross-link in the header. None when the
            exporter failed.

    Returns:
        A markdown string. Always non-empty; degenerate runs still
        produce a sensible placeholder.
    """
    eval_results = eval_results or {}
    sections: list[str] = []
    sections.append(_render_header(rr, paired_final_path))
    sections.append(_render_eval(eval_results))
    sections.append(_render_final_output_audit(rr))
    sections.append(_render_operational_footnote(rr))
    return "\n".join(s for s in sections if s).rstrip() + "\n"


def write_metrics_markdown(
    rr: ResearchRun,
    *,
    eval_results: dict[str, Any] | None = None,
    out_dir: str,
    paired_final_path: str | None = None,
) -> Path:
    """Render and write the metrics.md to disk; return its Path.

    When `paired_final_path` is provided, the filename mirrors it with the
    `final_` prefix swapped for `metrics_`. Otherwise we generate
    `metrics_<stamp>_<uuid8>_<slug>.md`.
    """
    out_dir_path = Path(out_dir)
    out_dir_path.mkdir(parents=True, exist_ok=True)

    if paired_final_path:
        final_name = Path(paired_final_path).name
        # Swap the leading "final_" prefix; otherwise prepend "metrics_".
        if final_name.startswith("final_"):
            metrics_name = "metrics_" + final_name[len("final_"):]
        else:
            metrics_name = "metrics_" + final_name
        out_path = out_dir_path / metrics_name
    else:
        slug = re.sub(r"[^a-z0-9]+", "_", rr.user_prompt.lower())[:40].strip("_")
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        out_path = out_dir_path / (
            f"metrics_{stamp}_{uuid.uuid4().hex[:8]}_{slug}.md"
        )

    body = render_metrics_markdown(
        rr, eval_results=eval_results, paired_final_path=paired_final_path,
    )
    out_path.write_text(body, encoding="utf-8")
    return out_path


# ── Section renderers ──────────────────────────────────────────────────────


def _render_header(rr: ResearchRun, paired_final_path: str | None) -> str:
    """Top of the file: prompt, timing, link to the paired final.md."""
    started = rr.started_at.isoformat() if rr.started_at else "(unknown)"
    duration = (
        f"{rr.metrics.elapsed_seconds:.1f}s" if rr.metrics else "(unknown)"
    )
    final_link = "—"
    if paired_final_path:
        # Render as a basename-only same-directory link.
        rel = Path(paired_final_path).name
        final_link = f"[{rel}](./{rel})"

    title_slug = _truncate(rr.user_prompt.replace("\n", " ").strip(), 80)
    return (
        f"# Run metrics — {title_slug}\n\n"
        f"> **Prompt:** {rr.user_prompt}\n"
        f"> **Started:** {started}\n"
        f"> **Duration:** {duration}\n"
        f"> **Final output:** {final_link}\n"
    )


def _render_eval(eval_results: dict[str, Any]) -> str:
    """Headline evaluation metrics: grounding, calibration, coverage, judge."""
    parts: list[str] = ["## Evaluation metrics\n"]
    parts.append(_render_grounding(eval_results.get("grounding")))
    parts.append(_render_calibration(eval_results.get("calibration")))
    parts.append(_render_coverage(eval_results.get("coverage")))
    parts.append(_render_judge(eval_results.get("judge")))
    return "\n".join(parts)


def _render_grounding(g: dict[str, Any] | None) -> str:
    """Per-claim grounding table + headline rate."""
    head = "### Grounding\n"
    if g is None:
        return head + "_not run_\n"
    if "error" in g:
        return head + f"_(eval failed: {g['error']})_\n"
    rate = g.get("rate", 0.0)
    details = g.get("details", []) or []
    n_grounded = sum(1 for d in details if d.get("grounded"))
    n_total = len(details)
    body = [head]
    body.append(
        f"**Rate:** {rate:.0%}  ({n_grounded}/{n_total} claims grounded)\n"
    )
    if details:
        body.append("| claim | grounded | reason |")
        body.append("|---:|:---:|---|")
        for d in details:
            ci = d.get("claim_index", "?")
            mark = "✅" if d.get("grounded") else "❌"
            reason = _md_escape_cell(d.get("reason", ""))
            body.append(f"| {ci} | {mark} | {reason} |")
        body.append("")
    return "\n".join(body)


def _render_calibration(c: dict[str, Any] | None) -> str:
    """Reliability buckets — n, grounded_rate, mean stated confidence."""
    head = "### Calibration\n"
    if c is None:
        return head + "_not run_\n"
    if "error" in c:
        return head + f"_(eval failed: {c['error']})_\n"
    buckets = c.get("buckets", []) or []
    n_claims = c.get("n_claims", 0)
    body = [head, f"**Total claims:** {n_claims}\n"]
    body.append("| confidence range | n | grounded rate | mean stated confidence |")
    body.append("|---|---:|---:|---:|")
    for b in buckets:
        lo, hi = b.get("range", (0, 0))
        n = b.get("n", 0)
        gr = b.get("grounded_rate")
        mc = b.get("mean_stated_confidence")
        gr_s = "—" if gr is None else f"{gr:.0%}"
        mc_s = "—" if mc is None else f"{mc:.2f}"
        body.append(f"| [{lo:.2f}, {hi:.2f}) | {n} | {gr_s} | {mc_s} |")
    body.append("")
    return "\n".join(body)


def _render_coverage(cov: dict[str, Any] | None) -> str:
    """Coverage rate + covered/missed sub-question id lists."""
    head = "### Coverage\n"
    if cov is None:
        return head + "_not run_\n"
    if "error" in cov:
        return head + f"_(eval failed: {cov['error']})_\n"
    rate = cov.get("rate", 0.0)
    covered = cov.get("covered", []) or []
    missed = cov.get("missed", []) or []
    body = [head, f"**Rate:** {rate:.0%}  ({len(covered)} covered, {len(missed)} missed)\n"]
    if covered:
        body.append("- **Covered:** " + ", ".join(f"`{x}`" for x in covered))
    if missed:
        body.append("- **Missed:** " + ", ".join(f"`{x}`" for x in missed))
    body.append("")
    return "\n".join(body)


def _render_judge(j: dict[str, Any] | None) -> str:
    """LLM judge rubric (5 dimensions + overall + notes)."""
    head = "### LLM judge rubric\n"
    if j is None:
        return head + "_not run_\n"
    if "error" in j:
        return head + f"_(eval failed: {j['error']})_\n"
    body = [head]
    body.append("| dimension | score |")
    body.append("|---|---:|")
    for k in (
        "correctness", "completeness", "calibration",
        "source_quality", "conflict_handling", "overall",
    ):
        v = j.get(k)
        v_s = "—" if v is None else f"{float(v):.2f} / 5"
        body.append(f"| {k.replace('_', ' ')} | {v_s} |")
    notes = j.get("notes")
    if notes:
        body.append("")
        body.append(f"> {notes}")
    body.append("")
    return "\n".join(body)


def _render_final_output_audit(rr: ResearchRun) -> str:
    """Exporter's own self-check. Skipped on runs without a FinalOutput."""
    fo = rr.final_output
    if fo is None:
        return ""
    head = "## Final output audit\n"
    body = [head]
    body.append(f"- **Format detected:** {fo.format_detected}")
    if fo.notes:
        body.append(f"- **Notes:** {fo.notes}")
    counts = {"satisfied": 0, "partial": 0, "not_satisfied": 0, "not_applicable": 0}
    for cc in fo.conditions or []:
        if cc.status in counts:
            counts[cc.status] += 1
    body.append(
        f"- **Conditions:** {counts['satisfied']} satisfied / "
        f"{counts['partial']} partial / "
        f"{counts['not_satisfied']} not satisfied / "
        f"{counts['not_applicable']} n/a"
    )
    if fo.unmet_conditions:
        body.append("- **Unmet:**")
        for u in fo.unmet_conditions:
            body.append(f"  - {u}")
    body.append("")
    return "\n".join(body)


def _render_operational_footnote(rr: ResearchRun) -> str:
    """Operational data: cost/tokens/elapsed, per-stage breakdown, tool mix,
    revision rounds, run metadata, and a mermaid cost-share pie. Wrapped in
    a `<details>` block so it doesn't clutter the headline."""
    parts: list[str] = ["<details>", "<summary>Operational details</summary>", ""]

    m = rr.metrics
    if m is not None:
        parts.append("### Cost & latency")
        parts.append(
            f"- Total cost: **${m.total_cost_usd:.4f}**  |  "
            f"input tokens: {m.total_input_tokens}  |  "
            f"output tokens: {m.total_output_tokens}  |  "
            f"elapsed: {m.elapsed_seconds:.1f}s  |  "
            f"revision rounds: {m.revision_rounds}"
        )
        parts.append("")

        # Per-stage table.
        if m.per_stage:
            parts.append("### Per-stage")
            parts.append("| stage | calls | input tokens | output tokens | cost (USD) | elapsed (s) |")
            parts.append("|---|---:|---:|---:|---:|---:|")
            for stage in sorted(m.per_stage):
                s = m.per_stage[stage]
                parts.append(
                    f"| {stage} | {s.calls} | {s.input_tokens} | {s.output_tokens} | "
                    f"${s.cost_usd:.4f} | {s.elapsed_seconds:.1f} |"
                )
            parts.append("")

            # Mermaid pie of cost-share; skipped on single-slice runs.
            cost_stages = [(k, v.cost_usd) for k, v in m.per_stage.items() if v.cost_usd > 0]
            if len(cost_stages) >= 2:
                parts.append("```mermaid")
                parts.append("pie title Cost share by stage")
                for stage, cost in sorted(cost_stages):
                    parts.append(f'    "{stage}" : {cost:.4f}')
                parts.append("```")
                parts.append("")

        # Tool mix table.
        tm = m.tool_mix
        parts.append("### Tool mix")
        parts.append("| tool | calls |")
        parts.append("|---|---:|")
        for k in ("web_search", "search_papers", "fetch_url",
                  "save_finding", "note_uncertainty"):
            parts.append(f"| {k} | {getattr(tm, k, 0)} |")
        parts.append("")

    # Run metadata.
    md = rr.metadata
    if md is not None:
        parts.append("### Run metadata")
        if md.commit_hash:
            parts.append(f"- commit: `{md.commit_hash}`  branch: `{md.branch or '?'}`")
        if md.captured_at:
            parts.append(f"- captured_at: {md.captured_at.isoformat()}")
        if md.models:
            model_line = ", ".join(f"{k}=`{v}`" for k, v in sorted(md.models.items()))
            parts.append(f"- models: {model_line}")
        parts.append("")

    parts.append("</details>")
    parts.append("")
    return "\n".join(parts)


# ── Helpers ────────────────────────────────────────────────────────────────


def _truncate(s: str, n: int) -> str:
    """Truncate `s` to `n` chars with an ellipsis suffix."""
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _md_escape_cell(s: str) -> str:
    """Escape pipes and newlines that would break a markdown table cell."""
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()
