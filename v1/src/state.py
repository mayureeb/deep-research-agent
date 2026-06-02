"""Structured types that flow between agents."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


Shape = Literal[
    "PRE_STRUCTURED",
    "CONTESTED",
    "SPARSE_EMERGING",
    "DISCOVERY",
    "GENERAL",
]


class ClassifiedShape(BaseModel):
    """Output of the shape classifier."""
    shape: Shape = "GENERAL"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    rationale: str = ""


class SubQuestion(BaseModel):
    """One concrete, source-answerable question carved out of the user prompt."""
    id: str
    question: str
    rationale: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    depends_on: list[str] = Field(default_factory=list)
    preferred_search: Literal["web_search", "search_papers", "either"] = "either"


class Decomposition(BaseModel):
    """The full set of sub-questions the planner produced for one prompt."""
    subquestions: list[SubQuestion]


class Finding(BaseModel):
    """One concrete claim with verbatim evidence supporting it."""
    subquestion_id: str
    claim: str
    evidence: str = Field(
        description="Verbatim or near-verbatim quote from the source"
    )
    source_url: str
    source_title: str = ""
    confidence: float = Field(ge=0.0, le=1.0)
    researcher_id: str = ""
    saved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    content_hash: str = ""
    domain: str = ""

    source_quality: float = Field(default=0.7, ge=0.0, le=1.0)

    agreement_score: float = Field(default=1.0, ge=0.0, le=1.0)
    combined_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    axis_disagreement: bool = False


class UncertaintyNote(BaseModel):
    """Researcher's explicit record that they searched but found no concrete evidence."""
    subquestion_id: str
    topic: str
    reason: str
    researcher_id: str = ""


class FindingsStore(BaseModel):
    """The shared blackboard for one research run. Append-only across the run."""
    findings: list[Finding] = Field(default_factory=list)
    uncertainty_notes: list[UncertaintyNote] = Field(default_factory=list)

    def add(self, f: Finding) -> None:
        self.findings.append(f)

    def add_uncertainty(self, n: UncertaintyNote) -> None:
        self.uncertainty_notes.append(n)

    def by_subquestion(self, sqid: str) -> list[Finding]:
        return [f for f in self.findings if f.subquestion_id == sqid]

    def all_sources(self) -> list[str]:
        return list({f.source_url for f in self.findings})

    def uncertainty_for(self, sqid: str) -> list[UncertaintyNote]:
        return [n for n in self.uncertainty_notes if n.subquestion_id == sqid]


class Contradiction(BaseModel):
    """One contradiction detected by the reconciler between two or more findings."""
    description: str = Field(
        description="One paragraph naming the disagreement, quoting both sides, "
                    "and listing source URLs."
    )
    finding_ids: list[int] = Field(
        description="Indices into FindingsStore.findings for the conflicting "
                    "findings. Must have at least 2 entries.",
    )
    severity: Literal["minor", "moderate", "major"] = "moderate"
    type: Literal[
        "factual", "methodological", "framing", "temporal", "other"
    ] = "factual"


class ReportClaim(BaseModel):
    """A claim in the final report, traced to specific findings."""
    claim: str
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_finding_indices: list[int] = Field(
        description="Indices into FindingsStore.findings that back this claim"
    )


class Report(BaseModel):
    """The writer's structured report."""
    user_prompt: str
    summary: str
    claims: list[ReportClaim]
    contradictions_surfaced: list[Contradiction] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


class CriticIssue(BaseModel):
    """One problem the critic found with one claim."""
    claim_index: int
    issue_type: Literal[
        "ungrounded",
        "overstated",
        "missing_citation",
        "source_mismatch",
    ]
    explanation: str


class CriticReport(BaseModel):
    """Output of one critic pass."""
    issues: list[CriticIssue]
    approved: bool


class ConditionCheck(BaseModel):
    """One condition extracted from the user prompt + whether the final
    output satisfies it."""
    condition: str
    status: Literal["satisfied", "partial", "not_satisfied", "not_applicable"]
    evidence: str = ""
    notes: str = ""


class FinalOutput(BaseModel):
    """The exporter's output: the post-critic Report rendered into the
    user-requested format, with a condition-satisfaction audit."""
    format_detected: str
    content: str
    conditions: list[ConditionCheck] = Field(default_factory=list)
    unmet_conditions: list[str] = Field(default_factory=list)
    notes: str = ""
    output_file_path: str | None = None


VerifyVerdict = Literal[
    "verified",
    "fabricated",
    "drifted",
    "unreachable",
]


class VerifyResult(BaseModel):
    """One re-fetch verdict for one finding."""
    finding_index: int
    verdict: VerifyVerdict
    detail: str = ""
    fetch_status: str = ""


class VerifyReport(BaseModel):
    """Output of one verifier run."""
    results: list[VerifyResult] = Field(default_factory=list)

    def by_index(self, finding_index: int) -> VerifyResult | None:
        for r in self.results:
            if r.finding_index == finding_index:
                return r
        return None

    @property
    def fabricated_indices(self) -> list[int]:
        return [r.finding_index for r in self.results if r.verdict == "fabricated"]

    @property
    def unreachable_indices(self) -> list[int]:
        return [r.finding_index for r in self.results if r.verdict == "unreachable"]


class StageStats(BaseModel):
    """Per-stage cost + latency rollup."""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    elapsed_seconds: float = 0.0


class ToolMix(BaseModel):
    """Tool-selection instrumentation across all researchers."""
    web_search: int = 0
    search_papers: int = 0
    fetch_url: int = 0
    save_finding: int = 0
    note_uncertainty: int = 0


class TraceStats(BaseModel):
    """TrACE telemetry rollup, populated only when cfg.trace_enabled."""
    steps_total: int = 0
    total_k: int = 0
    mean_k: float = 0.0
    mean_alpha: float = 0.0
    high_agreement_rate: float = 0.0
    per_tool_mean_k: dict[str, float] = Field(default_factory=dict)
    n_end_turn_overrides: int = 0
    n_save_finding_preferences: int = 0


class RunMetadata(BaseModel):
    """Reproducibility envelope around a ResearchRun."""
    commit_hash: str = ""
    branch: str = ""
    models: dict[str, str] = Field(default_factory=dict)
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    captured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class KAEResult(BaseModel):
    """Keypoint-Aligned Evaluation triplet (KSR / KCR / KOR)."""
    ksr: float
    kcr: float
    kor: float
    n_keypoints: int
    n_sources: int
    per_keypoint: list[dict] = Field(default_factory=list)


class Checklist(BaseModel):
    """ACE Stage-1 output: prompt-derived rubric the scorer evaluates against."""
    items: list[str] = Field(
        description="5-10 specific, measurable criteria a good research "
                    "report on this prompt should meet. Each is one sentence."
    )


class CheckItemScore(BaseModel):
    """ACE Stage-2 per-criterion verdict."""
    item: str
    met: bool
    score: float = Field(ge=0, le=1, description="Partial credit 0.0–1.0")
    reason: str


class ACEResult(BaseModel):
    """Adaptive Checklist Evaluation result."""
    checklist: Checklist
    item_scores: list[CheckItemScore]
    overall: float = Field(ge=0, le=1)
    notes: str = ""


class PlanJudgeResult(BaseModel):
    """Combined plan-quality judgment from one LLM call.

    Two dimensions, scored independently with partial credit (0.0–1.0):
      surface_coverage: do the sub-questions cover the prompt's surface area?
      intent_alignment: are the sub-questions about what the user asked?
    """
    surface_coverage: float = Field(ge=0, le=1)
    intent_alignment: float = Field(ge=0, le=1)
    missing_axes: list[str] = Field(
        default_factory=list,
        description="Surface-coverage gaps the judge identified.",
    )
    drift_examples: list[str] = Field(
        default_factory=list,
        description="Intent-alignment problems pairing sub-question id "
                    "with a brief description of how it drifted.",
    )
    reasoning: str = ""


class RunMetrics(BaseModel):
    """Per-run metrics shipped with the report."""
    total_subquestions: int
    subquestions_with_findings: int
    total_findings: int
    unique_sources: int
    revision_rounds: int
    final_grounding_rate: float = 0.0
    coverage_rate: float = 0.0
    elapsed_seconds: float = 0.0

    per_stage: dict[str, StageStats] = Field(default_factory=dict)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    researcher_latency_max: float = 0.0
    researcher_latency_avg: float = 0.0

    tool_mix: ToolMix = Field(default_factory=ToolMix)

    trace_stats: TraceStats = Field(default_factory=TraceStats)

    kae: KAEResult | None = None
    ace: ACEResult | None = None
    plan_judge: PlanJudgeResult | None = None


class TaxonomyNode(BaseModel):
    """One node in a hierarchical claim taxonomy."""
    label: str = Field(description="Short category/topic label")
    summary: str = Field(default="", description="One-sentence description of what this node groups")
    claims: list[ReportClaim] = Field(
        default_factory=list,
        description="Claims attached at this node (typically only at leaves)",
    )
    children: list["TaxonomyNode"] = Field(default_factory=list)

    def is_leaf(self) -> bool:
        return not self.children

    def all_leaf_claims(self) -> list[ReportClaim]:
        if self.is_leaf():
            return list(self.claims)
        out: list[ReportClaim] = []
        for ch in self.children:
            out.extend(ch.all_leaf_claims())
        return out

    def all_paths(self, prefix: list[str] | None = None) -> list[list[str]]:
        """Every root-to-leaf label path in the tree."""
        prefix = list(prefix or []) + [self.label]
        if self.is_leaf():
            return [prefix]
        out: list[list[str]] = []
        for ch in self.children:
            out.extend(ch.all_paths(prefix))
        return out

    def n_nodes(self) -> int:
        return 1 + sum(ch.n_nodes() for ch in self.children)


TaxonomyNode.model_rebuild()


class ResearchRun(BaseModel):
    """The full record of one research execution."""
    user_prompt: str
    shape: ClassifiedShape | None = None
    decomposition: Decomposition
    findings: FindingsStore
    report: Report | None = None
    critic_history: list[CriticReport] = Field(default_factory=list)
    verify_history: list[VerifyReport] = Field(default_factory=list)
    metadata: RunMetadata | None = None
    metrics: RunMetrics | None = None
    taxonomy: TaxonomyNode | None = None
    final_output: FinalOutput | None = None
    metrics_file_path: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class BaselineSummary(BaseModel):
    """One sub-question's worth of free-form research output."""
    subquestion_id: str
    subquestion_text: str
    summary_text: str
    sources_consulted: list[str] = Field(default_factory=list)
    raw_source_texts: list[str] = Field(
        default_factory=list,
        description="Raw fetched-and-cleaned text the researcher actually saw.",
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
