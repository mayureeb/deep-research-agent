"""Per-run metrics markdown renderer.

Pure renderer: takes pre-computed eval results and formats them as
markdown. No LLM calls. Eval failures surface inline as "(eval failed: ...)"
rather than raising.
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
    """Render a per-run metrics markdown string."""
    eval_results = eval_results or {}
    sections: list[str] = []
    sections.append(_render_header(rr, paired_final_path))
    sections.append(_render_tier1(eval_results))
    sections.append(_render_tier2(eval_results))
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

    Naming: when `paired_final_path` is provided, the filename mirrors
    it with the prefix `final_` swapped for `metrics_` (so the two
    files sit side-by-side in a directory listing). Otherwise we
    generate our own `metrics_<stamp>_<uuid8>_<slug>.md` using the same
    convention the exporter uses.
    """
    out_dir_path = Path(out_dir)
    out_dir_path.mkdir(parents=True, exist_ok=True)

    if paired_final_path:
        final_name = Path(paired_final_path).name
        # Swap the leading "final_" prefix; if the file didn't follow
        # that convention (e.g. user override), prepend "metrics_" so
        # we still get a distinct name.
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


def _render_tier1(eval_results: dict[str, Any]) -> str:
    """Tier 1 — the headline evaluation block."""
    parts: list[str] = ["## Tier 1 — Headline evaluation metrics\n"]
    parts.append(_render_grounding(eval_results.get("grounding")))
    parts.append(_render_fabrication(eval_results.get("fabrication")))
    parts.append(_render_contradictions(eval_results.get("contradictions")))
    parts.append(_render_honesty(eval_results.get("honesty")))
    parts.append(_render_source_quality(eval_results.get("source_quality")))
    parts.append(_render_calibration(eval_results.get("calibration")))
    parts.append(_render_coverage(eval_results.get("coverage")))
    parts.append(_render_judge(eval_results.get("judge")))
    return "\n".join(parts)


def _render_tier2(eval_results: dict[str, Any]) -> str:
    """Tier 2 — experimental rigor / depth of understanding."""
    parts: list[str] = ["## Tier 2 — Experimental rigor / depth of understanding\n"]
    parts.append(_render_researcher_signals(eval_results.get("researcher_signals")))
    parts.append(_render_citation_density(eval_results.get("citation_density")))

    pj = eval_results.get("plan_judge")
    if pj is not None:
        parts.append(_render_plan_judge(pj))
    kae = eval_results.get("kae")
    if kae is not None:
        parts.append(_render_kae(kae))
    ace = eval_results.get("ace")
    if ace is not None:
        parts.append(_render_ace(ace))

    return "\n".join(parts)


# ── Tier 1 sub-renderers ───────────────────────────────────────────────────


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


def _render_fabrication(f: dict[str, Any] | None) -> str:
    """Fabrication-rate headline + drift companion + run mode."""
    head = "### Fabrication rate\n"
    if f is None:
        return head + "_not run_\n"
    if "error" in f:
        return head + f"_(eval failed: {f['error']})_\n"
    rate = float(f.get("rate", 0.0))
    drift = float(f.get("drift_rate", 0.0))
    n_approved = f.get("approved_total", 0)
    n_fab = f.get("approved_with_fabricated", 0)
    n_drift = f.get("approved_with_drifted", 0)
    fab_idx = f.get("fabricated_finding_indices", []) or []
    rerun = f.get("verifier_was_rerun", False)
    body = [head]
    body.append(
        f"**Rate:** {rate:.0%}  "
        f"({n_fab}/{n_approved} approved claims cite a fabricated finding)\n"
    )
    body.append(
        f"- Drift rate: {drift:.0%}  ({n_drift}/{n_approved} approved claims cite a drifted finding)"
    )
    if fab_idx:
        body.append(
            f"- Fabricated finding indices: {', '.join(f'`{i}`' for i in fab_idx)}"
        )
    body.append(
        f"- Verifier source: {'post-hoc re-run (no native verify_history)' if rerun else 'native verify_history'}"
    )
    body.append("")
    return "\n".join(body)


def _render_contradictions(c: dict[str, Any] | None) -> str:
    """Contradiction surfacing rate by severity and type."""
    head = "### Contradictions surfaced\n"
    if c is None:
        return head + "_not run_\n"
    if "error" in c:
        return head + f"_(eval failed: {c['error']})_\n"
    n = c.get("n_surfaced", 0)
    by_sev = c.get("by_severity", {}) or {}
    by_typ = c.get("by_type", {}) or {}
    body = [head, f"**N surfaced:** {n}\n"]
    if n:
        sev_str = ", ".join(f"{k}={by_sev.get(k, 0)}" for k in ("minor", "moderate", "major"))
        typ_str = ", ".join(
            f"{k}={by_typ.get(k, 0)}"
            for k in ("factual", "methodological", "framing", "temporal", "other")
        )
        body.append(f"- By severity: {sev_str}")
        body.append(f"- By type: {typ_str}")
    body.append("")
    return "\n".join(body)


def _render_honesty(h: dict[str, Any] | None) -> str:
    """Honesty — uncertainty notes + caveats + the substring heuristic."""
    head = "### Honesty signals\n"
    if h is None:
        return head + "_not run_\n"
    if "error" in h:
        return head + f"_(eval failed: {h['error']})_\n"
    n_notes = h.get("n_uncertainty_notes", 0)
    n_caveats = h.get("n_caveats", 0)
    frac = h.get("fraction_subqs_with_uncertainty", 0.0) or 0.0
    refs = h.get("caveats_reference_uncertainty", False)
    body = [head]
    body.append(
        f"- Uncertainty notes: **{n_notes}**  |  caveats: **{n_caveats}**"
    )
    body.append(
        f"- Sub-questions with ≥1 uncertainty note: **{frac:.0%}**"
    )
    body.append(
        f"- Caveats reference uncertainty: **{'yes' if refs else 'no'}**"
    )
    body.append("")
    return "\n".join(body)


def _render_source_quality(s: dict[str, Any] | None) -> str:
    """Source-quality aggregate: cited vs. all, plus tier breakdown."""
    head = "### Source quality\n"
    if s is None:
        return head + "_not run_\n"
    if "error" in s:
        return head + f"_(eval failed: {s['error']})_\n"
    n_cited = s.get("n_cited_findings", 0)
    n_all = s.get("n_all_findings", 0)
    mean_c = s.get("mean_cited_quality", 0.0)
    median_c = s.get("median_cited_quality", 0.0)
    min_c = s.get("min_cited_quality", 0.0)
    mean_a = s.get("mean_all_quality", 0.0)
    by_cited = s.get("by_tier_cited", {}) or {}
    by_all = s.get("by_tier_all", {}) or {}
    body = [head]
    body.append(
        f"- Cited findings: **{n_cited}** of {n_all} harvested  |  "
        f"mean quality: **{mean_c:.2f}** (cited) vs. {mean_a:.2f} (all)"
    )
    body.append(
        f"- Cited quality — median: {median_c:.2f}, min: {min_c:.2f}"
    )
    tiers = list(by_cited.keys()) or list(by_all.keys())
    if tiers:
        body.append("")
        body.append("| tier | cited | all |")
        body.append("|---|---:|---:|")
        for t in tiers:
            body.append(f"| {t} | {by_cited.get(t, 0)} | {by_all.get(t, 0)} |")
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


# ── Tier 2 sub-renderers ───────────────────────────────────────────────────


def _render_researcher_signals(r: dict[str, Any] | None) -> str:
    """Researcher pool counts + concentration."""
    head = "### Researcher signals\n"
    if r is None:
        return head + "_not run_\n"
    if "error" in r:
        return head + f"_(eval failed: {r['error']})_\n"
    body = [head]
    body.append(
        f"- Sub-questions: **{r.get('n_subquestions', 0)}** total, "
        f"**{r.get('n_subquestions_without_findings', 0)}** without findings"
    )
    body.append(
        f"- Researchers with findings: **{r.get('n_researchers_with_findings', 0)}**  |  "
        f"with uncertainty: **{r.get('n_researchers_with_uncertainty', 0)}**  |  "
        f"with both: **{r.get('n_researchers_with_both', 0)}**"
    )
    body.append(
        f"- Findings: {r.get('total_findings', 0)} total  |  "
        f"uncertainty notes: {r.get('total_uncertainty_notes', 0)}"
    )
    body.append(
        f"- Per active researcher: mean **{float(r.get('mean_findings_per_active_researcher', 0.0)):.1f}** "
        f"findings, max **{r.get('max_findings_one_researcher', 0)}**"
    )
    body.append("")
    return "\n".join(body)


def _render_citation_density(c: dict[str, Any] | None) -> str:
    """Citation density: load-bearing fraction, per-claim shape, source concentration."""
    head = "### Citation density\n"
    if c is None:
        return head + "_not run_\n"
    if "error" in c:
        return head + f"_(eval failed: {c['error']})_\n"
    body = [head]
    body.append(
        f"- Load-bearing fraction: **{float(c.get('load_bearing_fraction', 0.0)):.0%}**  "
        f"({c.get('n_cited_findings', 0)} cited / {c.get('n_findings', 0)} harvested)"
    )
    body.append(
        f"- Per-claim citations: avg **{float(c.get('avg_per_claim', 0.0)):.2f}**  |  "
        f"median **{float(c.get('median_per_claim', 0.0)):.2f}**  |  "
        f"uncited claims: {c.get('uncited_claims', 0)}"
    )
    body.append(
        f"- Source concentration (Herfindahl): **{float(c.get('source_concentration_herfindahl', 0.0)):.3f}**  |  "
        f"max single-source share: **{float(c.get('max_source_share', 0.0)):.0%}**"
    )
    body.append(
        f"- Distinct domains per claim (mean): **{float(c.get('avg_per_claim_distinct_domains', 0.0)):.2f}**"
    )
    body.append("")
    return "\n".join(body)


def _render_plan_judge(pj: Any) -> str:
    """Plan judge: surface_coverage + intent_alignment."""
    head = "### Plan judge\n"
    if pj is None:
        return head + "_not run_\n"
    if isinstance(pj, dict) and "error" in pj:
        return head + f"_(eval failed: {pj['error']})_\n"
    sc = _attr_or_key(pj, "surface_coverage", 0.0)
    ia = _attr_or_key(pj, "intent_alignment", 0.0)
    missing = _attr_or_key(pj, "missing_axes", []) or []
    drift = _attr_or_key(pj, "drift_examples", []) or []
    reasoning = _attr_or_key(pj, "reasoning", "") or ""
    body = [head]
    body.append(
        f"- Surface coverage: **{float(sc):.2f}**  |  "
        f"intent alignment: **{float(ia):.2f}**"
    )
    if missing:
        body.append("- Missing axes:")
        for m in missing:
            body.append(f"  - {m}")
    if drift:
        body.append("- Drift examples:")
        for d in drift:
            body.append(f"  - {d}")
    if reasoning:
        body.append("")
        body.append(f"> {reasoning}")
    body.append("")
    return "\n".join(body)


def _render_kae(kae: Any) -> str:
    """KAE — keypoint-aligned evaluation (KSR / KCR / KOR)."""
    head = "### KAE — Keypoint-Aligned Evaluation\n"
    if kae is None:
        return head + "_not run_\n"
    if isinstance(kae, dict) and "error" in kae:
        return head + f"_(eval failed: {kae['error']})_\n"
    ksr = _attr_or_key(kae, "ksr", 0.0)
    kcr = _attr_or_key(kae, "kcr", 0.0)
    kor = _attr_or_key(kae, "kor", 0.0)
    n_kp = _attr_or_key(kae, "n_keypoints", 0)
    n_src = _attr_or_key(kae, "n_sources", 0)
    body = [head]
    body.append(
        f"- KSR (supported): **{float(ksr):.0%}**  |  "
        f"KCR (conflict): **{float(kcr):.0%}**  |  "
        f"KOR (omission): **{float(kor):.0%}**"
    )
    body.append(f"- Keypoints: {n_kp}  |  sources: {n_src}")
    body.append("")
    return "\n".join(body)


def _render_ace(ace: Any) -> str:
    """ACE — adaptively-generated checklist evaluation."""
    head = "### ACE — Adaptive Checklist Evaluation\n"
    if ace is None:
        return head + "_not run_\n"
    if isinstance(ace, dict) and "error" in ace:
        return head + f"_(eval failed: {ace['error']})_\n"
    overall = _attr_or_key(ace, "overall", 0.0)
    items = _attr_or_key(ace, "item_scores", []) or []
    notes = _attr_or_key(ace, "notes", "") or ""
    body = [head]
    body.append(f"**Overall:** {float(overall):.2f} / 1.00\n")
    if items:
        body.append("| item | met | score | reason |")
        body.append("|---|:---:|---:|---|")
        for it in items:
            item_text = _md_escape_cell(_attr_or_key(it, "item", ""))
            met = bool(_attr_or_key(it, "met", False))
            score = float(_attr_or_key(it, "score", 0.0))
            reason = _md_escape_cell(_attr_or_key(it, "reason", ""))
            mark = "✅" if met else "❌"
            body.append(f"| {item_text} | {mark} | {score:.2f} | {reason} |")
        body.append("")
    if notes:
        body.append(f"> {notes}")
        body.append("")
    return "\n".join(body)


# ── Final-output audit + operational footnote (unchanged from v0.1) ────────


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
    """Operational data: cost, tokens, per-stage breakdown, tool mix, metadata."""
    parts: list[str] = ["<details>", "<summary>Tier 3 — Operational details</summary>", ""]

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
        if m.researcher_latency_max:
            parts.append(
                f"- Researcher latency: max {m.researcher_latency_max:.1f}s, "
                f"avg {m.researcher_latency_avg:.1f}s"
            )
        parts.append("")

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

            cost_stages = [(k, v.cost_usd) for k, v in m.per_stage.items() if v.cost_usd > 0]
            if len(cost_stages) >= 2:
                parts.append("```mermaid")
                parts.append("pie title Cost share by stage")
                for stage, cost in sorted(cost_stages):
                    parts.append(f'    "{stage}" : {cost:.4f}')
                parts.append("```")
                parts.append("")

        tm = m.tool_mix
        parts.append("### Tool mix")
        parts.append("| tool | calls |")
        parts.append("|---|---:|")
        for k in ("web_search", "search_papers", "fetch_url",
                  "save_finding", "note_uncertainty"):
            parts.append(f"| {k} | {getattr(tm, k, 0)} |")
        parts.append("")

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
    """Escape pipes and newlines so a markdown table cell stays well-formed."""
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


def _attr_or_key(obj: Any, key: str, default: Any) -> Any:
    """Read `key` off either a pydantic model (attribute) or a dict."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)
