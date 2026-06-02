"""All structured types that flow between agents.

Every cross-agent message is one of these Pydantic models. Pydantic
provides .model_dump_json() / .model_validate_json() for `--out`
persistence and `inspect.py` reload.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Decomposition: planner output. Downstream parallel researchers each scope
# to one SubQuestion.
# ---------------------------------------------------------------------------


class SubQuestion(BaseModel):
    """One concrete, source-answerable question carved out of the user prompt.

    Attributes:
        id: Short stable identifier (e.g. "sq1"). The orchestrator repairs
            duplicates / blanks via UUID before fanning out.
        question: The actual question text the researcher will see.
        rationale: One sentence on why this sub-question matters.
    """
    id: str
    question: str
    rationale: str = ""


class Decomposition(BaseModel):
    """The full set of sub-questions the planner produced for one prompt."""
    subquestions: list[SubQuestion]


# ---------------------------------------------------------------------------
# Findings store: shared blackboard. Researchers append; reconciler / writer
# / critic / metrics all consume from here.
# ---------------------------------------------------------------------------


class Finding(BaseModel):
    """One concrete claim plus the verbatim evidence supporting it.

    Attributes:
        subquestion_id: Which SubQuestion.id this finding answers.
        claim: One sentence, specific and falsifiable.
        evidence: Verbatim or near-verbatim quote from the source.
        source_url: Where `evidence` was extracted from.
        source_title: Page / paper title, when fetch returned one.
        confidence: 0.0–1.0. Stored as `self_reported * source_quality`
            so a blog at self-report 0.9 ends up at 0.45 while a
            peer-reviewed source at 0.9 stays at 0.9.
        self_reported_confidence: What the researcher emitted in
            save_finding, before the source-quality multiplier.
        source_quality: Domain-table lookup score in [0, 1]. See
            agent/confidence.py:compute_source_quality.
        researcher_id: Short tag like "r-a1b2c3" identifying which sub-agent
            produced this finding.
        saved_at: UTC timestamp of when save_finding was called.
        fetched_at: When the source page was fetched (≈ saved_at in v0.1).
        content_hash: SHA-256 hex digest of the verbatim `evidence` text.
            Empty string on legacy runs.
        domain: Registered domain extracted from `source_url` at
            save_finding time.
    """
    subquestion_id: str
    claim: str
    evidence: str = Field(
        description="Verbatim or near-verbatim quote from the source"
    )
    source_url: str
    source_title: str = ""
    confidence: float = Field(ge=0.0, le=1.0)
    self_reported_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_quality: float = Field(default=1.0, ge=0.0, le=1.0)
    researcher_id: str = ""
    saved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    content_hash: str = ""
    domain: str = ""


class UncertaintyNote(BaseModel):
    """Researcher's explicit record that they searched but found no concrete evidence.

    Lets the writer emit a proper caveat rather than ignoring the question.

    Attributes:
        subquestion_id: Which SubQuestion.id the researcher was working on.
        topic: Specific thing they tried to find evidence on.
        reason: Why they couldn't (paywall, no relevant sources, etc.).
        researcher_id: Same scheme as Finding.researcher_id.
    """
    subquestion_id: str
    topic: str
    reason: str
    researcher_id: str = ""


class FindingsStore(BaseModel):
    """The shared blackboard for one research run. Append-only across the run.

    Each researcher returns its full list at the end of its run, and the
    orchestrator merges sequentially — no researcher-vs-researcher races.
    """
    findings: list[Finding] = Field(default_factory=list)
    uncertainty_notes: list[UncertaintyNote] = Field(default_factory=list)

    def add(self, f: Finding) -> None:
        """Append a finding."""
        self.findings.append(f)

    def add_uncertainty(self, n: UncertaintyNote) -> None:
        """Append an uncertainty note."""
        self.uncertainty_notes.append(n)

    def by_subquestion(self, sqid: str) -> list[Finding]:
        """All findings attributed to one SubQuestion."""
        return [f for f in self.findings if f.subquestion_id == sqid]

    def all_sources(self) -> list[str]:
        """Unique source URLs across all findings."""
        return list({f.source_url for f in self.findings})

    def uncertainty_for(self, sqid: str) -> list[UncertaintyNote]:
        """All uncertainty notes for one sub-question."""
        return [n for n in self.uncertainty_notes if n.subquestion_id == sqid]


# ---------------------------------------------------------------------------
# Report: writer's structured output. Claims cite finding indices for
# automated verification.
# ---------------------------------------------------------------------------


class ReportClaim(BaseModel):
    """A claim in the final report, traced to specific findings.

    Attributes:
        claim: One concrete sentence the report is asserting.
        confidence: 0.0–1.0. Should reflect EVIDENCE strength, not prose
            confidence.
        supporting_finding_indices: Indices into `FindingsStore.findings`
            that back this claim.
    """
    claim: str
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_finding_indices: list[int] = Field(
        description="Indices into FindingsStore.findings that back this claim"
    )


class Report(BaseModel):
    """The writer's structured report.

    Attributes:
        user_prompt: Echoed back so the report is self-contained on disk.
        summary: 2-4 sentence overview.
        claims: List of ReportClaim — the meat of the report.
        contradictions_surfaced: Prose paragraphs from the reconciler.
        caveats: Limitations the writer flagged.
    """
    user_prompt: str
    summary: str
    claims: list[ReportClaim]
    contradictions_surfaced: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Critic: verifies grounding. Sees claims + cited findings only.
# ---------------------------------------------------------------------------


class CriticIssue(BaseModel):
    """One problem the critic found with one claim.

    Issue types are typed (Literal[...]) so the writer's revision logic can
    branch without parsing free-form prose.
    """
    claim_index: int
    issue_type: Literal[
        "ungrounded",         # claim not supported by cited findings
        "overstated",         # findings support a weaker version of the claim
        "missing_citation",   # claim has no supporting findings
        "source_mismatch",    # cited finding's evidence doesn't match the claim
    ]
    explanation: str


class CriticReport(BaseModel):
    """Output of one critic pass.

    `approved=True` only when issues is empty. The orchestrator stops the
    revision loop when approved or when max_revisions is hit.
    """
    issues: list[CriticIssue]
    approved: bool


# ---------------------------------------------------------------------------
# Per-stage cost / latency rollups, tool-mix, run metadata.
# ---------------------------------------------------------------------------


class StageStats(BaseModel):
    """Per-stage cost + latency rollup.

    One per pipeline stage in RunMetrics.per_stage. Stages tracked:
    decompose, researcher, reconciler, writer, critic.

    Attributes:
        calls: Number of LLM calls billed to this stage.
        input_tokens / output_tokens: Sum across calls.
        cost_usd: Estimated cost from a coarse per-model price table.
        elapsed_seconds: Wall-clock time in this stage. For the parallel
            researcher fan-out this is the GATHER duration (= max
            researcher latency), not the sum.
    """
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    elapsed_seconds: float = 0.0


class ToolMix(BaseModel):
    """Tool-selection instrumentation across all researchers.

    Aggregated by the orchestrator from each researcher's per-tool counter.
    """
    web_search: int = 0
    search_papers: int = 0
    fetch_url: int = 0
    save_finding: int = 0
    note_uncertainty: int = 0


class RunMetadata(BaseModel):
    """Reproducibility envelope around a ResearchRun.

    Captured by the orchestrator at run-start and never mutated.

    Attributes:
        commit_hash: `git rev-parse HEAD` at run-start. Empty on failure.
        branch: `git rev-parse --abbrev-ref HEAD`. Empty on failure.
        models: {role: model_id} dict.
        config_snapshot: Dataclass-fields-as-dict, with API key fields
            redacted to "<set>"/"<unset>".
        captured_at: UTC at run-start.
    """
    commit_hash: str = ""
    branch: str = ""
    models: dict[str, str] = Field(default_factory=dict)
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    captured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# RunMetrics: emitted alongside the report.
# ---------------------------------------------------------------------------


class RunMetrics(BaseModel):
    """Per-run metrics shipped with the report.

    `final_grounding_rate` is `approved_claims / total_claims` — the share
    of claims in the final report not flagged by the final critic round.
    The LLM-judged grounding number is in eval/metrics/grounding.py.
    """
    total_subquestions: int
    subquestions_with_findings: int
    total_findings: int
    unique_sources: int
    revision_rounds: int
    final_grounding_rate: float = 0.0  # approved_claims / total_claims
    coverage_rate: float = 0.0         # % of sub-questions with ≥1 finding
    elapsed_seconds: float = 0.0

    per_stage: dict[str, StageStats] = Field(default_factory=dict)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0

    tool_mix: ToolMix = Field(default_factory=ToolMix)


class ConditionCheck(BaseModel):
    """One condition extracted from the user prompt + whether the final
    output satisfies it.

    Attributes:
        condition: The condition itself, in the exporter's words.
        status: "satisfied" / "partial" / "not_satisfied" / "not_applicable".
        evidence: A short pointer into the rendered content showing where
            the condition is met (or where it should have been).
        notes: Free-form, optional.
    """
    condition: str
    status: Literal["satisfied", "partial", "not_satisfied", "not_applicable"]
    evidence: str = ""
    notes: str = ""


class FinalOutput(BaseModel):
    """The exporter's output: the post-critic Report rendered into the
    user-requested format, with a condition-satisfaction audit.

    Attributes:
        format_detected: Natural-language description of the format the
            exporter inferred from the prompt.
        content: The rendered final output as a single string. Mermaid
            diagrams use ```mermaid``` fenced blocks so the markdown file
            is self-contained.
        conditions: Every condition the exporter extracted from the
            prompt + its satisfaction status.
        unmet_conditions: Convenience derived list of `condition` strings
            where status is "not_satisfied" or "partial".
        notes: Exporter's overall notes.
        output_file_path: Filesystem path of the markdown rendering.
            None when the file write failed (`notes` carries the exception).
    """
    format_detected: str
    content: str
    conditions: list[ConditionCheck] = Field(default_factory=list)
    unmet_conditions: list[str] = Field(default_factory=list)
    notes: str = ""
    output_file_path: str | None = None


class ResearchRun(BaseModel):
    """The full record of one research execution.

    Serialized to JSON via `--out` and read back by `inspect.py`.

    Attributes:
        user_prompt: Original input.
        decomposition: Planner's output.
        findings: All Finding + UncertaintyNote objects.
        report: Final structured Report (None only on catastrophic failure).
        critic_history: One CriticReport per critic round.
        metrics: RunMetrics.
        metadata: RunMetadata captured at run-start.
        final_output: Post-critic exporter rendering of the Report.
        metrics_file_path: Filesystem path of the per-run metrics
            markdown rendering.
        started_at: UTC timestamp when run() was called.
    """
    user_prompt: str
    decomposition: Decomposition
    findings: FindingsStore
    report: Report | None = None
    critic_history: list[CriticReport] = Field(default_factory=list)
    metrics: RunMetrics | None = None
    metadata: RunMetadata | None = None
    final_output: FinalOutput | None = None
    metrics_file_path: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Baseline pipeline (GPT-Researcher-style): planner -> researchers -> publisher.
# Free-form summaries, free-form report, no critic. Used for the head-to-head
# comparison in eval/baseline_compare.py.
# ---------------------------------------------------------------------------


class BaselineSummary(BaseModel):
    """One sub-question's worth of free-form research output.

    `raw_source_texts` is captured here because the baseline doesn't have
    per-claim citations — the grounding-freeform metric pools these texts
    as the evidence surface.
    """
    subquestion_id: str
    subquestion_text: str
    summary_text: str
    sources_consulted: list[str] = Field(default_factory=list)
    raw_source_texts: list[str] = Field(
        default_factory=list,
        description="Raw fetched-and-cleaned text the researcher actually saw. "
                    "Kept so grounding eval has something to check against.",
    )


class BaselineReport(BaseModel):
    """Free-form prose report assembled by the baseline publisher."""
    user_prompt: str
    final_report_text: str
    summaries: list[BaselineSummary]
    all_sources: list[str]


class BaselineRun(BaseModel):
    """The full record of one baseline execution."""
    user_prompt: str
    decomposition: Decomposition
    report: BaselineReport
    elapsed_seconds: float = 0.0
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
