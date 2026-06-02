"""Orchestrator: owns the full pipeline control flow.

Pipeline:
  classify shape → decompose → parallel researchers → aggregate findings →
  detect contradictions → compute confidence axes → write draft →
  critic + revise loop (with re-fetch verifier per round) → caveats fallback →
  final exporter → emit ResearchRun.
"""
from __future__ import annotations

import asyncio
import dataclasses
import subprocess
import time
import uuid
from contextlib import contextmanager

from .. import events
from ..config import Config
from ..llm import (
    aggregate_calls_by_stage,
    chat_json,
    reset_call_log,
    track_stage,
)
from ..state import (
    Decomposition,
    FinalOutput,
    FindingsStore,
    Report,
    ResearchRun,
    RunMetadata,
    RunMetrics,
    StageStats,
    SubQuestion,
    ToolMix,
    TraceStats,
    VerifyReport,
    VerifyResult,
)
from ..tools.fetch import FetchContext
from .confidence import combine_axes, compute_agreement
from .critic import critique
from .exporter import export_final
from .reconciler import detect_and_surface_contradictions
from .refetch_verifier import verify_cited
from .researcher import ResearcherResult, run_researcher
from .shape_classifier import classify_shape, planner_annex
from .writer import write_report


DECOMPOSE_SYSTEM = """You are the lead planner for a deep research investigation.

Given a research prompt, decompose it into 3–7 sub-questions that, taken together, would let a researcher answer the prompt comprehensively.

Good decompositions:
- Each sub-question is concrete and answerable from sources (not "what is the meaning of X?").
- Sub-questions partition the topic — minimal overlap.
- For contested or contradictory topics, include sub-questions that probe the disagreement explicitly.
- For sparse/emerging topics, include a sub-question on "what evidence is missing or weak?"
- Cover BOTH the substance of the question AND the meta-level (methodology, evaluation, dissent) when relevant.

Do NOT generate a sub-question that is just "summarize X" — that's the writer's job.

Each sub-question gets:
- A unique short id (e.g., "sq1", "sq2", ...)
- The question itself
- A one-sentence rationale for why this sub-question matters to the overall prompt
- confidence: 0.0–1.0 — how load-bearing this sub-question is for answering the user's prompt. Default 0.5 if uncertain. Use 0.8+ for sub-questions whose absence would make the report incomplete; use < 0.5 for nice-to-have angles.
- depends_on: list of OTHER sub-question ids that this one depends on (empty list if independent). Used for documenting question structure; does NOT serialize execution.
- preferred_search: "search_papers" for technical/scientific sub-questions; "web_search" for current/practical/news; "either" when both are useful. The researcher uses this as a hint."""


async def run_research(cfg: Config, prompt: str) -> ResearchRun:
    """Run the full pipeline on one prompt."""
    started = time.time()
    reset_call_log()
    metadata = _capture_run_metadata(cfg)
    stage_seconds: dict[str, float] = {}
    tool_mix = ToolMix()
    researcher_latencies: list[float] = []
    researcher_trace_logs: list[list] = []

    events.orch("classifying prompt shape...")
    with _stage_clock("classify_shape", stage_seconds), track_stage("classify_shape"):
        shape = classify_shape(cfg, prompt)

    events.orch("decomposing prompt...")
    with _stage_clock("decompose", stage_seconds), track_stage("decompose"):
        decomp = chat_json(
            cfg.anthropic_api_key,
            cfg.writer_model,
            DECOMPOSE_SYSTEM + planner_annex(shape),
            f"Research prompt:\n\n{prompt}",
            Decomposition,
            max_retries=cfg.decompose_max_retries,
        )
        decomp = _enforce_subq_count(decomp, cfg)
    events.planner(f"decomposed into {len(decomp.subquestions)} sub-questions:")
    for sq in decomp.subquestions:
        events.planner(f"  [{sq.id}] {sq.question}")

    findings_store = FindingsStore()
    fetch_ctx = FetchContext()

    events.orch(
        f"spawning {len(decomp.subquestions)} researchers "
        f"(max {cfg.parallel_researchers} in parallel)..."
    )
    sem = asyncio.Semaphore(cfg.parallel_researchers)
    tasks = [
        run_researcher(cfg, sq, sem, fetch_ctx)
        for sq in decomp.subquestions
    ]
    with _stage_clock("researcher", stage_seconds), track_stage("researcher"):
        per_subq_findings = await asyncio.gather(*tasks, return_exceptions=True)

    failed = 0
    for result in per_subq_findings:
        if isinstance(result, Exception):
            failed += 1
            events.orch(f"  ⚠ a researcher raised: {result}")
            continue
        rr_result: ResearcherResult = result
        for f in rr_result.findings:
            findings_store.add(f)
        for n in rr_result.uncertainty:
            findings_store.add_uncertainty(n)
        _add_tool_counts(tool_mix, rr_result.tool_counts)
        researcher_latencies.append(rr_result.elapsed_seconds)
        researcher_trace_logs.append(rr_result.trace_log)

    events.orch(
        f"all researchers returned — {len(findings_store.findings)} findings, "
        f"{len(findings_store.uncertainty_notes)} uncertainty notes, "
        f"{len(findings_store.all_sources())} unique sources"
        + (f" ({failed} failures)" if failed else "")
    )

    events.orch("detecting contradictions...")
    with _stage_clock("reconciler", stage_seconds), track_stage("reconciler"):
        contradictions = detect_and_surface_contradictions(cfg, decomp, findings_store)
    events.reconciler(f"found {len(contradictions)} contradiction(s)")

    with _stage_clock("confidence", stage_seconds):
        _compute_confidence_axes(findings_store, contradictions)
    n_disagree = sum(1 for f in findings_store.findings if f.axis_disagreement)
    events.orch(
        f"computed confidence axes — {n_disagree} finding(s) flagged "
        f"with axis disagreement"
    )

    events.orch("writing report (initial draft)...")
    with _stage_clock("writer", stage_seconds), track_stage("writer"):
        report = write_report(
            cfg, prompt, decomp, findings_store, contradictions, shape=shape,
        )
    events.writer(f"drafted {len(report.claims)} claims")
    critic_history: list = []
    verify_history: list[VerifyReport] = []
    verify_cache: dict[int, VerifyResult] = {}

    for round_num in range(cfg.max_revisions + 1):
        cited_indices = sorted({
            fi for c in report.claims for fi in c.supporting_finding_indices
            if 0 <= fi < len(findings_store.findings)
        })
        if not cfg.verifier_enabled:
            verify_report = VerifyReport(results=[])
        elif cited_indices:
            with _stage_clock("verifier", stage_seconds), track_stage("verifier"):
                verify_report = await verify_cited(
                    cfg, findings_store.findings, cited_indices,
                    fetch_ctx=fetch_ctx, cached=verify_cache,
                )
            n_fab = len(verify_report.fabricated_indices)
            n_unr = len(verify_report.unreachable_indices)
            events.orch(
                f"verifier round {round_num + 1}: "
                f"{n_fab} fabricated, {n_unr} unreachable, "
                f"{len(verify_report.results) - n_fab - n_unr} verified/drifted"
            )
        else:
            verify_report = VerifyReport(results=[])
        verify_history.append(verify_report)

        events.orch(f"fact-checking (round {round_num + 1})...")
        with _stage_clock("critic", stage_seconds), track_stage("critic"):
            critic_report = critique(
                cfg, prompt, decomp, findings_store, report,
                verify_report=verify_report,
            )
        critic_history.append(critic_report)
        if critic_report.approved:
            events.critic("approved ✓")
        else:
            events.critic(
                f"flagged {len(critic_report.issues)} issue(s)"
                + (" — revising" if round_num < cfg.max_revisions else " — max rounds hit, applying caveats fallback")
            )
        if critic_report.approved or round_num == cfg.max_revisions:
            break

        events.orch(f"writer revising (round {round_num + 1})...")
        with _stage_clock("writer", stage_seconds), track_stage("writer"):
            report = write_report(
                cfg, prompt, decomp, findings_store, contradictions,
                prior_report=report, critic_issues=critic_report.issues,
                verify_report=verify_report, shape=shape,
            )
        events.writer(f"revised — {len(report.claims)} claims")

    final_critic = critic_history[-1] if critic_history else None
    if final_critic is not None and not final_critic.approved:
        report.caveats.insert(0, _format_unresolved_caveat(final_critic.issues))
        events.orch(
            f"caveats fallback: surfaced {len(final_critic.issues)} unresolved "
            f"issue(s) in report.caveats"
        )

    final_output = None
    try:
        events.orch("rendering final output...")
        with _stage_clock("exporter", stage_seconds), track_stage("exporter"):
            final_output = export_final(
                cfg, prompt, decomp, findings_store, report,
            )
        unmet = len(final_output.unmet_conditions)
        events.orch(
            f"exporter done — format='{final_output.format_detected}', "
            f"{len(final_output.conditions)} conditions audited"
            + (f", {unmet} unmet" if unmet else ", all satisfied")
        )
    except Exception as e:
        events.orch(f"exporter failed: {e} — emitting degenerate FinalOutput")
        final_output = FinalOutput(
            format_detected="(exporter failed)",
            content=report.summary or "",
            notes=f"exporter raised: {e}",
        )

    metrics = _compute_metrics(
        decomp, findings_store, critic_history, report, started,
        stage_seconds=stage_seconds,
        researcher_latencies=researcher_latencies,
        tool_mix=tool_mix,
        researcher_trace_logs=researcher_trace_logs,
    )
    events.orch(
        f"done — coverage {metrics.coverage_rate:.0%}, "
        f"grounding {metrics.final_grounding_rate:.0%}, "
        f"{metrics.elapsed_seconds:.1f}s, "
        f"cost ${metrics.total_cost_usd:.3f}"
    )

    return ResearchRun(
        user_prompt=prompt,
        shape=shape,
        decomposition=decomp,
        findings=findings_store,
        report=report,
        critic_history=critic_history,
        verify_history=verify_history,
        metadata=metadata,
        metrics=metrics,
        final_output=final_output,
    )


def _enforce_subq_count(decomp: Decomposition, cfg: Config) -> Decomposition:
    """Clamp the planner's output to [min, max] and repair duplicate / blank ids."""
    sqs = decomp.subquestions
    if len(sqs) < cfg.min_subquestions:
        return decomp
    if len(sqs) > cfg.max_subquestions:
        sqs = sqs[: cfg.max_subquestions]
    seen = set()
    for sq in sqs:
        if not sq.id or sq.id in seen:
            sq.id = f"sq-{uuid.uuid4().hex[:4]}"
        seen.add(sq.id)
    return Decomposition(subquestions=sqs)


def _format_unresolved_caveat(issues) -> str:
    """Format unresolved critic issues as a single caveat string."""
    bullets = "\n".join(
        f"  • Claim #{i.claim_index} ({i.issue_type}): {i.explanation}"
        for i in issues
    )
    return (
        "WARNING — bounded revision exhausted with critic issues still "
        f"open. The report ships with {len(issues)} unresolved critic "
        f"issue(s):\n{bullets}"
    )


def _compute_confidence_axes(findings_store, contradictions) -> None:
    """Post-fan-out confidence pass; mutates findings with agreement_score,
    combined_confidence, and axis_disagreement."""
    findings = findings_store.findings
    for i, f in enumerate(findings):
        f.agreement_score = compute_agreement(
            finding_index=i,
            finding_subq_id=f.subquestion_id,
            all_findings=findings,
            contradictions=contradictions,
        )
        combined, disagreement = combine_axes(
            self_reported=f.confidence,
            source_quality=f.source_quality,
            agreement=f.agreement_score,
        )
        f.combined_confidence = combined
        f.axis_disagreement = disagreement


def _compute_metrics(
    decomp: Decomposition,
    findings: FindingsStore,
    critic_history: list,
    report,
    started: float,
    *,
    stage_seconds: dict[str, float] | None = None,
    researcher_latencies: list[float] | None = None,
    tool_mix: ToolMix | None = None,
    researcher_trace_logs: list[list] | None = None,
) -> RunMetrics:
    """Produce the RunMetrics emitted with the report."""
    sq_ids_with_findings = {f.subquestion_id for f in findings.findings}
    coverage = (
        len(sq_ids_with_findings) / max(1, len(decomp.subquestions))
    )

    final_critic = critic_history[-1] if critic_history else None
    if final_critic is None or report is None or not report.claims:
        grounding_rate = 0.0
    elif final_critic.approved:
        grounding_rate = 1.0
    else:
        flagged = {issue.claim_index for issue in final_critic.issues}
        total = len(report.claims)
        approved = total - len(flagged & set(range(total)))
        grounding_rate = approved / total if total else 0.0

    cost_rollup = aggregate_calls_by_stage()
    stage_seconds = stage_seconds or {}
    all_stages = set(cost_rollup) | set(stage_seconds)
    per_stage: dict[str, StageStats] = {}
    for stage in sorted(all_stages):
        rollup = cost_rollup.get(stage)
        per_stage[stage] = StageStats(
            calls=rollup.calls if rollup else 0,
            input_tokens=rollup.input_tokens if rollup else 0,
            output_tokens=rollup.output_tokens if rollup else 0,
            cost_usd=rollup.cost_usd if rollup else 0.0,
            elapsed_seconds=stage_seconds.get(stage, 0.0),
        )
    total_in = sum(s.input_tokens for s in per_stage.values())
    total_out = sum(s.output_tokens for s in per_stage.values())
    total_cost = sum(s.cost_usd for s in per_stage.values())

    researcher_latencies = researcher_latencies or []
    if researcher_latencies:
        rl_max = max(researcher_latencies)
        rl_avg = sum(researcher_latencies) / len(researcher_latencies)
    else:
        rl_max = 0.0
        rl_avg = 0.0

    trace_stats = _compute_trace_stats(researcher_trace_logs or [])

    return RunMetrics(
        total_subquestions=len(decomp.subquestions),
        subquestions_with_findings=len(sq_ids_with_findings),
        total_findings=len(findings.findings),
        unique_sources=len(findings.all_sources()),
        revision_rounds=max(0, len(critic_history) - 1),
        final_grounding_rate=grounding_rate,
        coverage_rate=coverage,
        elapsed_seconds=time.time() - started,
        per_stage=per_stage,
        total_input_tokens=total_in,
        total_output_tokens=total_out,
        total_cost_usd=total_cost,
        researcher_latency_max=rl_max,
        researcher_latency_avg=rl_avg,
        tool_mix=tool_mix or ToolMix(),
        trace_stats=trace_stats,
    )


def _compute_trace_stats(trace_logs: list[list]) -> TraceStats:
    """Roll up per-researcher TrACE step logs into a single TraceStats."""
    flat = [step for log in trace_logs for step in log]
    if not flat:
        return TraceStats()

    n = len(flat)
    total_k = sum(s.k_used for s in flat)
    sum_alpha = sum(s.alpha for s in flat)
    n_high = sum(1 for s in flat if s.agreement_high)
    n_overrides = sum(1 for s in flat if getattr(s, "end_turn_override", False))
    n_save_prefs = sum(1 for s in flat if getattr(s, "save_finding_preferred", False))

    per_tool_acc: dict[str, tuple[int, int]] = {}
    for s in flat:
        acc = per_tool_acc.get(s.chosen_tool, (0, 0))
        per_tool_acc[s.chosen_tool] = (acc[0] + s.k_used, acc[1] + 1)
    per_tool_mean_k = {
        tool: (k_sum / count) for tool, (k_sum, count) in per_tool_acc.items()
    }

    return TraceStats(
        steps_total=n,
        total_k=total_k,
        mean_k=total_k / n,
        mean_alpha=sum_alpha / n,
        high_agreement_rate=n_high / n,
        per_tool_mean_k=per_tool_mean_k,
        n_end_turn_overrides=n_overrides,
        n_save_finding_preferences=n_save_prefs,
    )


@contextmanager
def _stage_clock(name: str, store: dict[str, float]):
    """Time a block and accumulate elapsed seconds under `name`. Wraps with the
    same name accumulate."""
    started = time.monotonic()
    try:
        yield
    finally:
        store[name] = store.get(name, 0.0) + (time.monotonic() - started)


def _add_tool_counts(mix: ToolMix, counts: dict[str, int]) -> None:
    """Accumulate one researcher's per-tool counts into the run ToolMix."""
    for name, n in counts.items():
        if hasattr(mix, name):
            setattr(mix, name, getattr(mix, name) + n)


def _capture_run_metadata(cfg: Config) -> RunMetadata:
    """Build a RunMetadata for the current run. Best-effort; failures leave fields empty."""
    commit = _git_oneliner(["git", "rev-parse", "HEAD"])
    branch = _git_oneliner(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    models = {
        "researcher": cfg.researcher_model,
        "writer": cfg.writer_model,
        "critic": cfg.critic_model,
        "judge": cfg.judge_model,
        "kae_judge": cfg.kae_judge_model,
    }
    snapshot: dict = {}
    for f in dataclasses.fields(cfg):
        val = getattr(cfg, f.name)
        if f.name.endswith("api_key"):
            snapshot[f.name] = "<set>" if val else "<unset>"
        else:
            snapshot[f.name] = val
    return RunMetadata(
        commit_hash=commit,
        branch=branch,
        models=models,
        config_snapshot=snapshot,
    )


def _git_oneliner(cmd: list[str]) -> str:
    """Run a one-line git command and return stripped stdout, or "" on any failure."""
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=2.0, check=False,
        )
        if out.returncode != 0:
            return ""
        return out.stdout.strip()
    except Exception:
        return ""
