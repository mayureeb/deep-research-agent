"""Orchestrator: owns the full control flow.

Pipeline:
  1 decompose                       — 1 LLM call, schema-validated, retry-on-fail
  2 parallel researchers            — N async sub-agents, fresh context each
  3 aggregate to shared blackboard  — append into FindingsStore
  4 detect contradictions           — 1 LLM call (reconciler); surface, don't resolve
  5 writer                          — 1 LLM call, structured Report
  6 critic                          — 1 LLM call per round; revise once if flagged
  6b final exporter                 — render Report into the user-requested format
                                       and audit prompt-derived conditions
  7 emit                            — ResearchRun + RunMetrics
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
    ResearchRun,
    RunMetadata,
    RunMetrics,
    StageStats,
    SubQuestion,
    ToolMix,
)
from ..tools.fetch import FetchContext
from .critic import critique
from .exporter import export_final
from .reconciler import detect_and_surface_contradictions
from .researcher import run_researcher
from .writer import write_report


# Decomposer prompt. Tightly coupled to _enforce_subq_count below.
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
- A one-sentence rationale for why this sub-question matters to the overall prompt"""


async def run_research(cfg: Config, prompt: str) -> ResearchRun:
    """Run the full pipeline on one prompt.

    Args:
        cfg: Loaded Config (api keys, model choices, all caps).
        prompt: The research question.

    Returns:
        A complete ResearchRun, suitable for serialization and replay via
        inspect.py.
    """
    started = time.time()

    reset_call_log()
    metadata = _capture_run_metadata(cfg)
    # Per-stage wall-clock accumulator; each phase wraps in `_stage_clock(...)`.
    stage_seconds: dict[str, float] = {}
    tool_mix = ToolMix()

    # 1 Decompose: single LLM call, schema-validated, retry-on-fail. Uses
    # writer_model because the per-run cost is small and quality matters.
    events.orch("decomposing prompt...")
    with _stage_clock("decompose", stage_seconds), track_stage("decompose"):
        decomp = chat_json(
            cfg.anthropic_api_key,
            cfg.writer_model,
            DECOMPOSE_SYSTEM,
            f"Research prompt:\n\n{prompt}",
            Decomposition,
            max_retries=cfg.decompose_max_retries,
        )
    # Clamp count and repair duplicate ids; see _enforce_subq_count.
    decomp = _enforce_subq_count(decomp, cfg)
    events.planner(f"decomposed into {len(decomp.subquestions)} sub-questions:")
    for sq in decomp.subquestions:
        events.planner(f"  [{sq.id}] {sq.question}")

    # 2 Parallel researchers: one coroutine per sub-question, semaphore-bounded.
    # Each researcher owns a fresh context window.
    findings_store = FindingsStore()
    events.orch(
        f"spawning {len(decomp.subquestions)} researchers "
        f"(max {cfg.parallel_researchers} in parallel)..."
    )
    sem = asyncio.Semaphore(cfg.parallel_researchers)
    # Shared per-run FetchContext so per-domain failures accumulate ACROSS
    # researchers (not 3*N attempts on the same flaky domain).
    fetch_ctx = FetchContext()
    with _stage_clock("researcher", stage_seconds), track_stage("researcher"):
        tasks = [run_researcher(cfg, sq, sem, fetch_ctx) for sq in decomp.subquestions]
        # return_exceptions=True: one researcher's exception doesn't kill the run.
        per_subq_results = await asyncio.gather(*tasks, return_exceptions=True)

    # 3 Aggregate.
    failed = 0
    researcher_latencies: list[float] = []
    for result in per_subq_results:
        if isinstance(result, Exception):
            failed += 1
            events.orch(f"  ⚠ a researcher raised: {result}")
            continue
        for f in result.findings:
            findings_store.add(f)
        for n in result.uncertainty:
            findings_store.add_uncertainty(n)
        _add_tool_counts(tool_mix, result.tool_counts)
        researcher_latencies.append(result.elapsed_seconds)

    events.orch(
        f"all researchers returned — {len(findings_store.findings)} findings, "
        f"{len(findings_store.uncertainty_notes)} uncertainty notes, "
        f"{len(findings_store.all_sources())} unique sources"
        + (f" ({failed} failures)" if failed else "")
    )

    # 4 Reconcile: one LLM call. Surfaces contradictions as prose paragraphs;
    # does not pick a winner.
    events.orch("detecting contradictions...")
    with _stage_clock("reconciler", stage_seconds), track_stage("reconciler"):
        contradiction_notes = detect_and_surface_contradictions(cfg, decomp, findings_store)
    events.reconciler(f"found {len(contradiction_notes)} contradiction(s)")

    # 5 Write initial draft: structured Report with supporting_finding_indices.
    events.orch("writing report (initial draft)...")
    with _stage_clock("writer", stage_seconds), track_stage("writer"):
        report = write_report(cfg, prompt, decomp, findings_store, contradiction_notes)
    events.writer(f"drafted {len(report.claims)} claims")
    critic_history: list = []

    # 6 Critique → revise loop. At most max_revisions+1 rounds. The default
    # is one revision pass; if the critic still flags after that the report
    # ships as-is. Unresolved issues remain in critic_history for forensic
    # inspection.
    for round_num in range(cfg.max_revisions + 1):
        events.orch(f"fact-checking (round {round_num + 1})...")
        with _stage_clock("critic", stage_seconds), track_stage("critic"):
            critic_report = critique(cfg, prompt, decomp, findings_store, report)
        critic_history.append(critic_report)
        if critic_report.approved:
            events.critic("approved ✓")
        else:
            events.critic(
                f"flagged {len(critic_report.issues)} issue(s)"
                + (f" — revising" if round_num < cfg.max_revisions else " — max rounds hit, shipping")
            )
        if critic_report.approved or round_num == cfg.max_revisions:
            break
        events.orch(f"writer revising (round {round_num + 1})...")
        # Pass prior_report + critic_issues so the writer targets fixes.
        with _stage_clock("writer", stage_seconds), track_stage("writer"):
            report = write_report(
                cfg, prompt, decomp, findings_store, contradiction_notes,
                prior_report=report, critic_issues=critic_report.issues,
            )
        events.writer(f"revised — {len(report.claims)} claims")

    # 6b Final exporter: render the post-critic Report into the user-requested
    # format and audit each prompt-derived condition. One LLM call. Failure
    # emits a degenerate FinalOutput rather than killing the run. Never
    # modifies report.claims — render only.
    final_output: FinalOutput | None = None
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
        # Don't drop the run because the presentation layer hiccupped.
        events.orch(f"exporter failed: {e} — emitting degenerate FinalOutput")
        final_output = FinalOutput(
            format_detected="(exporter failed)",
            content=report.summary or "",
            notes=f"exporter raised: {e}",
        )

    # 7 Compute metrics + emit.
    metrics = _compute_metrics(
        decomp, findings_store, critic_history, report, started,
        stage_seconds=stage_seconds, tool_mix=tool_mix,
    )
    events.orch(
        f"done — coverage {metrics.coverage_rate:.0%}, "
        f"grounding {metrics.final_grounding_rate:.0%}, "
        f"cost ${metrics.total_cost_usd:.3f}, "
        f"{metrics.elapsed_seconds:.1f}s"
    )

    return ResearchRun(
        user_prompt=prompt,
        decomposition=decomp,
        findings=findings_store,
        report=report,
        critic_history=critic_history,
        metrics=metrics,
        metadata=metadata,
        final_output=final_output,
    )


def _enforce_subq_count(decomp: Decomposition, cfg: Config) -> Decomposition:
    """Clamp the planner's output to [min, max] and repair duplicate / blank ids.

    Below min, we proceed with what we got rather than synthesize fakes —
    a too-short decomposition is itself a signal. Above max we trim;
    duplicate ids would silently merge findings downstream so they get
    repaired with short UUIDs.
    """
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


def _compute_metrics(
    decomp: Decomposition,
    findings: FindingsStore,
    critic_history: list,
    report,
    started: float,
    *,
    stage_seconds: dict[str, float] | None = None,
    tool_mix: ToolMix | None = None,
) -> RunMetrics:
    """Produce the RunMetrics emitted with the report.

    These are the runtime-computable signals; LLM-judged metrics live in
    eval/metrics/. `final_grounding_rate` is `approved_claims / total_claims`.

    Optional kwargs:
      * stage_seconds: per-phase wall-clock dict; merged with the per-stage
        cost rollup to populate per_stage[stage] StageStats.
      * tool_mix: ToolMix from the researcher fan-out.
    """
    sq_ids_with_findings = {f.subquestion_id for f in findings.findings}
    coverage = (
        len(sq_ids_with_findings) / max(1, len(decomp.subquestions))
    )
    final_critic = critic_history[-1] if critic_history else None

    # Grounding = approved_claims / total_claims, where a claim is "approved"
    # if the final critic round did not flag it.
    total_claims = len(report.claims) if report else 0
    if total_claims == 0:
        grounding_rate = 0.0
    elif final_critic is None:
        # No critic ran — optimistic default.
        grounding_rate = 1.0
    elif final_critic.approved:
        grounding_rate = 1.0
    else:
        flagged_indices = {iss.claim_index for iss in final_critic.issues}
        approved_claims = total_claims - len(flagged_indices)
        grounding_rate = max(0.0, approved_claims) / total_claims

    # Build the per-stage rollup as the union of wall-clock stages and
    # call-log stages, so both pure-Python and LLM-only phases appear.
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
    # Run-level rollups across stages.
    total_in = sum(s.input_tokens for s in per_stage.values())
    total_out = sum(s.output_tokens for s in per_stage.values())
    total_cost = sum(s.cost_usd for s in per_stage.values())

    return RunMetrics(
        total_subquestions=len(decomp.subquestions),
        subquestions_with_findings=len(sq_ids_with_findings),
        total_findings=len(findings.findings),
        unique_sources=len(findings.all_sources()),
        # revisions = critic rounds - 1 (the first critic round is on the
        # initial draft, not a revision).
        revision_rounds=max(0, len(critic_history) - 1),
        final_grounding_rate=grounding_rate,
        coverage_rate=coverage,
        elapsed_seconds=time.time() - started,
        per_stage=per_stage,
        total_input_tokens=total_in,
        total_output_tokens=total_out,
        total_cost_usd=total_cost,
        tool_mix=tool_mix or ToolMix(),
    )


@contextmanager
def _stage_clock(name: str, store: dict[str, float]):
    """Time a block and accumulate elapsed seconds under `name`. Repeat
    wraps with the same name accumulate (e.g. "writer" for draft + revisions).
    """
    started = time.monotonic()
    try:
        yield
    finally:
        store[name] = store.get(name, 0.0) + (time.monotonic() - started)


def _add_tool_counts(mix: ToolMix, counts: dict[str, int]) -> None:
    """Accumulate one researcher's per-tool counts into the run ToolMix.
    Unknown tool names are silently dropped."""
    for name, n in counts.items():
        if hasattr(mix, name):
            setattr(mix, name, getattr(mix, name) + n)


def _capture_run_metadata(cfg: Config) -> RunMetadata:
    """Best-effort RunMetadata. Failures leave fields empty rather than raising.

    Any Config field name ending in "api_key" is redacted from the snapshot.
    """
    commit = _git_oneliner(["git", "rev-parse", "HEAD"])
    branch = _git_oneliner(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    models = {
        "researcher": cfg.researcher_model,
        "writer": cfg.writer_model,
        "critic": cfg.critic_model,
        "judge": cfg.judge_model,
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
    """Run a one-line git command and return its stripped stdout, or "" on failure."""
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=2.0, check=False,
        )
        if out.returncode != 0:
            return ""
        return out.stdout.strip()
    except Exception:
        return ""
