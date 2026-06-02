"""Smoke tests — verify imports and pure-Python logic without API calls.

Covers schema instantiation, FindingsStore + coverage logic,
reproducibility helpers, the fetch tool's typed-status / paywall /
RFC1918 / per-domain failure surface, contradiction schema, source-
quality + agreement helpers, the re-fetch verifier's quote heuristics,
the shape classifier's planner annex / writer prompt routing, the
metrics surface (RunMetadata / StageStats / ToolMix), the verifier
ablation flag, and fabrication-rate aggregation.
"""
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.state import (
    Contradiction, Decomposition, SubQuestion, Finding, FindingsStore,
    ReportClaim, Report, CriticIssue, CriticReport, RunMetrics, ResearchRun,
    VerifyResult, VerifyReport,
    ClassifiedShape,
    RunMetadata,
    StageStats, ToolMix,
)
from src.tools.fetch import (
    FetchResult, FetchContext, fetch,
    _validate_url, _matches_any, _PAYWALL_PHRASES, _BOT_PHRASES,
)
from src.config import Config
from src.agent.researcher import _meets_multi_signal_stop
from src.agent.orchestrator import (
    _compute_metrics, _compute_confidence_axes, _format_unresolved_caveat,
)
from src.agent.confidence import (
    compute_source_quality, compute_agreement, combine_axes,
    SQ_GOV_EDU, SQ_PEER_REVIEWED, SQ_NEWS, SQ_CURATED_TERTIARY,
    SQ_BLOG_QA, SQ_UNKNOWN,
    W_SELF, W_SOURCE_QUALITY, W_AGREEMENT, DISAGREEMENT_THRESHOLD,
)
from src.agent import refetch_verifier
from src.agent.refetch_verifier import (
    _verify_sync, _quote_present, _significant_token_overlap,
    DRIFT_OVERLAP_THRESHOLD,
)
from src.agent.critic import _merge_with_synthetic_fabrication_issues
from src.agent.shape_classifier import planner_annex
from src.agent.writer import (
    pick_writer_system, SYSTEM as _WRITER_BASE_SYSTEM,
)
from src.agent.orchestrator import _capture_run_metadata, _add_tool_counts
from src import llm as _llm_mod
from eval.metrics.coverage import coverage_rate
from eval.metrics.reproducibility import (
    claim_jaccard,
    subquestion_jaccard,
    claim_token_jaccard,
    subquestion_token_jaccard,
    finding_token_jaccard,
    _best_match_pair_score,
    _token_set,
)
from eval.metrics.fabrication_rate import (
    _approved_claim_indices, _empty_result, summarize_fabrication_results,
)
from eval.run_eval import _extract_step6_headline
from eval.v0_v1_compare import _adapt_v0_run_dict, _resolve_v0_python
from src.main import _disable_critic, _disable_verifier


def _cfg(**overrides) -> Config:
    """Minimal Config for tests. API keys are dummies — none of the smoke
    tests make network calls. `replace` is used so per-test overrides leave
    the frozen base instance untouched."""
    base = Config(anthropic_api_key="x", tavily_api_key="x")
    return replace(base, **overrides) if overrides else base


# ---------------------------------------------------------------------------
# Pre-existing v0 tests — kept as the regression floor.
# ---------------------------------------------------------------------------


def test_findings_store_basic():
    """FindingsStore: add, by_subquestion, all_sources work as advertised."""
    fs = FindingsStore()
    fs.add(Finding(
        subquestion_id="sq1", claim="X is Y", evidence="X is Y per source.",
        source_url="http://a.example", confidence=0.8,
    ))
    fs.add(Finding(
        subquestion_id="sq2", claim="A is B", evidence="A is B per source.",
        source_url="http://a.example", confidence=0.5,
    ))
    assert len(fs.findings) == 2
    assert len(fs.by_subquestion("sq1")) == 1
    assert len(fs.all_sources()) == 1


def test_coverage_rate():
    """coverage_rate: 1 of 2 sub-questions has a finding → 0.5."""
    fs = FindingsStore(findings=[
        Finding(subquestion_id="sq1", claim="c", evidence="e",
                source_url="u", confidence=0.5),
    ])
    rr = ResearchRun(
        user_prompt="p",
        decomposition=Decomposition(subquestions=[
            SubQuestion(id="sq1", question="q1"),
            SubQuestion(id="sq2", question="q2"),
        ]),
        findings=fs,
    )
    out = coverage_rate(rr)
    assert out["rate"] == 0.5
    assert out["missed"] == ["sq2"]


def test_jaccard_identical_runs():
    """claim_jaccard: two identical runs → mean_jaccard = 1.0."""
    rr_a = ResearchRun(
        user_prompt="p",
        decomposition=Decomposition(subquestions=[]),
        findings=FindingsStore(),
        report=Report(user_prompt="p", summary="s", claims=[
            ReportClaim(claim="X is Y", confidence=0.8, supporting_finding_indices=[]),
        ]),
    )
    rr_b = rr_a.model_copy(deep=True)
    out = claim_jaccard([rr_a, rr_b])
    assert out["mean_jaccard"] == 1.0


# ── token-Jaccard helpers ──


def test_token_set_lowercases_and_splits_on_whitespace():
    """_token_set: uses _normalize (lowercase + collapse) then splits."""
    assert _token_set("Hello   World  HELLO") == {"hello", "world"}
    assert _token_set("  ") == set()


def test_best_match_pair_score_identical_lists_returns_one():
    """Two identical token-set lists → best-match avg = 1.0."""
    a = [{"x", "y"}, {"a", "b", "c"}]
    assert _best_match_pair_score(a, a) == 1.0


def test_best_match_pair_score_disjoint_lists_returns_zero():
    """No item in A shares any token with any item in B → 0.0."""
    a = [{"alpha"}, {"beta"}]
    b = [{"gamma"}, {"delta"}]
    assert _best_match_pair_score(a, b) == 0.0


def test_best_match_pair_score_partial_overlap_is_graded():
    """Each A item has a partial-overlap counterpart in B — score should
    sit strictly between 0 and 1, demonstrating the metric is graded
    rather than binary."""
    a = [{"rag", "qa", "primary"}, {"latency", "cost"}]
    b = [{"rag", "qa", "principal"}, {"latency", "throughput"}]
    score = _best_match_pair_score(a, b)
    assert 0.0 < score < 1.0


def test_best_match_pair_score_both_empty_returns_one_degenerate():
    """Both runs produced nothing → degenerate match (1.0). The matching
    convention with the strict version: empty-vs-empty is a trivial
    agreement, not a 0.0 punishment."""
    assert _best_match_pair_score([], []) == 1.0


def test_best_match_pair_score_one_empty_returns_zero():
    """One run produced items, the other didn't → 0.0 (asymmetric
    failure to match)."""
    assert _best_match_pair_score([{"x"}], []) == 0.0
    assert _best_match_pair_score([], [{"x"}]) == 0.0


def test_subquestion_token_jaccard_survives_rephrasing():
    """The headline reason this metric exists: two re-runs of the same
    prompt produce semantically identical sub-questions phrased
    differently. Strict (whole-string) Jaccard floors at 0.0; token
    best-match should give a positive score."""
    rr_a = ResearchRun(
        user_prompt="p",
        decomposition=Decomposition(
            user_prompt="p", approach_summary="a",
            subquestions=[
                SubQuestion(id="sq1",
                    question="What are the primary architectural approaches to RAG?"),
                SubQuestion(id="sq2",
                    question="Compare RAG accuracy on QA benchmarks."),
            ],
        ),
        findings=FindingsStore(),
    )
    rr_b = ResearchRun(
        user_prompt="p",
        decomposition=Decomposition(
            user_prompt="p", approach_summary="a",
            subquestions=[
                SubQuestion(id="sq1",
                    question="What are the principal architectural approaches for RAG?"),
                SubQuestion(id="sq2",
                    question="Compare RAG accuracy across QA benchmarks."),
            ],
        ),
        findings=FindingsStore(),
    )
    strict = subquestion_jaccard([rr_a, rr_b])
    token = subquestion_token_jaccard([rr_a, rr_b])
    # Strict floors at 0.0 — no whole-string matches the rephrasing.
    assert strict["mean_jaccard"] == 0.0
    # Token best-match recovers most of the similarity.
    assert token["mean_jaccard"] > 0.6


def test_claim_token_jaccard_returns_zero_with_only_one_run():
    """Need ≥2 runs to have a pair — single run returns 0.0 to match
    the strict version's contract (and so reproducibility can't be
    silently inferred from a single run)."""
    rr = ResearchRun(
        user_prompt="p",
        decomposition=Decomposition(subquestions=[]),
        findings=FindingsStore(),
        report=Report(user_prompt="p", summary="s", claims=[
            ReportClaim(claim="x", confidence=0.5, supporting_finding_indices=[]),
        ]),
    )
    out = claim_token_jaccard([rr])
    assert out["mean_jaccard"] == 0.0
    assert out["pairs"] == []


def test_finding_token_jaccard_operates_on_findings_not_claims():
    """finding_token_jaccard reads researcher findings, not writer claims —
    populate findings only (no Report) and assert the metric still
    returns a positive score for paraphrased researcher findings.
    Sanity check: an empty Report should NOT affect the answer."""
    rr_a = ResearchRun(
        user_prompt="p",
        decomposition=Decomposition(subquestions=[]),
        findings=FindingsStore(findings=[
            Finding(subquestion_id="sq1",
                    claim="hybrid retrieval combines BM25 and dense embeddings",
                    evidence="from page",
                    source_url="https://example.com/a",
                    confidence=0.8),
        ]),
    )
    rr_b = ResearchRun(
        user_prompt="p",
        decomposition=Decomposition(subquestions=[]),
        findings=FindingsStore(findings=[
            Finding(subquestion_id="sq1",
                    claim="hybrid search uses BM25 sparse plus dense vectors",
                    evidence="from another page",
                    source_url="https://example.com/b",
                    confidence=0.8),
        ]),
    )
    out = finding_token_jaccard([rr_a, rr_b])
    assert 0.0 < out["mean_jaccard"] < 1.0
    assert len(out["pairs"]) == 1


def test_finding_token_jaccard_returns_zero_with_one_run():
    """Same single-run guard contract as the other token-Jaccards."""
    rr = ResearchRun(
        user_prompt="p",
        decomposition=Decomposition(subquestions=[]),
        findings=FindingsStore(findings=[
            Finding(subquestion_id="sq1", claim="x", evidence="y",
                    source_url="https://example.com", confidence=0.5),
        ]),
    )
    out = finding_token_jaccard([rr])
    assert out["mean_jaccard"] == 0.0
    assert out["pairs"] == []


def test_claim_token_jaccard_pair_structure():
    """Returns the same {mean_jaccard, pairs:[{i,j,jaccard}, ...]} shape
    as claim_jaccard so finding1_step0 can swap in trivially."""
    def _mk(claim_text):
        return ResearchRun(
            user_prompt="p",
            decomposition=Decomposition(subquestions=[]),
            findings=FindingsStore(),
            report=Report(user_prompt="p", summary="s", claims=[
                ReportClaim(claim=claim_text, confidence=0.5,
                            supporting_finding_indices=[]),
            ]),
        )
    runs = [_mk("hybrid retrieval combines BM25 and dense embeddings"),
            _mk("hybrid search uses BM25 sparse plus dense vectors"),
            _mk("entirely unrelated topic about cryptography")]
    out = claim_token_jaccard(runs)
    # 3 runs → C(3,2)=3 pairs.
    assert len(out["pairs"]) == 3
    assert {(p["i"], p["j"]) for p in out["pairs"]} == {(0, 1), (0, 2), (1, 2)}
    # The (0,1) pair (paraphrases) should score higher than (0,2)
    # (unrelated claim) — sanity check the metric is doing the right thing.
    pair_01 = next(p for p in out["pairs"] if (p["i"], p["j"]) == (0, 1))
    pair_02 = next(p for p in out["pairs"] if (p["i"], p["j"]) == (0, 2))
    assert pair_01["jaccard"] > pair_02["jaccard"]


# ---------------------------------------------------------------------------
# Fetch-tool surface tests. None of these touch the network.
# ---------------------------------------------------------------------------


def test_fetch_result_is_usable():
    """FetchResult.is_usable: True only when status == 'ok'."""
    assert FetchResult(url="x", status="ok", text="body").is_usable
    assert not FetchResult(url="x", status="paywall").is_usable
    assert not FetchResult(url="x", status="blocked").is_usable
    assert not FetchResult(url="x", status="not_found").is_usable
    assert not FetchResult(url="x", status="rate_limited").is_usable
    assert not FetchResult(url="x", status="non_html").is_usable
    assert not FetchResult(url="x", status="timeout").is_usable
    assert not FetchResult(url="x", status="error").is_usable


def test_fetch_context_three_strikes():
    """FetchContext: domain promotes to blocked after threshold failures.

    Default threshold is 3. After three failures on the same domain,
    is_blocked() returns True; subsequent fetches to that domain should
    short-circuit without a network call.
    """
    ctx = FetchContext()
    url = "https://flaky.example.com/page"
    assert not ctx.is_blocked(url)
    ctx.record_failure(url)
    ctx.record_failure(url)
    assert not ctx.is_blocked(url)  # under threshold
    ctx.record_failure(url)
    assert ctx.is_blocked(url)      # at threshold
    # Subdomains track separately on purpose — see _extract_domain doc.
    other = "https://other.example.com/page"
    assert not ctx.is_blocked(other)


def test_fetch_context_failure_count_isolation():
    """Failures on one domain don't leak into another's count."""
    ctx = FetchContext()
    a = "https://a.example/p"
    b = "https://b.example/p"
    ctx.record_failure(a)
    ctx.record_failure(a)
    assert ctx.domain_failures.get("a.example") == 2
    assert ctx.domain_failures.get("b.example", 0) == 0


def test_fetch_validate_url_rejects_bad_schemes():
    """_validate_url: only http/https allowed before GET."""
    assert _validate_url("file:///etc/passwd") is not None
    assert _validate_url("ftp://example.com/x") is not None
    assert _validate_url("javascript:alert(1)") is not None
    # http and https are fine.
    assert _validate_url("http://example.com/x") is None
    assert _validate_url("https://example.com/x") is None


def test_fetch_validate_url_rejects_rfc1918():
    """_validate_url: refuses literal RFC1918 / loopback / link-local IPs."""
    # RFC1918 (private) ranges.
    assert _validate_url("http://10.0.0.1/admin") is not None
    assert _validate_url("http://172.16.0.1/x") is not None
    assert _validate_url("http://192.168.1.1/x") is not None
    # Loopback.
    assert _validate_url("http://127.0.0.1/x") is not None
    # Link-local.
    assert _validate_url("http://169.254.169.254/latest/meta-data/") is not None
    # Public IP literal is fine.
    assert _validate_url("http://8.8.8.8/") is None


def test_fetch_validate_url_passes_hostnames():
    """_validate_url: hostnames (not IP literals) are passed through.

    We deliberately don't DNS-resolve in pre-check — see fetch.py docstring.
    """
    assert _validate_url("https://example.com/x") is None
    assert _validate_url("https://blog.example.com/x") is None


def test_fetch_validate_url_rejects_garbage():
    """_validate_url: empty / non-string / no-host inputs are rejected."""
    assert _validate_url("") is not None
    assert _validate_url(None) is not None  # type: ignore[arg-type]
    # No host → reject. urlparse on a bare path yields no hostname.
    assert _validate_url("http://") is not None


def test_fetch_paywall_phrases_match():
    """_matches_any: paywall keyword set fires on canonical paywall text."""
    body = (
        "this article is for subscribers — please subscribe to read the rest."
    ).lower()
    assert _matches_any(body, _PAYWALL_PHRASES)


def test_fetch_bot_detection_phrases_match():
    """_matches_any: bot-detection keyword set fires on Cloudflare-style text."""
    body = "verify you are human by completing the action below.".lower()
    assert _matches_any(body, _BOT_PHRASES)


def test_fetch_with_blocked_domain_short_circuits():
    """fetch(): blocked domain returns 'blocked' without making a network call.

    Pre-loads a FetchContext past the threshold for a domain, then calls
    fetch. The result must be status=blocked AND must not have hit the
    network (we verify by ensuring no exception about network).
    """
    ctx = FetchContext()
    url = "https://blocked.example/x"
    for _ in range(ctx.failure_threshold):
        ctx.record_failure(url)
    result = fetch(url, ctx=ctx)
    assert result.status == "blocked"
    assert not result.is_usable
    assert "domain blocked" in (result.error or "").lower()


def test_fetch_invalid_url_returns_error():
    """fetch(): invalid URL is rejected before any network call."""
    result = fetch("file:///etc/passwd")
    assert result.status == "error"
    assert not result.is_usable
    assert result.error is not None


# ---------------------------------------------------------------------------
# Reconciler / contradiction surface tests.
# ---------------------------------------------------------------------------


def test_contradiction_schema_basic():
    """Contradiction model accepts the four documented fields and validates
    severity / type Literal values."""
    c = Contradiction(
        description="Source A says 30%, Source B says 70%.",
        finding_ids=[0, 1],
        severity="major",
        type="factual",
    )
    assert c.severity == "major"
    assert c.type == "factual"
    # Defaults: severity=moderate, type=factual.
    c2 = Contradiction(description="d", finding_ids=[1, 2])
    assert c2.severity == "moderate"
    assert c2.type == "factual"
    # Bad severity → ValidationError.
    with pytest.raises(ValidationError):
        Contradiction(description="d", finding_ids=[1, 2], severity="catastrophic")
    # Bad type → ValidationError.
    with pytest.raises(ValidationError):
        Contradiction(description="d", finding_ids=[1, 2], type="vibes")


def _mk_finding(url: str, conf: float = 0.7, sqid: str = "sq1") -> Finding:
    """Build a Finding with the bare minimum fields for stop-signal tests."""
    return Finding(
        subquestion_id=sqid, claim="c", evidence="e",
        source_url=url, confidence=conf,
    )


def test_meets_multi_signal_stop_count_floor():
    """Under min_findings_to_stop → blocker is 'need_more_findings'."""
    cfg = _cfg(researcher_min_findings_to_stop=3)
    findings = [_mk_finding("http://a")]  # only 1 of required 3
    met, blocker = _meets_multi_signal_stop(findings, cfg)
    assert not met
    assert blocker == "need_more_findings"


def test_meets_multi_signal_stop_diversity_floor():
    """Enough findings but all from the same source → 'need_more_sources'."""
    cfg = _cfg(
        researcher_min_findings_to_stop=3,
        researcher_min_unique_sources=2,
        researcher_min_avg_confidence=0.0,
    )
    findings = [_mk_finding("http://a") for _ in range(3)]
    met, blocker = _meets_multi_signal_stop(findings, cfg)
    assert not met
    assert blocker == "need_more_sources"


def test_meets_multi_signal_stop_confidence_floor():
    """Enough findings, enough sources, but avg confidence too low →
    'low_confidence'."""
    cfg = _cfg(
        researcher_min_findings_to_stop=3,
        researcher_min_unique_sources=2,
        researcher_min_avg_confidence=0.55,
    )
    findings = [
        _mk_finding("http://a", conf=0.3),
        _mk_finding("http://b", conf=0.4),
        _mk_finding("http://c", conf=0.3),
    ]
    met, blocker = _meets_multi_signal_stop(findings, cfg)
    assert not met
    assert blocker == "low_confidence"


def test_meets_multi_signal_stop_passes():
    """All three signals clear → met=True, blocker is empty."""
    cfg = _cfg(
        researcher_min_findings_to_stop=3,
        researcher_min_unique_sources=2,
        researcher_min_avg_confidence=0.55,
    )
    findings = [
        _mk_finding("http://a", conf=0.7),
        _mk_finding("http://b", conf=0.7),
        _mk_finding("http://c", conf=0.7),
    ]
    met, blocker = _meets_multi_signal_stop(findings, cfg)
    assert met
    assert blocker == ""


def _mk_run(num_claims: int) -> tuple[Decomposition, FindingsStore, Report]:
    """Build a trivial decomposition + findings + report for grounding-rate
    tests. Decomposition has 1 sub-question; findings list is empty; the
    report has `num_claims` claims with empty supporting indices (the
    grounding-rate formula doesn't read them — it reads CriticReport.issues
    and report.claims length)."""
    decomp = Decomposition(subquestions=[SubQuestion(id="sq1", question="q")])
    findings = FindingsStore()
    report = Report(
        user_prompt="p", summary="s",
        claims=[
            ReportClaim(claim=f"c{i}", confidence=0.5, supporting_finding_indices=[])
            for i in range(num_claims)
        ],
    )
    return decomp, findings, report


def test_grounding_rate_no_critic_returns_zero():
    """No critic round → 0.0 (we have no signal)."""
    decomp, findings, report = _mk_run(num_claims=3)
    metrics = _compute_metrics(decomp, findings, [], report, started=0.0)
    assert metrics.final_grounding_rate == 0.0


def test_grounding_rate_approved_returns_one():
    """Final critic approved → 1.0 regardless of issue count."""
    decomp, findings, report = _mk_run(num_claims=4)
    cr = CriticReport(issues=[], approved=True)
    metrics = _compute_metrics(decomp, findings, [cr], report, started=0.0)
    assert metrics.final_grounding_rate == 1.0


def test_grounding_rate_partial_uses_correct_formula():
    """4 claims, 2 distinct claim indices flagged → 2/4 = 0.5.

    Defends the v1 (2.4) fix against regression to the v0 1-n/(n+1)
    formula (which would have returned 1/3 ≈ 0.333 for 2 issues).
    Multiple issues on the same claim count once (set semantics).
    """
    decomp, findings, report = _mk_run(num_claims=4)
    cr = CriticReport(
        issues=[
            CriticIssue(claim_index=0, issue_type="ungrounded", explanation="x"),
            CriticIssue(claim_index=2, issue_type="overstated", explanation="x"),
            # Duplicate flag on claim 0 — should still count as ONE flagged claim.
            CriticIssue(claim_index=0, issue_type="source_mismatch", explanation="x"),
        ],
        approved=False,
    )
    metrics = _compute_metrics(decomp, findings, [cr], report, started=0.0)
    # claims flagged: {0, 2} → 2 of 4 approved → 2/4 = 0.5.
    assert metrics.final_grounding_rate == 0.5


def test_grounding_rate_zero_claims_returns_zero():
    """Defensive: report with no claims → 0.0 (avoids div-by-zero)."""
    decomp, findings, report = _mk_run(num_claims=0)
    cr = CriticReport(issues=[], approved=True)
    metrics = _compute_metrics(decomp, findings, [cr], report, started=0.0)
    assert metrics.final_grounding_rate == 0.0


# ---------------------------------------------------------------------------
# Confidence-axes surface tests.
# ---------------------------------------------------------------------------


def test_source_quality_gov_edu():
    """compute_source_quality: .gov / .edu hosts (TLD or subdomain) → 1.0."""
    assert compute_source_quality("https://www.cdc.gov/page") == SQ_GOV_EDU
    assert compute_source_quality("https://stanford.edu/") == SQ_GOV_EDU
    assert compute_source_quality("https://nasa.gov/x") == SQ_GOV_EDU
    assert compute_source_quality("https://cs.mit.edu/x") == SQ_GOV_EDU


def test_source_quality_peer_reviewed():
    """compute_source_quality: known peer-reviewed venues → 1.0."""
    assert compute_source_quality("https://arxiv.org/abs/1234") == SQ_PEER_REVIEWED
    assert compute_source_quality("https://www.nature.com/articles/x") == SQ_PEER_REVIEWED
    assert compute_source_quality("https://semanticscholar.org/paper/x") == SQ_PEER_REVIEWED
    assert compute_source_quality("https://pubmed.ncbi.nlm.nih.gov/12345") == SQ_PEER_REVIEWED


def test_source_quality_news():
    """compute_source_quality: reputable news outlets → 0.85."""
    assert compute_source_quality("https://www.nytimes.com/2024/x.html") == SQ_NEWS
    assert compute_source_quality("https://reuters.com/article/x") == SQ_NEWS
    assert compute_source_quality("https://www.bbc.com/news/x") == SQ_NEWS


def test_source_quality_curated_tertiary():
    """compute_source_quality: Wikipedia → 0.75 (curated but tertiary)."""
    assert compute_source_quality("https://en.wikipedia.org/wiki/X") == SQ_CURATED_TERTIARY


def test_source_quality_blog_and_qa():
    """compute_source_quality: blog hosts + Q&A → 0.5."""
    assert compute_source_quality("https://medium.com/@user/post") == SQ_BLOG_QA
    assert compute_source_quality("https://substack.com/p/x") == SQ_BLOG_QA
    assert compute_source_quality("https://stackoverflow.com/q/123") == SQ_BLOG_QA
    assert compute_source_quality("https://www.reddit.com/r/x") == SQ_BLOG_QA


def test_source_quality_unknown_default():
    """compute_source_quality: anything else → SQ_UNKNOWN (0.7)."""
    assert compute_source_quality("https://random-corp-blog.io/post") == SQ_UNKNOWN
    assert compute_source_quality("https://example.com/x") == SQ_UNKNOWN


def test_source_quality_unparseable():
    """compute_source_quality: garbage URL → SQ_UNKNOWN (defensive default).

    Defends save_finding from raising — a misformatted URL must not lose
    the finding."""
    assert compute_source_quality("") == SQ_UNKNOWN
    assert compute_source_quality("not-a-url") == SQ_UNKNOWN


def test_source_quality_subdomain_match_no_false_positive():
    """compute_source_quality: 'evilnature.com' must NOT match 'nature.com'.

    Defends the dot-anchored suffix match in _domain_matches_any. Without
    the dot, "evilnature.com".endswith("nature.com") is True — the test
    catches that regression."""
    assert compute_source_quality("https://evilnature.com/x") == SQ_UNKNOWN
    # But the legitimate subdomain DOES match.
    assert compute_source_quality("https://blog.nature.com/x") == SQ_PEER_REVIEWED


def test_compute_agreement_solo_finding_returns_one():
    """Solo finding (no peers) → agreement = 1.0 (neutral, no signal).

    The writer should not be punished for using a finding that simply has
    no peers — source_quality is what keeps a solo blog from getting a
    free pass."""
    f = Finding(subquestion_id="sq1", claim="c", evidence="e",
                source_url="http://a", confidence=0.5)
    assert compute_agreement(0, "sq1", [f], contradictions=[]) == 1.0


def test_compute_agreement_no_contradictions_full_agreement():
    """3 same-subq peers, no contradictions → agreement = 1.0."""
    fs = [
        Finding(subquestion_id="sq1", claim=f"c{i}", evidence="e",
                source_url=f"http://{i}", confidence=0.5)
        for i in range(3)
    ]
    assert compute_agreement(0, "sq1", fs, contradictions=[]) == 1.0


def test_compute_agreement_with_contradiction_lowers_score():
    """Index 0 paired with index 1 in a Contradiction → agreement < 1.0.

    Defends the supporting/total formula. With 2 same-subq peers and 1 of
    them in a contradiction, supporting=1, total=2 → 0.5."""
    fs = [
        Finding(subquestion_id="sq1", claim="A says X", evidence="e",
                source_url="http://a", confidence=0.5),
        Finding(subquestion_id="sq1", claim="B says not-X", evidence="e",
                source_url="http://b", confidence=0.5),
        Finding(subquestion_id="sq1", claim="C says X too", evidence="e",
                source_url="http://c", confidence=0.5),
    ]
    contradictions = [Contradiction(
        description="A vs B disagree on X.",
        finding_ids=[0, 1], severity="moderate", type="factual",
    )]
    # finding 0 has 2 peers (1, 2). 1 is contradicting → supporting=1, total=2 → 0.5.
    assert compute_agreement(0, "sq1", fs, contradictions) == 0.5
    # finding 2 has 2 peers (0, 1). Neither in a contradiction with 2 → 1.0.
    assert compute_agreement(2, "sq1", fs, contradictions) == 1.0


def test_combine_axes_weights_sum_to_one():
    """The three weights must sum to 1.0 — otherwise combined ∉ [0, 1]
    even with all-1.0 inputs. Pinning the constants here means a silent
    edit fails loudly."""
    assert abs((W_SELF + W_SOURCE_QUALITY + W_AGREEMENT) - 1.0) < 1e-9


def test_combine_axes_known_value():
    """Hand-computed expected combined for self=0.8, sq=0.9, ag=0.6.

    expected = 0.40*0.8 + 0.35*0.9 + 0.25*0.6 = 0.32 + 0.315 + 0.15 = 0.785.
    Spread = max - min = 0.9 - 0.6 = 0.3 < 0.35 → no disagreement flag."""
    combined, disagreement = combine_axes(0.8, 0.9, 0.6)
    assert abs(combined - 0.785) < 1e-9
    assert disagreement is False


def test_combine_axes_disagreement_fires_on_wide_spread():
    """self=0.9, sq=0.4, ag=0.5 → spread = 0.5 ≥ threshold → flag True.

    Defends the canonical "researcher claims high confidence on a low-
    quality source no one corroborates" case."""
    combined, disagreement = combine_axes(0.9, 0.4, 0.5)
    assert disagreement is True
    # combined: 0.40*0.9 + 0.35*0.4 + 0.25*0.5 = 0.36 + 0.14 + 0.125 = 0.625.
    assert abs(combined - 0.625) < 1e-9


def test_combine_axes_clips_out_of_range_inputs():
    """Defensive: inputs outside [0,1] (e.g. legacy serialized run) get
    clipped, no exception."""
    combined, _ = combine_axes(self_reported=2.0, source_quality=-1.0, agreement=0.5)
    # All inputs clip → 1.0, 0.0, 0.5 → 0.40 + 0 + 0.125 = 0.525
    assert abs(combined - 0.525) < 1e-9


def test_finding_serializes_v1_provenance_fields():
    """Finding round-trips with the new v1 fields.

    Defends serialize/deserialize symmetry — if one of the new fields was
    missing a default, model_validate_json on a partial JSON would raise."""
    f = Finding(
        subquestion_id="sq1", claim="c", evidence="e",
        source_url="http://nature.com/x", confidence=0.8,
        content_hash="abc123", domain="nature.com",
        source_quality=1.0, agreement_score=0.7,
        combined_confidence=0.85, axis_disagreement=True,
    )
    raw = f.model_dump_json()
    g = Finding.model_validate_json(raw)
    assert g.content_hash == "abc123"
    assert g.domain == "nature.com"
    assert g.source_quality == 1.0
    assert g.agreement_score == 0.7
    assert g.combined_confidence == 0.85
    assert g.axis_disagreement is True


def test_finding_defaults_for_legacy_runs():
    """Finding with only v0 fields populated still validates — the new v1
    fields take their defaults so old serialized runs keep loading."""
    legacy_json = (
        '{"subquestion_id":"sq1","claim":"c","evidence":"e",'
        '"source_url":"http://x","confidence":0.5}'
    )
    f = Finding.model_validate_json(legacy_json)
    assert f.content_hash == ""
    assert f.domain == ""
    assert f.source_quality == SQ_UNKNOWN
    assert f.agreement_score == 1.0
    assert f.combined_confidence == 0.0
    assert f.axis_disagreement is False


def test_compute_confidence_axes_populates_in_place():
    """End-to-end: orchestrator helper sweeps a FindingsStore in place,
    populating agreement_score + combined_confidence + axis_disagreement
    on every finding using compute_agreement + combine_axes."""
    fs = FindingsStore(findings=[
        Finding(subquestion_id="sq1", claim="A says X",
                evidence="e", source_url="http://nature.com/x",
                confidence=0.9, source_quality=1.0),
        Finding(subquestion_id="sq1", claim="B says not-X",
                evidence="e", source_url="http://medium.com/x",
                confidence=0.4, source_quality=0.5),
    ])
    contradictions = [Contradiction(
        description="A vs B.", finding_ids=[0, 1],
        severity="moderate", type="factual",
    )]
    _compute_confidence_axes(fs, contradictions)
    # finding 0: peer 1 is contradicting → agreement = 0/1 = 0.0.
    # combined_0 = 0.40*0.9 + 0.35*1.0 + 0.25*0.0 = 0.36 + 0.35 + 0 = 0.71
    # spread = 1.0 - 0.0 = 1.0 → disagreement = True.
    assert abs(fs.findings[0].agreement_score - 0.0) < 1e-9
    assert abs(fs.findings[0].combined_confidence - 0.71) < 1e-9
    assert fs.findings[0].axis_disagreement is True


# ---------------------------------------------------------------------------
# Re-fetch verifier + caveats-fallback surface tests.
# ---------------------------------------------------------------------------


def test_verify_report_accessors_split_verdicts():
    """fabricated_indices and unreachable_indices return only their type."""
    vr = VerifyReport(results=[
        VerifyResult(finding_index=0, verdict="verified"),
        VerifyResult(finding_index=1, verdict="fabricated"),
        VerifyResult(finding_index=2, verdict="unreachable", fetch_status="paywall"),
        VerifyResult(finding_index=3, verdict="drifted"),
        VerifyResult(finding_index=4, verdict="fabricated"),
    ])
    assert vr.fabricated_indices == [1, 4]
    assert vr.unreachable_indices == [2]
    assert vr.by_index(3).verdict == "drifted"
    assert vr.by_index(99) is None


def test_verify_report_rejects_invalid_verdict():
    """Verdict Literal validation fires on a typo'd verdict."""
    with pytest.raises(ValidationError):
        VerifyResult(finding_index=0, verdict="hallucinated")  # type: ignore[arg-type]


def test_quote_present_exact_substring():
    """_quote_present: verbatim quote in body → True."""
    body = "Lorem ipsum. The cat sat on the mat. Dolor sit amet."
    assert _quote_present("The cat sat on the mat", body)


def test_quote_present_case_and_whitespace_insensitive():
    """_quote_present normalizes case + whitespace before matching."""
    body = "Lorem ipsum.   The CAT  sat\non the MAT.\n\nDolor."
    assert _quote_present("the cat sat on the mat", body)


def test_quote_present_long_quote_prefix_match():
    """Long quote where the body has the first 60 chars but truncates the
    tail still matches (handles editorial cleanup at end of quote)."""
    quote = "x" * 100
    body = "lead-in " + ("x" * 70) + " then different text"
    assert _quote_present(quote, body)


def test_quote_present_returns_false_for_unrelated():
    """No substring match + no prefix match → False."""
    assert not _quote_present("the dog barked loudly", "completely unrelated body text")


def test_quote_present_empty_quote_is_false():
    """Defensive: empty quote → False (don't return True for vacuous match)."""
    assert not _quote_present("", "any body")


def test_significant_token_overlap_high_when_words_match():
    """Many shared significant tokens → high overlap (≥ DRIFT_OVERLAP_THRESHOLD).

    Used to validate the 'drifted' branch — page rewritten but same topic
    keeps most significant tokens."""
    quote = "transformer attention scales quadratically with sequence length"
    body = (
        "The transformer architecture suffers because attention complexity "
        "scales with the square of sequence length, making long sequence "
        "modeling expensive."
    )
    assert _significant_token_overlap(quote, body) >= DRIFT_OVERLAP_THRESHOLD


def test_significant_token_overlap_low_for_unrelated():
    """No shared significant tokens → ~0.0 (well below the drift threshold).

    The fabrication path: researcher made up a quote that doesn't match
    the page's topic at all."""
    quote = "the quick brown fox jumps over lazy dogs"
    body = "completely orthogonal text about transformer attention complexity"
    assert _significant_token_overlap(quote, body) < DRIFT_OVERLAP_THRESHOLD


# Helper: monkeypatch the fetch_url symbol the verifier uses.
def _patch_fetch(monkeypatch, result: FetchResult):
    """Replace refetch_verifier.fetch_url with a stub returning `result`."""
    monkeypatch.setattr(refetch_verifier, "fetch_url", lambda url, **kw: result)


def _mk_v_finding(claim: str = "c", evidence: str = "e",
                   url: str = "http://x") -> Finding:
    """Bare-minimum Finding for verify_sync tests."""
    return Finding(
        subquestion_id="sq1", claim=claim, evidence=evidence,
        source_url=url, confidence=0.5,
    )


def test_verify_sync_paywall_marks_unreachable(monkeypatch):
    """Paywall fetch result → 'unreachable', NOT 'fabricated'."""
    _patch_fetch(monkeypatch, FetchResult(
        url="x", status="paywall",
        error="page body contains paywall markers",
    ))
    result = _verify_sync(0, _mk_v_finding(), fetch_ctx=None)
    assert result.verdict == "unreachable"
    assert result.fetch_status == "paywall"
    # Detail names the underlying status so post-hoc inspection can
    # tell paywall from 404 from timeout.
    assert "paywall" in result.detail.lower()


def test_verify_sync_ok_with_quote_match_marks_verified(monkeypatch):
    """Live page contains the verbatim quote → 'verified'."""
    _patch_fetch(monkeypatch, FetchResult(
        url="x", status="ok",
        text="Background prose. The cat sat on the mat. Conclusion.",
    ))
    f = _mk_v_finding(evidence="The cat sat on the mat")
    result = _verify_sync(0, f, fetch_ctx=None)
    assert result.verdict == "verified"
    assert result.fetch_status == "ok"


def test_verify_sync_ok_with_drift_marks_drifted(monkeypatch):
    """Live page lacks the verbatim quote BUT high token overlap → 'drifted'.

    Defends the page-edited-after-fetch case from being misclassified as
    fabrication."""
    _patch_fetch(monkeypatch, FetchResult(
        url="x", status="ok",
        text=(
            "The transformer architecture suffers because attention complexity "
            "scales with the square of sequence length, making long sequence "
            "modeling expensive — quadratic memory cost."
        ),
    ))
    f = _mk_v_finding(
        evidence="transformer attention scales quadratically with sequence length",
    )
    result = _verify_sync(0, f, fetch_ctx=None)
    assert result.verdict == "drifted"


def test_verify_sync_ok_with_no_overlap_marks_fabricated(monkeypatch):
    """Live page is reachable but neither contains the quote nor shares
    significant tokens → 'fabricated' (the headline failure mode)."""
    _patch_fetch(monkeypatch, FetchResult(
        url="x", status="ok",
        text="Completely unrelated content about cooking pasta.",
    ))
    f = _mk_v_finding(
        evidence="transformer attention scales quadratically with sequence length",
    )
    result = _verify_sync(0, f, fetch_ctx=None)
    assert result.verdict == "fabricated"


def test_format_unresolved_caveat_contains_warning_and_per_issue_lines():
    """The caveats fallback string starts with 'WARNING' and lists every
    unresolved issue with its claim index + type + explanation.

    This is the v1 (4.5) anti-silent-papering-over property — defending
    it via test means a future edit can't accidentally drop the WARNING
    prefix."""
    issues = [
        CriticIssue(claim_index=0, issue_type="ungrounded", explanation="why0"),
        CriticIssue(claim_index=2, issue_type="overstated", explanation="why2"),
    ]
    out = _format_unresolved_caveat(issues)
    assert out.startswith("WARNING")
    assert "Claim #0" in out and "ungrounded" in out and "why0" in out
    assert "Claim #2" in out and "overstated" in out and "why2" in out


def test_critic_synthetic_injection_flags_fabricated_claim():
    """Defense-in-depth: a claim citing a fabricated finding gets an
    auto-injected 'ungrounded' issue even if the LLM critic missed it."""
    report = Report(
        user_prompt="p", summary="s",
        claims=[
            ReportClaim(claim="c0", confidence=0.9, supporting_finding_indices=[0]),
            ReportClaim(claim="c1", confidence=0.5, supporting_finding_indices=[1]),
        ],
    )
    verify_report = VerifyReport(results=[
        VerifyResult(finding_index=0, verdict="fabricated"),
        VerifyResult(finding_index=1, verdict="verified"),
    ])
    # LLM critic missed claim 0's fabrication.
    llm_report = CriticReport(issues=[], approved=True)
    merged = _merge_with_synthetic_fabrication_issues(llm_report, report, verify_report)
    assert merged.approved is False
    assert any(
        i.claim_index == 0 and i.issue_type == "ungrounded"
        for i in merged.issues
    )
    # Claim 1 (verified) doesn't get an injection.
    assert not any(i.claim_index == 1 for i in merged.issues)


def test_critic_synthetic_injection_preserves_llm_explanation():
    """If the LLM critic already flagged a fabricated claim ungrounded,
    we keep its (more informative) explanation rather than overwriting."""
    report = Report(
        user_prompt="p", summary="s",
        claims=[ReportClaim(claim="c0", confidence=0.9, supporting_finding_indices=[0])],
    )
    verify_report = VerifyReport(results=[
        VerifyResult(finding_index=0, verdict="fabricated"),
    ])
    llm_explanation = "claim makes a stronger assertion than the cited evidence"
    llm_report = CriticReport(
        issues=[CriticIssue(claim_index=0, issue_type="ungrounded",
                            explanation=llm_explanation)],
        approved=False,
    )
    merged = _merge_with_synthetic_fabrication_issues(llm_report, report, verify_report)
    # Exactly one ungrounded issue for claim 0; explanation is the LLM's.
    ungrounded_for_0 = [
        i for i in merged.issues
        if i.claim_index == 0 and i.issue_type == "ungrounded"
    ]
    assert len(ungrounded_for_0) == 1
    assert ungrounded_for_0[0].explanation == llm_explanation


def test_critic_no_injection_without_verify_report_keys():
    """Empty fabricated set → merge is a no-op."""
    report = Report(
        user_prompt="p", summary="s",
        claims=[ReportClaim(claim="c", confidence=0.5, supporting_finding_indices=[0])],
    )
    verify_report = VerifyReport(results=[
        VerifyResult(finding_index=0, verdict="verified"),
    ])
    llm_report = CriticReport(issues=[], approved=True)
    merged = _merge_with_synthetic_fabrication_issues(llm_report, report, verify_report)
    assert merged.approved is True
    assert merged.issues == []


# ---------------------------------------------------------------------------
# Shape-classifier surface tests.
# ---------------------------------------------------------------------------


def test_classified_shape_validates_literal():
    """ClassifiedShape rejects unknown shape strings; accepts the five."""
    for s in ("PRE_STRUCTURED", "CONTESTED", "SPARSE_EMERGING", "DISCOVERY", "GENERAL"):
        cs = ClassifiedShape(shape=s, confidence=0.7, rationale="r")
        assert cs.shape == s
    with pytest.raises(ValidationError):
        ClassifiedShape(shape="META", confidence=0.5, rationale="r")  # type: ignore[arg-type]


def test_classified_shape_defaults():
    """Empty-construct → GENERAL/0.5/empty rationale (safe defaults so a
    missing classifier output round-trips cleanly)."""
    cs = ClassifiedShape()
    assert cs.shape == "GENERAL"
    assert cs.confidence == 0.5
    assert cs.rationale == ""


def test_subquestion_v1_fields_default_for_legacy_runs():
    """SubQuestion with only v0 fields populated still validates — new
    fields take their defaults so old serialized runs keep loading.

    Defends backward-compat. v0 decompositions exist on disk and the v1
    schema must round-trip them."""
    legacy_json = '{"id":"sq1","question":"q"}'
    sq = SubQuestion.model_validate_json(legacy_json)
    assert sq.confidence == 0.5
    assert sq.depends_on == []
    assert sq.preferred_search == "either"


def test_subquestion_preferred_search_validates_literal():
    """SubQuestion.preferred_search rejects unknown values."""
    with pytest.raises(ValidationError):
        SubQuestion(id="sq1", question="q", preferred_search="oracle")  # type: ignore[arg-type]


def test_planner_annex_per_shape():
    """planner_annex returns shape-tagged guidance; empty for GENERAL/None."""
    assert planner_annex(None) == ""
    assert planner_annex(ClassifiedShape(shape="GENERAL")) == ""
    pre = planner_annex(ClassifiedShape(shape="PRE_STRUCTURED"))
    cont = planner_annex(ClassifiedShape(shape="CONTESTED"))
    sparse = planner_annex(ClassifiedShape(shape="SPARSE_EMERGING"))
    disc = planner_annex(ClassifiedShape(shape="DISCOVERY"))
    # Each annex is non-empty and names its shape (so a misrouting is
    # immediately obvious in the prompt).
    assert "PRE_STRUCTURED" in pre and len(pre) > 50
    assert "CONTESTED" in cont and len(cont) > 50
    assert "SPARSE_EMERGING" in sparse and len(sparse) > 50
    assert "DISCOVERY" in disc and len(disc) > 50
    # The four annexes are pairwise distinct (defends the
    # shape-specificity property).
    assert len({pre, cont, sparse, disc}) == 4


def test_pick_writer_system_per_shape():
    """pick_writer_system returns the base SYSTEM for None/GENERAL and
    base+annex for the other shapes; the four annexed prompts are
    pairwise distinct."""
    assert pick_writer_system(None) == _WRITER_BASE_SYSTEM
    assert pick_writer_system(ClassifiedShape(shape="GENERAL")) == _WRITER_BASE_SYSTEM
    pre = pick_writer_system(ClassifiedShape(shape="PRE_STRUCTURED"))
    cont = pick_writer_system(ClassifiedShape(shape="CONTESTED"))
    sparse = pick_writer_system(ClassifiedShape(shape="SPARSE_EMERGING"))
    disc = pick_writer_system(ClassifiedShape(shape="DISCOVERY"))
    # All four annexed prompts START with the base SYSTEM (so the writer's
    # baseline contract holds across shapes).
    for p in (pre, cont, sparse, disc):
        assert p.startswith(_WRITER_BASE_SYSTEM)
        assert p != _WRITER_BASE_SYSTEM  # but each adds the shape annex
    assert "PRE_STRUCTURED" in pre
    assert "CONTESTED" in cont
    assert "SPARSE_EMERGING" in sparse
    assert "DISCOVERY" in disc
    assert len({pre, cont, sparse, disc}) == 4


# ---------------------------------------------------------------------------
# Metrics surface tests.
# ---------------------------------------------------------------------------


# --- cost / token / latency tracking ---------------------------


def test_track_stage_tags_calls_and_aggregates():
    """track_stage tags appended LLMCalls; aggregate_calls_by_stage rolls
    them up with calls / token totals."""
    _llm_mod.reset_call_log()
    fake_resp = type("R", (), {"usage": type("U", (), {
        "input_tokens": 100, "output_tokens": 50,
    })()})()
    with _llm_mod.track_stage("decompose"):
        _llm_mod._record_call("claude-haiku-4-5", fake_resp)
        _llm_mod._record_call("claude-haiku-4-5", fake_resp)
    with _llm_mod.track_stage("writer"):
        _llm_mod._record_call("claude-sonnet-4-6", fake_resp)
    rollup = _llm_mod.aggregate_calls_by_stage()
    assert "decompose" in rollup and "writer" in rollup
    assert rollup["decompose"].calls == 2
    assert rollup["decompose"].input_tokens == 200
    assert rollup["decompose"].output_tokens == 100
    # Cost: haiku is $1/M input + $5/M output → 200 * 1e-6 + 100 * 5e-6 = 0.0007.
    assert abs(rollup["decompose"].cost_usd - 0.0007) < 1e-9
    assert rollup["writer"].calls == 1
    # Sonnet is $3/M input + $15/M output → 100 * 3e-6 + 50 * 15e-6 = 0.00105.
    assert abs(rollup["writer"].cost_usd - 0.00105) < 1e-9


def test_track_stage_restores_outer_on_exit():
    """Nested track_stage restores the OUTER stage on exit (not 'unknown').

    Defends the reentrancy contract — without it, a self-check stage
    nested inside writer would un-tag subsequent writer calls as
    'unknown'."""
    _llm_mod.reset_call_log()
    assert _llm_mod._current_stage == "unknown"
    with _llm_mod.track_stage("writer"):
        assert _llm_mod._current_stage == "writer"
        with _llm_mod.track_stage("inner_stage"):
            assert _llm_mod._current_stage == "inner_stage"
        # Outer restored, NOT collapsed to "unknown".
        assert _llm_mod._current_stage == "writer"
    assert _llm_mod._current_stage == "unknown"


def test_unknown_model_zeros_cost_without_raising():
    """An unrecognized model id → cost 0.0, no exception. The first miss
    emits a stderr warning; subsequent misses on the same id are silent."""
    _llm_mod.reset_call_log()
    fake_resp = type("R", (), {"usage": type("U", (), {
        "input_tokens": 1000, "output_tokens": 500,
    })()})()
    with _llm_mod.track_stage("unknown_stage"):
        _llm_mod._record_call("claude-future-99", fake_resp)
    rollup = _llm_mod.aggregate_calls_by_stage()
    assert rollup["unknown_stage"].cost_usd == 0.0
    assert rollup["unknown_stage"].input_tokens == 1000


def test_stage_stats_round_trip():
    """StageStats with all five fields round-trips through JSON."""
    s = StageStats(calls=3, input_tokens=200, output_tokens=80,
                    cost_usd=0.0123, elapsed_seconds=4.5)
    s2 = StageStats.model_validate_json(s.model_dump_json())
    assert s2.calls == 3 and s2.elapsed_seconds == 4.5


# --- 6.5 tool-mix accumulator --------------------------------------------


def test_add_tool_counts_accumulates_known_buckets():
    """_add_tool_counts merges known-tool counts into a ToolMix, dropping
    unknown names silently (defensive against future tool additions)."""
    mix = ToolMix()
    _add_tool_counts(mix, {"web_search": 3, "fetch_url": 2, "save_finding": 1})
    _add_tool_counts(mix, {"web_search": 4, "search_papers": 5})
    assert mix.web_search == 7
    assert mix.fetch_url == 2
    assert mix.search_papers == 5
    assert mix.save_finding == 1
    # Unknown bucket silently dropped (does not raise).
    _add_tool_counts(mix, {"future_tool": 99})
    assert not hasattr(mix, "future_tool")


# --- 6.4 run metadata ----------------------------------------------------


def test_capture_run_metadata_redacts_api_keys():
    """_capture_run_metadata snapshots Config but redacts api_key fields.

    Defends against a future SECRET_API_KEY field accidentally leaking
    into a saved run JSON — the redaction is done by name suffix
    (anything ending in 'api_key')."""
    cfg = Config(
        anthropic_api_key="real-secret-key", tavily_api_key="another-secret",
    )
    md = _capture_run_metadata(cfg)
    assert md.config_snapshot["anthropic_api_key"] == "<set>"
    assert md.config_snapshot["tavily_api_key"] == "<set>"
    # Non-secret fields are preserved verbatim.
    assert md.config_snapshot["max_revisions"] == cfg.max_revisions
    assert md.config_snapshot["parallel_researchers"] == cfg.parallel_researchers
    # Models dict is broken out separately for grep-ability.
    assert md.models["researcher"] == cfg.researcher_model
    assert md.models["writer"] == cfg.writer_model
    assert md.models["critic"] == cfg.critic_model
    assert md.models["judge"] == cfg.judge_model


def test_capture_run_metadata_redacts_unset_keys():
    """An empty api key still gets redacted, with '<unset>' marker so the
    distinction is visible."""
    cfg = Config(anthropic_api_key="", tavily_api_key="")
    md = _capture_run_metadata(cfg)
    assert md.config_snapshot["anthropic_api_key"] == "<unset>"
    assert md.config_snapshot["tavily_api_key"] == "<unset>"


def test_run_metadata_round_trip():
    """RunMetadata round-trips through JSON with all four fields."""
    md = RunMetadata(
        commit_hash="abc123", branch="main",
        models={"writer": "claude-sonnet-4-6"},
        config_snapshot={"max_revisions": 2, "anthropic_api_key": "<set>"},
    )
    md2 = RunMetadata.model_validate_json(md.model_dump_json())
    assert md2.commit_hash == "abc123" and md2.branch == "main"
    assert md2.models["writer"] == "claude-sonnet-4-6"
    assert md2.config_snapshot["max_revisions"] == 2


# --- ResearchRun + RunMetrics fields -------------


def test_research_run_carries_metadata():
    """ResearchRun round-trips with metadata populated; legacy runs without metadata also validate."""
    rr = ResearchRun(
        user_prompt="p",
        decomposition=Decomposition(subquestions=[]),
        findings=FindingsStore(),
        metadata=RunMetadata(commit_hash="abc"),
    )
    rr2 = ResearchRun.model_validate_json(rr.model_dump_json())
    assert rr2.metadata is not None
    assert rr2.metadata.commit_hash == "abc"
    # Legacy decode (no metadata) still works.
    legacy = (
        '{"user_prompt":"p","decomposition":{"subquestions":[]},'
        '"findings":{"findings":[],"uncertainty_notes":[]}}'
    )
    rr3 = ResearchRun.model_validate_json(legacy)
    assert rr3.metadata is None


def test_run_metrics_legacy_fields_default():
    """RunMetrics deserialized from a legacy JSON keeps loading; all
    new fields take their defaults so old run records replay."""
    legacy = (
        '{"total_subquestions":3,"subquestions_with_findings":2,'
        '"total_findings":5,"unique_sources":4,"revision_rounds":1}'
    )
    m = RunMetrics.model_validate_json(legacy)
    assert m.per_stage == {}
    assert m.total_input_tokens == 0
    assert m.total_cost_usd == 0.0
    assert isinstance(m.tool_mix, ToolMix)
    assert m.researcher_latency_max == 0.0


def test_tool_mix_round_trip_and_defaults():
    """ToolMix defaults to all-zero counters and round-trips with the five tool buckets."""
    tm = ToolMix()
    assert tm.web_search == 0 and tm.note_uncertainty == 0
    tm2 = ToolMix(web_search=3, search_papers=5, fetch_url=10,
                  save_finding=4, note_uncertainty=1)
    tm3 = ToolMix.model_validate_json(tm2.model_dump_json())
    assert tm3.web_search == 3 and tm3.note_uncertainty == 1


# --- 6.6 plan reproducibility metric -------------------------------------


def test_subquestion_jaccard_identical_decompositions():
    """Two identical decompositions → mean_jaccard = 1.0.

    The parallel of claim_jaccard for measuring planner-level reproducibility."""
    decomp = Decomposition(subquestions=[
        SubQuestion(id="sq1", question="What does X mean?"),
        SubQuestion(id="sq2", question="How does Y differ from X?"),
    ])
    rr_a = ResearchRun(
        user_prompt="p", decomposition=decomp, findings=FindingsStore(),
    )
    rr_b = rr_a.model_copy(deep=True)
    out = subquestion_jaccard([rr_a, rr_b])
    assert out["mean_jaccard"] == 1.0


def test_subquestion_jaccard_disjoint_decompositions():
    """Two completely different decompositions → mean_jaccard = 0.0."""
    rr_a = ResearchRun(
        user_prompt="p",
        decomposition=Decomposition(subquestions=[
            SubQuestion(id="sq1", question="alpha question"),
            SubQuestion(id="sq2", question="beta question"),
        ]),
        findings=FindingsStore(),
    )
    rr_b = ResearchRun(
        user_prompt="p",
        decomposition=Decomposition(subquestions=[
            SubQuestion(id="sq1", question="completely orthogonal angle"),
            SubQuestion(id="sq2", question="another unrelated facet"),
        ]),
        findings=FindingsStore(),
    )
    out = subquestion_jaccard([rr_a, rr_b])
    assert out["mean_jaccard"] == 0.0


def test_subquestion_jaccard_single_run_returns_zero():
    """One run → no pairs → 0.0 (NOT 1.0; we genuinely can't measure)."""
    rr = ResearchRun(
        user_prompt="p",
        decomposition=Decomposition(subquestions=[
            SubQuestion(id="sq1", question="q"),
        ]),
        findings=FindingsStore(),
    )
    out = subquestion_jaccard([rr])
    assert out["mean_jaccard"] == 0.0
    assert out["pairs"] == []


# ---------------------------------------------------------------------------
# Verifier ablation + v0-vs-v1 fabrication-rate surface tests.
# ---------------------------------------------------------------------------


def test_config_verifier_enabled_default_true():
    """cfg.verifier_enabled defaults to True. Flipping it via
    dataclasses.replace yields a NEW frozen Config; original isn't mutated."""
    cfg = _cfg()
    assert cfg.verifier_enabled is True
    cfg_off = replace(cfg, verifier_enabled=False)
    assert cfg_off.verifier_enabled is False
    # Frozen invariant: original Config unchanged.
    assert cfg.verifier_enabled is True


def test_disable_verifier_helper_returns_new_config():
    """main._disable_verifier returns a new Config with verifier_enabled=False
    and leaves all other fields intact (max_revisions especially —
    composable with --no-critic)."""
    cfg = _cfg(max_revisions=2)
    cfg_off = _disable_verifier(cfg)
    assert cfg_off.verifier_enabled is False
    assert cfg_off.max_revisions == 2  # not collapsed by the verifier ablation
    # Original untouched.
    assert cfg.verifier_enabled is True


def test_disable_critic_and_verifier_compose():
    """--no-critic + --no-verifier is the closest in-process v0
    approximation. Both flags must compose without one reverting the
    other (frozen-dataclass replace returns a new instance each time)."""
    cfg = _cfg()
    cfg_off = _disable_verifier(_disable_critic(cfg))
    assert cfg_off.max_revisions == 0
    assert cfg_off.verifier_enabled is False


# --- 7.4 fabrication_rate metric (pure-function paths) -------------------


def _mk_findings(n: int) -> FindingsStore:
    """Build N synthetic findings for fabrication_rate tests."""
    return FindingsStore(findings=[
        Finding(subquestion_id="sq1", claim=f"c{i}",
                evidence=f"e{i}", source_url=f"http://src{i}",
                confidence=0.5)
        for i in range(n)
    ])


def _mk_report(citations: list[list[int]]) -> Report:
    """Build a Report whose claim i cites citations[i] (list of finding indices)."""
    return Report(
        user_prompt="p", summary="s",
        claims=[
            ReportClaim(claim=f"c{i}", confidence=0.5,
                        supporting_finding_indices=cites)
            for i, cites in enumerate(citations)
        ],
    )


def test_approved_claim_indices_no_critic_returns_all():
    """No critic round → every claim is approved (we have no signal to
    drop any). Used by fabrication_rate so v0-no-critic / baseline
    ablations still produce a measurable denominator."""
    report = _mk_report([[0], [1], [2]])
    assert _approved_claim_indices(report, []) == [0, 1, 2]


def test_approved_claim_indices_critic_approved_returns_all():
    """Critic approved=True → all claims approved regardless of
    issues list (which should be empty when approved=True)."""
    report = _mk_report([[0], [1]])
    cr = CriticReport(issues=[], approved=True)
    assert _approved_claim_indices(report, [cr]) == [0, 1]


def test_approved_claim_indices_dedupes_multiple_issues_per_claim():
    """Multiple critic issues on the same claim → claim counted once
    (set semantics). Defends consistency with _compute_metrics's
    grounding rate denominator."""
    report = _mk_report([[0], [1], [2]])
    cr = CriticReport(
        issues=[
            CriticIssue(claim_index=0, issue_type="ungrounded", explanation="x"),
            CriticIssue(claim_index=0, issue_type="overstated", explanation="x"),
            CriticIssue(claim_index=2, issue_type="missing_citation", explanation="x"),
        ],
        approved=False,
    )
    # Claim 1 not flagged → still approved. Claims 0 and 2 dropped.
    assert _approved_claim_indices(report, [cr]) == [1]


def test_summarize_fabrication_weighted_rate():
    """summarize: weighted_rate is Σfab / Σapproved. Macro is mean of
    per-prompt rates. Both reported because they answer different
    questions about whether one big prompt dominates the suite."""
    rows = [
        # 4 approved, 2 fab → rate 50%
        {"rate": 0.5, "approved_total": 4, "approved_with_fabricated": 2,
         "drift_rate": 0.0},
        # 6 approved, 0 fab → rate 0%
        {"rate": 0.0, "approved_total": 6, "approved_with_fabricated": 0,
         "drift_rate": 0.0},
    ]
    s = summarize_fabrication_results(rows)
    # Σfab=2, Σapproved=10 → weighted = 0.20
    assert abs(s["weighted_rate"] - 0.2) < 1e-9
    # Macro = mean(0.5, 0.0) = 0.25 — different from weighted on
    # purpose (the small-N prompt is over-represented).
    assert abs(s["macro_rate"] - 0.25) < 1e-9
    assert s["n_prompts"] == 2
    assert s["approved_total_sum"] == 10
    assert s["approved_with_fabricated_sum"] == 2


def test_summarize_fabrication_empty_rows_zero():
    """Empty input → all-zero summary (NOT NaN / NOT crash).

    Defends the edge case where every prompt's run failed."""
    s = summarize_fabrication_results([])
    assert s["n_prompts"] == 0
    assert s["weighted_rate"] == 0.0
    assert s["macro_rate"] == 0.0


def test_empty_result_shape():
    """_empty_result has all the keys callers expect; rate=0.0, sentinel
    VerifyReport. Defends against "no signal" paths returning a partial
    dict that downstream rendering chokes on."""
    er = _empty_result()
    assert er["rate"] == 0.0
    assert er["approved_total"] == 0
    assert er["approved_with_fabricated"] == 0
    assert er["fabricated_finding_indices"] == []
    assert er["drift_rate"] == 0.0
    assert er["verifier_was_rerun"] is False
    assert isinstance(er["verify_report"], VerifyReport)


# --- 7.3 v0→v1 adapter ---------------------------------------------------


def test_adapt_v0_run_dict_strips_contradictions_surfaced():
    """v0's Report.contradictions_surfaced: list[str] would fail v1's
    list[Contradiction] validation. The adapter empties the field in
    place; loading then succeeds with v1's default ([])."""
    raw = {
        "user_prompt": "p",
        "decomposition": {"subquestions": []},
        "findings": {"findings": [], "uncertainty_notes": []},
        "report": {
            "user_prompt": "p",
            "summary": "s",
            "claims": [],
            # v0 shape: prose strings, NOT Contradiction objects.
            "contradictions_surfaced": ["A and B disagree on X."],
            "caveats": [],
        },
    }
    _adapt_v0_run_dict(raw)
    assert raw["report"]["contradictions_surfaced"] == []
    # And the adapted dict now validates as a v1 ResearchRun.
    rr = ResearchRun.model_validate(raw)
    assert rr.report is not None
    assert rr.report.contradictions_surfaced == []


def test_adapt_v0_run_dict_no_op_when_field_absent():
    """Adapter is safe to call on a dict without report (catastrophic
    upstream v0 failure case) — does not raise."""
    raw = {"user_prompt": "p", "decomposition": {"subquestions": []},
           "findings": {"findings": [], "uncertainty_notes": []}}
    _adapt_v0_run_dict(raw)  # should not raise
    assert "report" not in raw


def test_resolve_v0_python_explicit_arg_wins(tmp_path):
    """Explicit --v0-python beats both venv detection and PATH fallback.

    Defends the precedence order documented in _resolve_v0_python."""
    chosen = _resolve_v0_python(str(tmp_path), v0_python_arg="/explicit/python")
    assert chosen == "/explicit/python"


def test_resolve_v0_python_venv_default(tmp_path):
    """When --v0-python is absent but <v0_dir>/.venv/bin/python exists,
    that's the default (not 'python' from PATH)."""
    venv = tmp_path / ".venv" / "bin"
    venv.mkdir(parents=True)
    fake_py = venv / "python"
    fake_py.write_text("#!/bin/sh\necho stub")
    fake_py.chmod(0o755)
    chosen = _resolve_v0_python(str(tmp_path), v0_python_arg=None)
    assert chosen == str(fake_py.resolve())


def test_resolve_v0_python_falls_back_to_path(tmp_path):
    """When neither flag nor venv interpreter is available, fall back to
    'python' (resolved at subprocess time via PATH)."""
    chosen = _resolve_v0_python(str(tmp_path), v0_python_arg=None)
    assert chosen == "python"


# --- 7.1 run_eval Step-6-headline extraction ------------------------------


def test_extract_step6_headline_pulls_all_keys():
    """_extract_step6_headline produces a flat dict of all expected
    Step-6 surface numbers. Defends the run_eval row schema — adding
    a Step-6 field requires landing it here too."""
    rr = ResearchRun(
        user_prompt="p",
        shape=ClassifiedShape(shape="CONTESTED", confidence=0.8, rationale="r"),
        decomposition=Decomposition(subquestions=[]),
        findings=FindingsStore(),
        metadata=RunMetadata(commit_hash="deadbeef", branch="main"),
        metrics=RunMetrics(
            total_subquestions=3, subquestions_with_findings=2,
            total_findings=8, unique_sources=5, revision_rounds=1,
            total_input_tokens=1000, total_output_tokens=500,
            total_cost_usd=0.012,
            researcher_latency_max=4.5, researcher_latency_avg=3.1,
            tool_mix=ToolMix(web_search=10, search_papers=5,
                             fetch_url=8, save_finding=4,
                             note_uncertainty=1),
        ),
    )
    out = _extract_step6_headline(rr)
    assert out["shape"] == "CONTESTED"
    assert out["cost_usd_total"] == 0.012
    assert out["tokens_input_total"] == 1000
    assert out["tokens_output_total"] == 500
    assert out["researcher_latency_max_s"] == 4.5
    assert out["researcher_latency_avg_s"] == 3.1
    # tool_mix_total = 10+5+8+4+1 = 28 across the five tool buckets.
    assert out["tool_mix_total"] == 28
    assert "tool_mix_scout" not in out
    assert out["commit_hash"] == "deadbeef"
    assert out["branch"] == "main"


def test_extract_step6_headline_handles_legacy_run():
    """Legacy run with no shape / metadata — extraction returns sensible
    defaults rather than crashing."""
    rr = ResearchRun(
        user_prompt="p",
        decomposition=Decomposition(subquestions=[]),
        findings=FindingsStore(),
        metrics=RunMetrics(
            total_subquestions=1, subquestions_with_findings=0,
            total_findings=0, unique_sources=0, revision_rounds=0,
        ),
    )
    out = _extract_step6_headline(rr)
    assert out["shape"] is None
    assert out["commit_hash"] == ""
    assert out["tool_mix_total"] == 0


# ---------------------------------------------------------------------------
# Paper-inspired ports — TaxonomyNode + KAEResult + ACEResult schema
# round-trips, plus pure-Python hierarchy diagnostics. No LLM calls.
# ---------------------------------------------------------------------------


def test_taxonomy_node_round_trip():
    """TaxonomyNode with depth-2 tree (root + 2 children, leaves carry
    claims) round-trips through model_dump_json / model_validate_json."""
    from src.state import TaxonomyNode
    c1 = ReportClaim(claim="A", confidence=0.7, supporting_finding_indices=[0])
    c2 = ReportClaim(claim="B", confidence=0.8, supporting_finding_indices=[1])
    c3 = ReportClaim(claim="C", confidence=0.6, supporting_finding_indices=[2])
    tree = TaxonomyNode(
        label="root", summary="top",
        children=[
            TaxonomyNode(label="left", claims=[c1, c2]),
            TaxonomyNode(label="right", claims=[c3]),
        ],
    )
    js = tree.model_dump_json()
    decoded = TaxonomyNode.model_validate_json(js)
    assert decoded.label == "root"
    assert len(decoded.children) == 2
    assert decoded.children[0].claims[0].claim == "A"
    assert decoded.children[1].claims[0].claim == "C"


def test_taxonomy_helpers():
    """is_leaf / all_leaf_claims / all_paths / n_nodes on a small tree."""
    from src.state import TaxonomyNode
    c1 = ReportClaim(claim="A", confidence=0.7, supporting_finding_indices=[0])
    c2 = ReportClaim(claim="B", confidence=0.8, supporting_finding_indices=[1])
    c3 = ReportClaim(claim="C", confidence=0.6, supporting_finding_indices=[2])
    tree = TaxonomyNode(
        label="root",
        children=[
            TaxonomyNode(label="left", claims=[c1, c2]),
            TaxonomyNode(label="right", claims=[c3]),
        ],
    )
    assert not tree.is_leaf()
    assert tree.children[0].is_leaf()
    leaf_claims = tree.all_leaf_claims()
    assert {c.claim for c in leaf_claims} == {"A", "B", "C"}
    paths = tree.all_paths()
    assert sorted(paths) == [["root", "left"], ["root", "right"]]
    assert tree.n_nodes() == 3  # root + 2 children


def test_research_run_carries_taxonomy():
    """ResearchRun with taxonomy field round-trips. Taxonomy is None on
    runs that didn't go through the v2 eval (the orchestrator never sets it)."""
    from src.state import TaxonomyNode
    rr = ResearchRun(
        user_prompt="p",
        decomposition=Decomposition(subquestions=[]),
        findings=FindingsStore(),
        taxonomy=TaxonomyNode(
            label="root",
            children=[TaxonomyNode(label="leaf", claims=[])],
        ),
    )
    js = rr.model_dump_json()
    decoded = ResearchRun.model_validate_json(js)
    assert decoded.taxonomy is not None
    assert decoded.taxonomy.label == "root"
    assert decoded.taxonomy.children[0].label == "leaf"


def test_run_metrics_carries_kae_and_ace():
    """RunMetrics with kae + ace populated round-trips."""
    from src.state import ACEResult, Checklist, CheckItemScore, KAEResult
    metrics = RunMetrics(
        total_subquestions=1, subquestions_with_findings=1,
        total_findings=2, unique_sources=2, revision_rounds=0,
        kae=KAEResult(ksr=0.6, kcr=0.1, kor=0.3, n_keypoints=10, n_sources=2),
        ace=ACEResult(
            checklist=Checklist(items=["a", "b"]),
            item_scores=[
                CheckItemScore(item="a", met=True, score=1.0, reason="ok"),
                CheckItemScore(item="b", met=False, score=0.3, reason="partial"),
            ],
            overall=0.65, notes="1/2 fully met",
        ),
    )
    js = metrics.model_dump_json()
    decoded = RunMetrics.model_validate_json(js)
    assert decoded.kae.ksr == 0.6
    assert decoded.kae.n_keypoints == 10
    assert decoded.ace.overall == 0.65
    assert len(decoded.ace.item_scores) == 2


def test_hierarchy_summary_flags_over_segmented():
    """over_segmented flag is True when most leaves are singletons OR
    avg_per_leaf < 1.5; False on a balanced tree."""
    from eval.metrics.hierarchy import hierarchy_summary
    from src.state import TaxonomyNode

    def _claim(s):
        return ReportClaim(claim=s, confidence=0.5, supporting_finding_indices=[0])

    # 5 singleton leaves + 1 multi-claim leaf → over-segmented.
    fragmented = TaxonomyNode(
        label="root",
        children=[
            TaxonomyNode(label=f"l{i}", claims=[_claim(f"c{i}")])
            for i in range(5)
        ] + [
            TaxonomyNode(label="big", claims=[_claim("a"), _claim("b"), _claim("c")]),
        ],
    )
    summary = hierarchy_summary(fragmented)
    assert summary["singleton_frac"] > 0.5
    assert summary["over_segmented"] is True

    # Balanced tree: 2 leaves × 3 claims each. Not over-segmented.
    balanced = TaxonomyNode(
        label="root",
        children=[
            TaxonomyNode(label="a", claims=[_claim("c1"), _claim("c2"), _claim("c3")]),
            TaxonomyNode(label="b", claims=[_claim("c4"), _claim("c5"), _claim("c6")]),
        ],
    )
    summary = hierarchy_summary(balanced)
    assert summary["singleton_frac"] == 0.0
    assert summary["avg_per_leaf"] == 3.0
    assert summary["over_segmented"] is False


def test_consolidation_score_empty_tree():
    """Defensive: None tree → zeroed dict, no crash."""
    from eval.metrics.hierarchy import consolidation_score
    out = consolidation_score(None)
    assert out == {"avg_per_leaf": 0.0, "singleton_frac": 0.0,
                   "n_leaves": 0, "n_claims": 0}


def test_path_diversity_linear_vs_balanced():
    """Linear chain → n_paths=1; balanced 2-leaf tree → n_paths=2."""
    from eval.metrics.hierarchy import path_diversity
    from src.state import TaxonomyNode

    linear = TaxonomyNode(
        label="root",
        children=[TaxonomyNode(
            label="mid",
            children=[TaxonomyNode(label="leaf", claims=[])],
        )],
    )
    out = path_diversity(linear)
    assert out["n_paths"] == 1
    assert out["max_depth"] == 3

    balanced = TaxonomyNode(
        label="root",
        children=[
            TaxonomyNode(label="a", claims=[]),
            TaxonomyNode(label="b", claims=[]),
        ],
    )
    out = path_diversity(balanced)
    assert out["n_paths"] == 2
    assert out["max_depth"] == 2


def test_kae_judge_model_default_is_haiku():
    """KAE has its own cfg.kae_judge_model defaulting to a cheaper model
    than the eval-wide cfg.judge_model."""
    cfg = Config(anthropic_api_key="x", tavily_api_key="x")
    assert cfg.kae_judge_model == "claude-haiku-4-5"
    # And the eval-wide judge_model is still Opus (ACE / grounding / judge
    # all key on it; only KAE was downgraded).
    assert cfg.judge_model == "claude-opus-4-7"


def test_kae_uses_kae_judge_model_not_judge_model():
    """eval/metrics/kae.py must call chat_json with cfg.kae_judge_model,
    not cfg.judge_model. Otherwise we'd silently regress to Opus billing
    on every KAE call."""
    import inspect as _inspect
    from eval.metrics import kae as kae_mod
    src = _inspect.getsource(kae_mod.kae_score)
    # Both LLM-call sites in kae_score should reference the new field.
    assert "cfg.kae_judge_model" in src
    assert "cfg.judge_model" not in src


def test_capture_run_metadata_includes_kae_judge_model():
    """RunMetadata.models carries a 'kae_judge' entry so the saved-run snapshot records both judge models."""
    cfg = Config(
        anthropic_api_key="x", tavily_api_key="x",
        kae_judge_model="claude-haiku-4-5",
    )
    md = _capture_run_metadata(cfg)
    assert md.models["judge"] == cfg.judge_model
    assert md.models["kae_judge"] == "claude-haiku-4-5"


def test_metric_stats_basic_numeric_aggregation():
    """Three runs with numeric values → correct mean / stdev / min / max
    rolled up per metric. Uses population stdev (statistics.pstdev)."""
    from eval.metrics.reproducibility import metric_stats
    per_run = [
        {"grounding_rate": 0.8, "f1": 0.7, "cost_usd_total": 0.10},
        {"grounding_rate": 0.6, "f1": 0.5, "cost_usd_total": 0.20},
        {"grounding_rate": 1.0, "f1": 0.9, "cost_usd_total": 0.30},
    ]
    stats = metric_stats(per_run)
    # grounding: mean = 0.8, pstdev = sqrt(((0.8-0.8)^2 + (0.6-0.8)^2 + (1.0-0.8)^2)/3)
    #         = sqrt(0.08/3) ≈ 0.1633
    assert abs(stats["grounding_rate"]["mean"] - 0.8) < 1e-9
    assert abs(stats["grounding_rate"]["stdev"] - 0.16329931618554522) < 1e-9
    assert stats["grounding_rate"]["min"] == 0.6
    assert stats["grounding_rate"]["max"] == 1.0
    assert stats["grounding_rate"]["n"] == 3
    assert stats["cost_usd_total"]["min"] == 0.10
    assert stats["cost_usd_total"]["max"] == 0.30


def test_metric_stats_skips_all_none_keys():
    """Keys where every value across runs is None are omitted from the
    stats dict — no row for 'we never measured this'."""
    from eval.metrics.reproducibility import metric_stats
    per_run = [
        {"grounding_rate": 0.8, "kae_ksr": None, "ace_overall": None},
        {"grounding_rate": 0.6, "kae_ksr": None, "ace_overall": None},
    ]
    stats = metric_stats(per_run)
    assert "grounding_rate" in stats
    assert "kae_ksr" not in stats
    assert "ace_overall" not in stats


def test_metric_stats_partial_none_aggregates_present_only():
    """When a metric is None on some runs (e.g., KAE skipped on one of
    three), the rollup uses only the present values and reports n
    correctly."""
    from eval.metrics.reproducibility import metric_stats
    per_run = [
        {"grounding_rate": 0.8, "kae_ksr": 0.7},
        {"grounding_rate": 0.6, "kae_ksr": None},
        {"grounding_rate": 1.0, "kae_ksr": 0.9},
    ]
    stats = metric_stats(per_run)
    assert stats["kae_ksr"]["n"] == 2
    assert abs(stats["kae_ksr"]["mean"] - 0.8) < 1e-9
    # grounding still has all 3
    assert stats["grounding_rate"]["n"] == 3


def test_metric_stats_boolean_fraction_true():
    """Booleans aggregate as fraction_true with n, NOT mean/stdev.
    Important: bool is a subclass of int in Python, so the check must
    happen before the numeric path."""
    from eval.metrics.reproducibility import metric_stats
    per_run = [
        {"hierarchy_over_segmented": True},
        {"hierarchy_over_segmented": False},
        {"hierarchy_over_segmented": True},
        {"hierarchy_over_segmented": True},
    ]
    stats = metric_stats(per_run)
    assert stats["hierarchy_over_segmented"]["fraction_true"] == 0.75
    assert stats["hierarchy_over_segmented"]["n"] == 4
    assert "mean" not in stats["hierarchy_over_segmented"]


def test_metric_stats_single_run_yields_zero_stdev():
    """N=1 input is degenerate for stdev — return 0.0 rather than crashing
    on statistics.pstdev. Useful so the runner doesn't need to special-case."""
    from eval.metrics.reproducibility import metric_stats
    stats = metric_stats([{"grounding_rate": 0.7, "f1": 0.5}])
    assert stats["grounding_rate"]["stdev"] == 0.0
    assert stats["grounding_rate"]["mean"] == 0.7
    assert stats["grounding_rate"]["n"] == 1


def test_metric_stats_empty_input():
    """Empty per_run list → empty dict, no crash."""
    from eval.metrics.reproducibility import metric_stats
    assert metric_stats([]) == {}


def test_run_eval_runs_flag_present():
    """The merged run_eval main() exposes the --runs flag for the
    extended reproducibility mode."""
    import importlib
    import inspect
    mod = importlib.import_module("eval.run_eval")
    sig = inspect.signature(mod.main)
    assert "runs" in sig.parameters
    # Default is wrapped in a typer OptionInfo; check the inner default.
    # Should be 3 to preserve prior --reproducibility N=3 behavior.
    runs_default = sig.parameters["runs"].default
    inner = getattr(runs_default, "default", runs_default)
    assert inner == 3


def test_run_eval_v2_flags_present():
    """Confirms the v2 paper-inspired surface is reachable through the
    merged run_eval CLI: --with-taxonomy / --with-kae / --with-ace flags
    exist on main(), and the v2 metric helpers were imported into the
    module namespace. Catches accidental removal of the merge."""
    import importlib
    import inspect
    mod = importlib.import_module("eval.run_eval")
    assert hasattr(mod, "main")
    assert hasattr(mod, "_print_summary")
    # The merged module must surface the v2 metric callables (imported at
    # module top-level so the loop body can call them).
    for name in ("write_taxonomy", "kae_score", "ace_score", "hierarchy_summary"):
        assert hasattr(mod, name), f"run_eval is missing {name} after the merge"
    # main() must expose the three opt-in flags. typer.Option defaults are
    # introspectable via the function signature.
    sig = inspect.signature(mod.main)
    for flag in ("with_taxonomy", "with_kae", "with_ace"):
        assert flag in sig.parameters, f"run_eval.main is missing parameter {flag}"


# ---------------------------------------------------------------------------
# Quality-signal metrics — citation density, source-quality aggregate,
# category signals, sub-question overlap, plan-judge schema.
# ---------------------------------------------------------------------------


def _mk_finding_qs(claim: str, url: str, sq_id: str = "sq1", sqv: float = 0.7) -> Finding:
    """Quality-signal test helper (distinct from _mk_finding above which has
    a different signature for multi-signal-stop tests)."""
    return Finding(
        subquestion_id=sq_id, claim=claim, evidence="evidence",
        source_url=url, source_quality=sqv,
        domain=url.split("/")[2] if "//" in url else url,
        confidence=0.7,
    )


def test_citation_density_basic():
    """3 claims (one with 2 cites, one with 1 cite, one uncited);
    5 findings (3 cited). Exercises avg / load_bearing / uncited."""
    from eval.metrics.citation_density import citation_density
    findings = FindingsStore(findings=[
        _mk_finding_qs("a", "https://arxiv.org/abs/1"),
        _mk_finding_qs("b", "https://arxiv.org/abs/2"),
        _mk_finding_qs("c", "https://news.example.com/x"),
        _mk_finding_qs("d", "https://blog.example.com/y"),  # uncited
        _mk_finding_qs("e", "https://random.example.com/z"),  # uncited
    ])
    report = Report(
        user_prompt="p", summary="s",
        claims=[
            ReportClaim(claim="C1", confidence=0.7, supporting_finding_indices=[0, 1]),
            ReportClaim(claim="C2", confidence=0.7, supporting_finding_indices=[2]),
            ReportClaim(claim="C3", confidence=0.7, supporting_finding_indices=[]),
        ],
    )
    out = citation_density(report, findings)
    assert out["avg_per_claim"] == pytest.approx(1.0)  # (2+1+0)/3
    assert out["uncited_claims"] == 1
    # 3 distinct cited findings out of 5 harvested
    assert out["n_cited_findings"] == 3
    assert out["n_findings"] == 5
    assert out["load_bearing_fraction"] == pytest.approx(3 / 5)
    # Distribution: 1 claim with 0, 1 with 1, 1 with 2.
    assert out["claim_count_distribution"] == [1, 1, 1]


def test_citation_density_empty():
    """None / no-claim report → all zeros, no crash."""
    from eval.metrics.citation_density import citation_density
    out = citation_density(None, FindingsStore())
    assert out["avg_per_claim"] == 0.0
    assert out["load_bearing_fraction"] == 0.0
    assert out["claim_count_distribution"] == []


def test_citation_density_single_source_concentration():
    """All cited findings share one URL → herfindahl=1.0, max_share=1.0."""
    from eval.metrics.citation_density import citation_density
    same = "https://only.example.com/page"
    findings = FindingsStore(findings=[
        _mk_finding_qs("a", same), _mk_finding_qs("b", same), _mk_finding_qs("c", same),
    ])
    report = Report(
        user_prompt="p", summary="s",
        claims=[
            ReportClaim(claim="C1", confidence=0.5, supporting_finding_indices=[0, 1]),
            ReportClaim(claim="C2", confidence=0.5, supporting_finding_indices=[2]),
        ],
    )
    out = citation_density(report, findings)
    assert out["max_source_share"] == 1.0
    assert out["source_concentration_herfindahl"] == 1.0
    # Each claim has citations from exactly 1 distinct domain.
    assert out["avg_per_claim_distinct_domains"] == pytest.approx(1.0)


def test_source_quality_agg_cited_vs_all():
    """Cited subset has only .gov; all has .gov + a blog. Mean cited > mean all."""
    from eval.metrics.source_quality_agg import source_quality_summary
    findings = FindingsStore(findings=[
        _mk_finding_qs("a", "https://nih.gov/a", sqv=1.0),     # cited
        _mk_finding_qs("b", "https://medium.com/post", sqv=0.5),  # uncited
    ])
    report = Report(
        user_prompt="p", summary="s",
        claims=[
            ReportClaim(claim="C1", confidence=0.5, supporting_finding_indices=[0]),
        ],
    )
    out = source_quality_summary(report, findings)
    assert out["mean_cited_quality"] == pytest.approx(1.0)
    assert out["mean_all_quality"] == pytest.approx(0.75)
    assert out["mean_cited_quality"] > out["mean_all_quality"]
    # Tier counts cover BOTH cited and all populations.
    assert out["by_tier_cited"]["gov_edu"] == 1
    assert out["by_tier_all"]["blog_qa"] == 1


def test_contradiction_signals_severity_distribution():
    """Mixed-severity Contradictions on a Report → by_severity counts correct."""
    from eval.metrics.category_signals import contradiction_signals
    report = Report(
        user_prompt="p", summary="s",
        claims=[ReportClaim(claim="C", confidence=0.5, supporting_finding_indices=[])],
        contradictions_surfaced=[
            Contradiction(
                description="d1", finding_ids=[0, 1],
                severity="major", type="factual",
            ),
            Contradiction(
                description="d2", finding_ids=[2, 3],
                severity="minor", type="framing",
            ),
            Contradiction(
                description="d3", finding_ids=[4, 5],
                severity="major", type="factual",
            ),
        ],
    )
    out = contradiction_signals(report)
    assert out["n_surfaced"] == 3
    assert out["by_severity"]["major"] == 2
    assert out["by_severity"]["minor"] == 1
    assert out["by_severity"]["moderate"] == 0
    assert out["by_type"]["factual"] == 2
    assert out["by_type"]["framing"] == 1


def test_contradiction_aggregate_match_rate():
    """Mix of contradictory rows (some with surfaced, some without) +
    non-contradictory rows. Aggregate denominator restricts to category."""
    from eval.metrics.category_signals import contradiction_aggregate
    rows = [
        {"category": "easy", "contradictions": {"n_surfaced": 0}},
        {"category": "contradictory", "contradictions": {"n_surfaced": 2}},
        {"category": "contradictory", "contradictions": {"n_surfaced": 0}},
        {"category": "contradictory", "contradictions": {"n_surfaced": 1}},
    ]
    out = contradiction_aggregate(rows)
    assert out["category_total"] == 3
    assert out["category_with_contradictions"] == 2
    assert out["category_match_rate"] == pytest.approx(2 / 3)
    # n_surfaced_total counts ALL rows, not just contradictory category.
    assert out["n_surfaced_total"] == 3


def test_honesty_signals_caveat_reference_heuristic():
    """When a caveat string contains an UncertaintyNote.topic substring,
    caveats_reference_uncertainty=True. Otherwise False."""
    from eval.metrics.category_signals import honesty_signals
    from src.state import UncertaintyNote
    # Match case
    rr_match = ResearchRun(
        user_prompt="p",
        decomposition=Decomposition(subquestions=[
            SubQuestion(id="sq1", question="q"),
        ]),
        findings=FindingsStore(uncertainty_notes=[
            UncertaintyNote(subquestion_id="sq1", topic="dose-response curves",
                            reason="no data", researcher_id="r1"),
        ]),
        report=Report(
            user_prompt="p", summary="s",
            claims=[ReportClaim(claim="C", confidence=0.5,
                                supporting_finding_indices=[])],
            caveats=["We could not find robust dose-response curves for X."],
        ),
    )
    out = honesty_signals(rr_match)
    assert out["n_uncertainty_notes"] == 1
    assert out["n_caveats"] == 1
    assert out["caveats_reference_uncertainty"] is True
    # No-match case
    rr_no = ResearchRun(
        user_prompt="p",
        decomposition=Decomposition(subquestions=[SubQuestion(id="sq1", question="q")]),
        findings=FindingsStore(uncertainty_notes=[
            UncertaintyNote(subquestion_id="sq1", topic="dose-response curves",
                            reason="no data", researcher_id="r1"),
        ]),
        report=Report(
            user_prompt="p", summary="s",
            claims=[ReportClaim(claim="C", confidence=0.5,
                                supporting_finding_indices=[])],
            caveats=["We did not consider regulatory frameworks."],
        ),
    )
    out_no = honesty_signals(rr_no)
    assert out_no["caveats_reference_uncertainty"] is False


def test_honesty_aggregate_sparse_category():
    """Aggregate restricts denominator to sparse category."""
    from eval.metrics.category_signals import honesty_aggregate
    rows = [
        {"category": "easy",   "honesty": {"n_uncertainty_notes": 0, "n_caveats": 0}},
        {"category": "sparse", "honesty": {"n_uncertainty_notes": 2, "n_caveats": 0}},
        {"category": "sparse", "honesty": {"n_uncertainty_notes": 0, "n_caveats": 0}},
        {"category": "sparse", "honesty": {"n_uncertainty_notes": 0, "n_caveats": 1}},
    ]
    out = honesty_aggregate(rows)
    assert out["category_total"] == 3
    assert out["category_with_signal"] == 2  # rows with notes>=1 OR caveats>=1
    assert out["category_match_rate"] == pytest.approx(2 / 3)


def test_subquestion_overlap_zero_for_disjoint():
    """Sub-questions with no token overlap → mean_pairwise_jaccard ≈ 0."""
    from eval.metrics.reproducibility import subquestion_overlap
    decomp = Decomposition(subquestions=[
        SubQuestion(id="sq1", question="apple banana"),
        SubQuestion(id="sq2", question="cherry date"),
        SubQuestion(id="sq3", question="elderberry fig"),
    ])
    out = subquestion_overlap(decomp)
    assert out["n_pairs"] == 3
    assert out["mean_pairwise_jaccard"] == pytest.approx(0.0)
    assert out["max_pairwise_jaccard"] == pytest.approx(0.0)


def test_subquestion_overlap_high_for_duplicates():
    """Two near-identical sub-questions → mean_pairwise_jaccard high."""
    from eval.metrics.reproducibility import subquestion_overlap
    decomp = Decomposition(subquestions=[
        SubQuestion(id="sq1", question="what are RAG retrieval methods"),
        SubQuestion(id="sq2", question="what RAG retrieval methods exist"),
    ])
    out = subquestion_overlap(decomp)
    assert out["n_pairs"] == 1
    # Tokens identical modulo word "are"/"exist": Jaccard = 4/6 ≈ 0.67
    assert out["mean_pairwise_jaccard"] > 0.6


def test_plan_judge_result_round_trip():
    """PlanJudgeResult round-trips through model_dump_json /
    model_validate_json with all fields."""
    from src.state import PlanJudgeResult
    pjr = PlanJudgeResult(
        surface_coverage=0.8, intent_alignment=0.9,
        missing_axes=["evaluation methodology"],
        drift_examples=["sq3 drifts to inference cost — out of scope"],
        reasoning="Strong on intent; one coverage gap on eval methodology.",
    )
    js = pjr.model_dump_json()
    decoded = PlanJudgeResult.model_validate_json(js)
    assert decoded.surface_coverage == 0.8
    assert decoded.intent_alignment == 0.9
    assert decoded.missing_axes == ["evaluation methodology"]
    assert "sq3 drifts" in decoded.drift_examples[0]


def test_run_metrics_carries_plan_judge():
    """RunMetrics round-trips with plan_judge populated."""
    from src.state import PlanJudgeResult
    metrics = RunMetrics(
        total_subquestions=1, subquestions_with_findings=1,
        total_findings=1, unique_sources=1, revision_rounds=0,
        plan_judge=PlanJudgeResult(
            surface_coverage=0.7, intent_alignment=1.0,
            missing_axes=["a"], drift_examples=[],
            reasoning="ok",
        ),
    )
    decoded = RunMetrics.model_validate_json(metrics.model_dump_json())
    assert decoded.plan_judge is not None
    assert decoded.plan_judge.surface_coverage == 0.7
    assert decoded.plan_judge.intent_alignment == 1.0
    assert decoded.plan_judge.missing_axes == ["a"]


def test_run_eval_plan_judge_flag_present():
    """The merged run_eval main() exposes the --with-plan-judge flag."""
    import importlib
    import inspect
    mod = importlib.import_module("eval.run_eval")
    sig = inspect.signature(mod.main)
    assert "with_plan_judge" in sig.parameters
    # Module also surfaces the plan_judge callable + the new always-on
    # quality-signal helpers (catches regressions in the wiring).
    for name in (
        "plan_judge", "citation_density", "source_quality_summary",
        "contradiction_signals", "honesty_signals", "subquestion_overlap",
        "_print_quality_signals_table", "_print_category_aggregates",
        "researcher_signals", "unique_sources",
        "subquestion_count_stats",
        "shape_selection_stats", "unique_source_stats",
    ):
        assert hasattr(mod, name), f"run_eval is missing {name}"


# ---------------------------------------------------------------------------
# Researcher / unique-source signals + reproducibility helper tests.
# ---------------------------------------------------------------------------


def _mk_rr_for_signals(
    *,
    findings: list[Finding] | None = None,
    notes: list | None = None,
    subquestions: list[SubQuestion] | None = None,
    shape: ClassifiedShape | None = None,
    report: Report | None = None,
) -> ResearchRun:
    """Tiny ResearchRun builder for the per-prompt signal tests."""
    from src.state import UncertaintyNote
    decomp = Decomposition(
        user_prompt="p",
        approach_summary="a",
        subquestions=subquestions or [SubQuestion(id="sq1", question="q?")],
    )
    fs = FindingsStore(findings=findings or [], uncertainty_notes=notes or [])
    return ResearchRun(
        user_prompt="p",
        decomposition=decomp,
        findings=fs,
        report=report,
        shape=shape,
    )


# ---- researcher_signals ----------------------------------------------------


def test_researcher_signals_basic_counts():
    """3 sub-qs, researcher r1 produces 2 findings on sq1 + sq2; r2 silent
    on sq3 (no finding, no uncertainty); r1 also emits one uncertainty note."""
    from eval.metrics.researcher_signals import researcher_signals
    from src.state import UncertaintyNote
    sqs = [
        SubQuestion(id="sq1", question="q1?"),
        SubQuestion(id="sq2", question="q2?"),
        SubQuestion(id="sq3", question="q3?"),
    ]
    findings = [
        Finding(subquestion_id="sq1", claim="c1", evidence="e",
                source_url="https://a.example.com", confidence=0.7,
                researcher_id="r1"),
        Finding(subquestion_id="sq2", claim="c2", evidence="e",
                source_url="https://b.example.com", confidence=0.7,
                researcher_id="r1"),
    ]
    notes = [
        UncertaintyNote(
            subquestion_id="sq1", topic="x", reason="r",
            researcher_id="r1",
        ),
    ]
    rr = _mk_rr_for_signals(findings=findings, notes=notes, subquestions=sqs)
    out = researcher_signals(rr)
    assert out["n_subquestions"] == 3
    assert out["n_subquestions_without_findings"] == 1  # sq3
    assert out["n_researchers_with_findings"] == 1
    assert out["n_researchers_with_uncertainty"] == 1
    assert out["n_researchers_with_both"] == 1
    assert out["total_findings"] == 2
    assert out["total_uncertainty_notes"] == 1
    assert out["max_findings_one_researcher"] == 2
    assert out["mean_findings_per_active_researcher"] == pytest.approx(2.0)
    assert "scout_used" not in out


def test_researcher_signals_concentration():
    """One researcher contributes 4/5 findings → high max vs mean."""
    from eval.metrics.researcher_signals import researcher_signals
    findings = [
        Finding(subquestion_id="sq1", claim=f"c{i}", evidence="e",
                source_url=f"https://{i}.com", confidence=0.7,
                researcher_id="rA")
        for i in range(4)
    ] + [
        Finding(subquestion_id="sq1", claim="c5", evidence="e",
                source_url="https://5.com", confidence=0.7,
                researcher_id="rB"),
    ]
    out = researcher_signals(_mk_rr_for_signals(findings=findings))
    assert out["max_findings_one_researcher"] == 4
    assert out["n_researchers_with_findings"] == 2
    assert out["mean_findings_per_active_researcher"] == pytest.approx(2.5)


# ---- unique_sources --------------------------------------------------------


def test_unique_sources_cited_vs_harvested():
    """5 harvested URLs across 4 domains; 3 cited across 2 domains."""
    from eval.metrics.unique_sources import unique_sources
    findings = [
        _mk_finding_qs("a", "https://www.nytimes.com/1"),  # cited
        _mk_finding_qs("b", "https://nytimes.com/2"),       # cited (same domain)
        _mk_finding_qs("c", "https://arxiv.org/abs/3"),     # cited
        _mk_finding_qs("d", "https://blog.example.com/4"),  # uncited
        _mk_finding_qs("e", "https://other.example.com/5"), # uncited
    ]
    report = Report(
        user_prompt="p", summary="s",
        claims=[
            ReportClaim(claim="C1", confidence=0.5, supporting_finding_indices=[0, 1]),
            ReportClaim(claim="C2", confidence=0.5, supporting_finding_indices=[2]),
        ],
    )
    out = unique_sources(report, FindingsStore(findings=findings))
    assert out["n_unique_urls_cited"] == 3
    assert out["n_unique_urls_harvested"] == 5
    # nytimes.com (after www-strip) + arxiv.org = 2 cited domains
    assert out["n_unique_domains_cited"] == 2
    # nytimes + arxiv + blog.example.com + other.example.com = 4
    assert out["n_unique_domains_harvested"] == 4
    # 3 distinct cited URLs from a 5-URL harvest = 3/5 cited-to-harvested URL ratio
    assert out["cited_to_harvested_url_ratio"] == pytest.approx(3 / 5)
    # 2 cited domains from 4 harvested = 0.5
    assert out["cited_to_harvested_domain_ratio"] == pytest.approx(0.5)


def test_unique_sources_empty():
    """Empty findings + None report → all zeros, no crash."""
    from eval.metrics.unique_sources import unique_sources
    out = unique_sources(None, FindingsStore())
    assert out["n_unique_urls_cited"] == 0
    assert out["n_unique_urls_harvested"] == 0
    assert out["cited_url_reuse_ratio"] == 0.0
    assert out["cited_to_harvested_url_ratio"] == 0.0


def test_unique_sources_url_reuse_ratio():
    """Same URL cited from 2 claims → reuse_ratio = 2.0 / 1 distinct = 2.0."""
    from eval.metrics.unique_sources import unique_sources
    findings = [_mk_finding_qs("a", "https://only.example.com/p")]
    report = Report(
        user_prompt="p", summary="s",
        claims=[
            ReportClaim(claim="C1", confidence=0.5, supporting_finding_indices=[0]),
            ReportClaim(claim="C2", confidence=0.5, supporting_finding_indices=[0]),
        ],
    )
    out = unique_sources(report, FindingsStore(findings=findings))
    assert out["n_unique_urls_cited"] == 1
    assert out["cited_url_reuse_ratio"] == pytest.approx(2.0)
    assert out["cited_domain_diversity_ratio"] == pytest.approx(1.0)  # 1 dom / 1 url


# ---- shape_selection_stats -------------------------------------------------


def test_shape_selection_stats_modal_share_and_per_shape():
    """3 runs: PRE_STRUCTURED (conf 0.9, 0.8) + DISCOVERY (conf 0.5)
    → modal=PRE_STRUCTURED at 2/3; per-shape conf carries the right buckets."""
    from eval.metrics.reproducibility import shape_selection_stats
    runs = [
        _mk_rr_for_signals(shape=ClassifiedShape(
            shape="PRE_STRUCTURED", confidence=0.9, reasoning="r")),
        _mk_rr_for_signals(shape=ClassifiedShape(
            shape="PRE_STRUCTURED", confidence=0.8, reasoning="r")),
        _mk_rr_for_signals(shape=ClassifiedShape(
            shape="DISCOVERY", confidence=0.5, reasoning="r")),
    ]
    out = shape_selection_stats(runs)
    assert out["distribution"] == {"PRE_STRUCTURED": 2, "DISCOVERY": 1}
    assert out["modal_shape"] == "PRE_STRUCTURED"
    assert out["modal_share"] == pytest.approx(2 / 3)
    assert out["n_runs_with_shape"] == 3
    assert out["n_runs"] == 3
    assert out["mean_confidence"] == pytest.approx((0.9 + 0.8 + 0.5) / 3)
    cb = out["confidence_by_shape"]
    assert cb["PRE_STRUCTURED"]["mean"] == pytest.approx(0.85)
    assert cb["PRE_STRUCTURED"]["n"] == 2
    assert cb["DISCOVERY"]["mean"] == pytest.approx(0.5)
    assert cb["DISCOVERY"]["n"] == 1
    # per_run_shapes preserves order with confidence
    assert out["per_run_shapes"][0] == {"shape": "PRE_STRUCTURED", "confidence": 0.9}


def test_shape_selection_stats_no_shapes():
    """Runs without rr.shape → modal_shape None + zero confidences."""
    from eval.metrics.reproducibility import shape_selection_stats
    runs = [_mk_rr_for_signals(), _mk_rr_for_signals()]
    out = shape_selection_stats(runs)
    assert out["modal_shape"] is None
    assert out["modal_share"] == 0.0
    assert out["n_runs_with_shape"] == 0
    assert out["confidence_by_shape"] == {}
    assert out["per_run_shapes"] == [None, None]


# ---- subquestion_count_stats -----------------------------------------------


def test_subquestion_count_stats_bimodal():
    """Bimodal {3, 7, 3} count → modal_count=3, modal_share=2/3."""
    from eval.metrics.reproducibility import subquestion_count_stats
    runs = [
        _mk_rr_for_signals(subquestions=[
            SubQuestion(id=f"sq{i}", question="q?") for i in range(3)
        ]),
        _mk_rr_for_signals(subquestions=[
            SubQuestion(id=f"sq{i}", question="q?") for i in range(7)
        ]),
        _mk_rr_for_signals(subquestions=[
            SubQuestion(id=f"sq{i}", question="q?") for i in range(3)
        ]),
    ]
    out = subquestion_count_stats(runs)
    assert out["distribution"] == {3: 2, 7: 1}
    assert out["modal_count"] == 3
    assert out["modal_share"] == pytest.approx(2 / 3)
    assert out["mean"] == pytest.approx((3 + 7 + 3) / 3)
    assert out["min"] == 3
    assert out["max"] == 7
    assert out["per_run_counts"] == [3, 7, 3]


# ---- unique_source_stats ---------------------------------------------------


def test_unique_source_stats_cited_url_jaccard():
    """Two runs sharing 2/3 of their cited URLs → jaccard 2/4 = 0.5."""
    from eval.metrics.reproducibility import unique_source_stats

    def _mk_run_with_cites(urls: list[str]) -> ResearchRun:
        findings = [_mk_finding_qs(f"c{i}", u) for i, u in enumerate(urls)]
        report = Report(
            user_prompt="p", summary="s",
            claims=[
                ReportClaim(
                    claim=f"C{i}", confidence=0.5,
                    supporting_finding_indices=[i],
                )
                for i in range(len(urls))
            ],
        )
        return _mk_rr_for_signals(findings=findings, report=report)

    runs = [
        _mk_run_with_cites([
            "https://a.com/1", "https://b.com/2", "https://c.com/3",
        ]),
        _mk_run_with_cites([
            "https://a.com/1", "https://b.com/2", "https://d.com/4",
        ]),
    ]
    out = unique_source_stats(runs)
    cited = out["cited_url_jaccard"]
    # 2 shared / 4 union = 0.5
    assert cited["mean_jaccard"] == pytest.approx(0.5)
    # Same domains too — domain set follows the URL set 1:1 here.
    assert out["cited_domain_jaccard"]["mean_jaccard"] == pytest.approx(0.5)
    # per_run_unique_counts populated.
    assert out["per_run_unique_counts"][0]["n_cited_urls"] == 3


def test_unique_source_stats_single_run_returns_empty_jaccards():
    """Single run → no pair to Jaccard → empty Jaccard dicts."""
    from eval.metrics.reproducibility import unique_source_stats
    out = unique_source_stats([_mk_rr_for_signals()])
    assert out["cited_url_jaccard"] == {}
    assert out["cited_domain_jaccard"] == {}
    assert out["per_run_unique_counts"][0]["n_cited_urls"] == 0


# ---- _print_reproducibility_summary signature integration -----------------


def test_print_reproducibility_summary_accepts_new_args():
    """The new positional args (sq_count_sel, unique_sel) are in the signature."""
    import inspect
    from eval.run_eval import _print_reproducibility_summary
    sig = inspect.signature(_print_reproducibility_summary)
    for name in ("shape_sel", "sq_count_sel", "unique_sel"):
        assert name in sig.parameters, f"missing param {name}"
    assert "scout_sel" not in sig.parameters


# ---------------------------------------------------------------------------
# Final exporter — FinalOutput / ConditionCheck schema + export_final phase.
# ---------------------------------------------------------------------------


def test_final_output_and_condition_check_round_trip():
    """FinalOutput + ConditionCheck round-trip through model_dump_json."""
    from src.state import FinalOutput, ConditionCheck
    fo = FinalOutput(
        format_detected="bulleted comparison list",
        content="- A: ...\n- B: ...\n- C: ...",
        conditions=[
            ConditionCheck(
                condition="compare on at least 3 axes",
                status="satisfied",
                evidence="Three bullets enumerate axes A/B/C",
            ),
            ConditionCheck(
                condition="cite peer-reviewed sources",
                status="partial",
                evidence="2 of 5 citations are arxiv preprints (not peer-reviewed)",
                notes="Worth flagging; preprints aren't strictly peer-reviewed.",
            ),
            ConditionCheck(
                condition="under 500 words",
                status="not_applicable",
                evidence="",
                notes="Bulleted list is well under 500 words trivially.",
            ),
        ],
        unmet_conditions=["cite peer-reviewed sources"],
        notes="Inferred bulleted-comparison format from the prompt's 'compare X vs Y' framing.",
    )
    fo2 = FinalOutput.model_validate_json(fo.model_dump_json())
    assert fo2.format_detected == fo.format_detected
    assert len(fo2.conditions) == 3
    assert fo2.conditions[1].status == "partial"
    assert fo2.unmet_conditions == ["cite peer-reviewed sources"]


def test_condition_check_status_enum_rejects_unknown():
    """ConditionCheck.status is a Literal — typo'd values raise."""
    from src.state import ConditionCheck
    with pytest.raises(ValidationError):
        ConditionCheck(condition="x", status="kind_of_satisfied")  # type: ignore[arg-type]


def test_research_run_carries_final_output_field():
    """ResearchRun has a final_output field that defaults to None and
    accepts a FinalOutput when set."""
    from src.state import FinalOutput
    decomp = Decomposition(
        user_prompt="p", approach_summary="a",
        subquestions=[SubQuestion(id="sq1", question="q?")],
    )
    rr = ResearchRun(
        user_prompt="p",
        decomposition=decomp,
        findings=FindingsStore(),
    )
    assert rr.final_output is None
    rr.final_output = FinalOutput(
        format_detected="markdown summary",
        content="hello",
    )
    rr2 = ResearchRun.model_validate_json(rr.model_dump_json())
    assert rr2.final_output is not None
    assert rr2.final_output.format_detected == "markdown summary"


def test_export_final_calls_chat_json_with_final_output_schema(monkeypatch):
    """export_final passes the right system prompt + writer_model + schema
    to chat_json. We monkeypatch chat_json to capture args without making
    a real LLM call. _render_markdown is also stubbed so the test doesn't
    write a real .md file to disk."""
    from src.agent import exporter
    from src.state import FinalOutput, Report, ReportClaim
    captured = {}

    def fake_chat_json(api_key, model, system, user, schema,
                      max_retries=2, max_tokens=16384):
        captured["model"] = model
        captured["system"] = system
        captured["user"] = user
        captured["schema"] = schema
        return FinalOutput(
            format_detected="markdown summary",
            content="rendered content",
        )

    monkeypatch.setattr(exporter, "chat_json", fake_chat_json)
    # Stub the markdown renderer so the exporter test is hermetic — we
    # test the markdown render path separately below.
    monkeypatch.setattr(
        exporter, "_render_markdown",
        lambda **kw: Path("/tmp/fake_final.md"),
    )

    cfg = Config(anthropic_api_key="k", tavily_api_key="x",
                 writer_model="opus-test", researcher_model="haiku-test")
    decomp = Decomposition(
        user_prompt="ignored", approach_summary="a",
        subquestions=[SubQuestion(id="sq1", question="What is X?")],
    )
    findings = FindingsStore(findings=[
        Finding(subquestion_id="sq1", claim="c", evidence="evidence text",
                source_url="https://example.com/a", confidence=0.7),
    ])
    report = Report(
        user_prompt="Compare X and Y in 3 bullets",
        summary="X is foo. Y is bar.",
        claims=[
            ReportClaim(claim="X works because A",
                        confidence=0.8, supporting_finding_indices=[0]),
        ],
    )
    out = exporter.export_final(cfg, "Compare X and Y in 3 bullets",
                                decomp, findings, report)
    # Schema + model are correct.
    assert captured["model"] == "opus-test"
    assert captured["schema"] is FinalOutput
    # The user prompt is verbatim in the exporter's user message — the
    # exporter relies on this for format/condition extraction.
    assert "Compare X and Y in 3 bullets" in captured["user"]
    # Sub-questions surface in the user message so the exporter can
    # check coverage against the decomposition.
    assert "[sq1]" in captured["user"]
    # Cited evidence text from the Finding flows through.
    assert "evidence text" in captured["user"]
    # System prompt names the role + the audit job.
    assert "FINAL EXPORTER" in captured["system"]
    # Returned object is what the LLM produced (passes through).
    assert out.format_detected == "markdown summary"


def test_export_final_populates_output_file_path_on_md_success(monkeypatch, tmp_path):
    """When _render_markdown returns a path, export_final stamps it onto FinalOutput.output_file_path."""
    from src.agent import exporter
    from src.state import FinalOutput, Report

    monkeypatch.setattr(
        exporter, "chat_json",
        lambda *a, **k: FinalOutput(
            format_detected="markdown",
            content="hello world",
            notes="seeded note",
        ),
    )
    fake_md = tmp_path / "out.md"
    monkeypatch.setattr(exporter, "_render_markdown", lambda **kw: fake_md)

    cfg = Config(anthropic_api_key="k", tavily_api_key="x",
                 exporter_md_dir=str(tmp_path))
    decomp = Decomposition(
        user_prompt="p", approach_summary="a",
        subquestions=[SubQuestion(id="sq1", question="q?")],
    )
    out = exporter.export_final(
        cfg, "p", decomp, FindingsStore(),
        Report(user_prompt="p", summary="s", claims=[]),
    )
    assert out.output_file_path == str(fake_md)
    # Notes preserved — successful markdown render must NOT clobber the LLM's notes.
    assert out.notes == "seeded note"


def test_export_final_records_md_failure_in_notes(monkeypatch, tmp_path):
    """Markdown render failures don't propagate — output_file_path stays None and the failure is appended to notes."""
    from src.agent import exporter
    from src.state import FinalOutput, Report

    monkeypatch.setattr(
        exporter, "chat_json",
        lambda *a, **k: FinalOutput(
            format_detected="markdown", content="hello",
            notes="prior note",
        ),
    )

    def boom(**kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(exporter, "_render_markdown", boom)

    cfg = Config(anthropic_api_key="k", tavily_api_key="x",
                 exporter_md_dir=str(tmp_path))
    decomp = Decomposition(
        user_prompt="p", approach_summary="a",
        subquestions=[SubQuestion(id="sq1", question="q?")],
    )
    out = exporter.export_final(
        cfg, "p", decomp, FindingsStore(),
        Report(user_prompt="p", summary="s", claims=[]),
    )
    assert out.output_file_path is None
    # Existing notes preserved + failure appended via " | " separator.
    assert "prior note" in out.notes
    assert "Markdown render failed" in out.notes
    assert "disk full" in out.notes


def test_render_markdown_writes_a_real_md_file(tmp_path):
    """End-to-end smoke for _render_markdown: writes a realistic markdown
    payload (including a mermaid block) and verifies a non-empty .md file
    lands at the returned path with the expected preamble + content."""
    from src.agent.exporter import _render_markdown

    body = (
        "# Heading\n\n"
        "- bullet one\n- bullet two\n\n"
        "```mermaid\nflowchart LR\nA --> B\n```\n"
    )
    out_path = _render_markdown(
        content=body,
        user_prompt="What is the comparative effectiveness of X vs Y?",
        format_detected="markdown summary",
        out_dir=str(tmp_path),
    )
    assert out_path.exists()
    # Filename pattern: final_{stamp}_{uuid8}_{slug}.md — slug is derived
    # from the prompt so a directory listing tells you which run is which.
    assert out_path.name.startswith("final_")
    assert out_path.suffix == ".md"
    assert "comparative_effectiveness" in out_path.name
    text = out_path.read_text(encoding="utf-8")
    # Preamble carries the prompt + format so the doc is self-describing.
    assert "Original prompt:" in text
    assert "Detected format:" in text
    assert "comparative effectiveness of X vs Y" in text
    # Body is preserved verbatim (mermaid block intact).
    assert "```mermaid" in text
    assert "flowchart LR" in text
    assert "# Heading" in text
