"""Smoke tests — imports and pure-Python logic, no API calls."""
from pathlib import Path

from src.state import (
    Decomposition, SubQuestion, Finding, FindingsStore,
    ReportClaim, Report, CriticIssue, CriticReport, RunMetrics, ResearchRun,
    StageStats, ToolMix, RunMetadata, FinalOutput, ConditionCheck,
)
from src.config import Config
from src.agent.confidence import (
    compute_source_quality,
    SQ_PEER_REVIEWED, SQ_NEWS, SQ_BLOG_QA, SQ_UNKNOWN, SQ_GOV_EDU,
)
from src.tools.fetch import FetchResult, FetchContext
from src import llm
from eval.metrics.coverage import coverage_rate
from eval.metrics.reproducibility import claim_jaccard, subquestion_jaccard


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
    # Both findings share the same source URL → dedupes to 1.
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


def test_no_refetch_verifier_module():
    """Structural guard: src/agent/refetch_verifier.py must not exist."""
    import importlib
    try:
        importlib.import_module("src.agent.refetch_verifier")
    except ImportError:
        return
    raise AssertionError(
        "src/agent/refetch_verifier.py must not exist."
    )


def test_source_quality_buckets():
    """compute_source_quality buckets and the suffix-with-dot guard."""
    assert compute_source_quality("https://arxiv.org/abs/1234") == SQ_PEER_REVIEWED
    assert compute_source_quality("https://www.nature.com/foo") == SQ_PEER_REVIEWED
    assert compute_source_quality("https://www.nytimes.com/x") == SQ_NEWS
    assert compute_source_quality("https://medium.com/x/y") == SQ_BLOG_QA
    assert compute_source_quality("https://stanford.edu/x") == SQ_GOV_EDU
    assert compute_source_quality("https://example.com/x") == SQ_UNKNOWN
    # "evilnature.com" must NOT match "nature.com".
    assert compute_source_quality("https://evilnature.com/x") == SQ_UNKNOWN
    # Unparseable URL falls back to unknown rather than raising.
    assert compute_source_quality("not-a-url") == SQ_UNKNOWN


def test_finding_source_quality_field():
    """Finding carries source_quality + self_reported_confidence."""
    f = Finding(
        subquestion_id="sq1", claim="x", evidence="x is x.",
        source_url="https://medium.com/x", confidence=0.45,
        self_reported_confidence=0.9, source_quality=0.5,
    )
    assert f.source_quality == 0.5
    assert f.self_reported_confidence == 0.9
    assert f.confidence == 0.45  # already weighted


def test_finding_provenance_fields_default_and_round_trip():
    """Finding's fetched_at / content_hash / domain default and round-trip."""
    import hashlib
    from datetime import datetime, timezone
    f_default = Finding(
        subquestion_id="sq1", claim="x", evidence="x is x.",
        source_url="https://example.com/p", confidence=0.7,
    )
    assert f_default.fetched_at is not None
    assert f_default.content_hash == ""
    assert f_default.domain == ""

    evidence = "Live model collapse occurs when..."
    expected_hash = hashlib.sha256(evidence.encode("utf-8")).hexdigest()
    f = Finding(
        subquestion_id="sq2", claim="c", evidence=evidence,
        source_url="https://www.nature.com/articles/x", confidence=0.85,
        content_hash=expected_hash, domain="www.nature.com",
        fetched_at=datetime(2026, 5, 4, tzinfo=timezone.utc),
    )
    assert f.content_hash == expected_hash
    assert f.domain == "www.nature.com"
    restored = Finding.model_validate_json(f.model_dump_json())
    assert restored.content_hash == expected_hash
    assert restored.domain == "www.nature.com"
    assert restored.fetched_at == f.fetched_at


def test_fetch_result_is_usable_only_for_ok():
    """FetchResult.is_usable must be True only for status='ok'."""
    assert FetchResult(url="u", status="ok", text="real content").is_usable is True
    for bad in ("paywall", "blocked", "not_found", "timeout",
                "rate_limited", "non_html", "error"):
        assert FetchResult(url="u", status=bad).is_usable is False, bad


def test_fetch_context_three_strikes():
    """FetchContext blocks a domain after `failure_threshold` failures."""
    ctx = FetchContext(failure_threshold=3)
    ctx.record_failure("https://flaky.example/page1")
    ctx.record_failure("https://flaky.example/page2")
    assert ctx.is_blocked("https://flaky.example/page3") is False
    ctx.record_failure("https://flaky.example/page4")
    assert ctx.is_blocked("https://flaky.example/page5") is True
    # Different domain unaffected.
    assert ctx.is_blocked("https://other.example/page") is False


def test_track_stage_records_unknown_models_at_zero_cost():
    """track_stage tags calls; unknown models cost 0.0 but still count;
    aggregate_calls_by_stage rolls up by stage."""
    llm.reset_call_log()

    class FakeUsage:
        input_tokens = 100
        output_tokens = 50

    class FakeResp:
        usage = FakeUsage()

    with llm.track_stage("decompose"):
        llm._record_call("claude-haiku-4-5", FakeResp())
    with llm.track_stage("researcher"):
        llm._record_call("unknown-model-xyz", FakeResp())
        llm._record_call("claude-haiku-4-5", FakeResp())

    rollup = llm.aggregate_calls_by_stage()
    assert set(rollup) == {"decompose", "researcher"}
    assert rollup["decompose"].calls == 1
    assert rollup["researcher"].calls == 2
    # Unknown model contributes input/output tokens but $0 cost.
    assert rollup["researcher"].input_tokens == 200
    haiku_in, haiku_out = llm.PRICE_USD_PER_M_TOKENS["claude-haiku-4-5"]
    expected_haiku_cost = (100 * haiku_in + 50 * haiku_out) / 1_000_000
    assert abs(rollup["decompose"].cost_usd - expected_haiku_cost) < 1e-9
    # 1 known + 1 unknown should equal one haiku cost (unknown adds 0).
    assert abs(rollup["researcher"].cost_usd - expected_haiku_cost) < 1e-9
    llm.reset_call_log()


def test_subquestion_jaccard_partial_overlap():
    """subquestion_jaccard: two runs sharing 2/3 sub-questions → 2/4 = 0.5."""
    rr_a = ResearchRun(
        user_prompt="p", findings=FindingsStore(),
        decomposition=Decomposition(subquestions=[
            SubQuestion(id="sq1", question="What is X?"),
            SubQuestion(id="sq2", question="What is Y?"),
            SubQuestion(id="sq3", question="What is Z?"),
        ]),
    )
    rr_b = ResearchRun(
        user_prompt="p", findings=FindingsStore(),
        decomposition=Decomposition(subquestions=[
            SubQuestion(id="sq1", question="What is X?"),
            SubQuestion(id="sq2", question="What is Y?"),
            SubQuestion(id="sq3", question="What is Q?"),
        ]),
    )
    out = subquestion_jaccard([rr_a, rr_b])
    assert abs(out["mean_jaccard"] - 0.5) < 1e-9


def test_run_metrics_per_stage_and_metadata_round_trip():
    """RunMetrics per_stage + ToolMix and ResearchRun.metadata round-trip."""
    metrics = RunMetrics(
        total_subquestions=3,
        subquestions_with_findings=2,
        total_findings=4,
        unique_sources=3,
        revision_rounds=0,
        per_stage={"decompose": StageStats(calls=1, input_tokens=100,
                                            output_tokens=50, cost_usd=0.0008,
                                            elapsed_seconds=1.5)},
        total_input_tokens=100,
        total_output_tokens=50,
        total_cost_usd=0.0008,
        tool_mix=ToolMix(web_search=2, save_finding=4),
    )
    metadata = RunMetadata(
        commit_hash="abc123", branch="main",
        models={"researcher": "claude-haiku-4-5"},
        config_snapshot={"min_subquestions": 3},
    )
    rr = ResearchRun(
        user_prompt="p", findings=FindingsStore(),
        decomposition=Decomposition(subquestions=[]),
        metrics=metrics, metadata=metadata,
    )
    blob = rr.model_dump_json()
    restored = ResearchRun.model_validate_json(blob)
    assert restored.metrics.per_stage["decompose"].calls == 1
    assert restored.metrics.tool_mix.save_finding == 4
    assert restored.metadata.commit_hash == "abc123"


def test_final_output_round_trip_on_research_run():
    """FinalOutput hangs off ResearchRun and round-trips with conditions intact."""
    fo = FinalOutput(
        format_detected="markdown summary",
        content="# Hello\n\n- bullet",
        conditions=[
            ConditionCheck(condition="under 500 words", status="satisfied",
                           evidence="length ~30 words"),
            ConditionCheck(condition="cite peer-reviewed", status="not_satisfied",
                           notes="no peer-reviewed source available"),
        ],
        unmet_conditions=["cite peer-reviewed"],
        notes="default narrative",
        output_file_path="/tmp/x.md",
    )
    rr = ResearchRun(
        user_prompt="p",
        decomposition=Decomposition(subquestions=[SubQuestion(id="sq1", question="q?")]),
        findings=FindingsStore(),
        final_output=fo,
    )
    restored = ResearchRun.model_validate_json(rr.model_dump_json())
    assert restored.final_output is not None
    assert restored.final_output.format_detected == "markdown summary"
    assert len(restored.final_output.conditions) == 2
    assert restored.final_output.unmet_conditions == ["cite peer-reviewed"]
    assert restored.final_output.output_file_path == "/tmp/x.md"


def test_export_final_calls_chat_json_with_final_output_schema(monkeypatch):
    """export_final passes the right system prompt + writer_model + schema."""
    from src.agent import exporter
    captured = {}

    def fake_chat_json(api_key, model, system, user, schema,
                       max_retries=2, max_tokens=4096):
        captured["model"] = model
        captured["system"] = system
        captured["user"] = user
        captured["schema"] = schema
        return FinalOutput(
            format_detected="markdown summary",
            content="rendered content",
        )

    monkeypatch.setattr(exporter, "chat_json", fake_chat_json)
    # Stub the markdown renderer; tested separately below.
    monkeypatch.setattr(
        exporter, "_render_markdown",
        lambda **kw: Path("/tmp/fake_final.md"),
    )

    cfg = Config(anthropic_api_key="k", tavily_api_key="x",
                 writer_model="opus-test", researcher_model="haiku-test")
    decomp = Decomposition(
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
    assert captured["model"] == "opus-test"
    assert captured["schema"] is FinalOutput
    # User prompt verbatim, sub-questions surfaced, cited evidence flows through.
    assert "Compare X and Y in 3 bullets" in captured["user"]
    assert "[sq1]" in captured["user"]
    assert "evidence text" in captured["user"]
    assert "FINAL EXPORTER" in captured["system"]
    assert "mermaid" in captured["system"]
    assert out.format_detected == "markdown summary"


def test_export_final_populates_output_file_path_on_md_success(monkeypatch, tmp_path):
    """_render_markdown success stamps the path onto FinalOutput.output_file_path."""
    from src.agent import exporter

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
        subquestions=[SubQuestion(id="sq1", question="q?")],
    )
    out = exporter.export_final(
        cfg, "p", decomp, FindingsStore(),
        Report(user_prompt="p", summary="s", claims=[]),
    )
    assert out.output_file_path == str(fake_md)
    # Notes preserved — successful render must NOT clobber the LLM's notes.
    assert out.notes == "seeded note"


def test_export_final_records_md_failure_in_notes(monkeypatch, tmp_path):
    """Markdown render failures don't propagate; notes is appended."""
    from src.agent import exporter

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
        subquestions=[SubQuestion(id="sq1", question="q?")],
    )
    out = exporter.export_final(
        cfg, "p", decomp, FindingsStore(),
        Report(user_prompt="p", summary="s", claims=[]),
    )
    assert out.output_file_path is None
    # Existing notes preserved + failure appended.
    assert "prior note" in out.notes
    assert "Markdown render failed" in out.notes
    assert "disk full" in out.notes


def test_render_metrics_markdown_includes_eval_sections(tmp_path):
    """Renderer surfaces all four eval sections + final-output audit + footnote."""
    from src import metrics_md
    from src.state import RunMetadata

    rr = ResearchRun(
        user_prompt="Compare X and Y",
        decomposition=Decomposition(subquestions=[
            SubQuestion(id="sq1", question="What is X?"),
            SubQuestion(id="sq2", question="What is Y?"),
        ]),
        findings=FindingsStore(findings=[
            Finding(subquestion_id="sq1", claim="c1", evidence="e1",
                    source_url="https://example.com/a", confidence=0.7),
        ]),
        report=Report(
            user_prompt="Compare X and Y", summary="X is foo. Y is bar.",
            claims=[
                ReportClaim(claim="X works", confidence=0.85,
                            supporting_finding_indices=[0]),
                ReportClaim(claim="Y is small", confidence=0.40,
                            supporting_finding_indices=[]),
            ],
        ),
        metrics=RunMetrics(
            total_subquestions=2, subquestions_with_findings=1,
            total_findings=1, unique_sources=1, revision_rounds=0,
            elapsed_seconds=12.3,
            per_stage={
                "decompose": StageStats(calls=1, input_tokens=100,
                                         output_tokens=50, cost_usd=0.001,
                                         elapsed_seconds=1.5),
                "researcher": StageStats(calls=4, input_tokens=400,
                                          output_tokens=200, cost_usd=0.012,
                                          elapsed_seconds=8.0),
            },
            total_input_tokens=500, total_output_tokens=250,
            total_cost_usd=0.013,
            tool_mix=ToolMix(web_search=2, save_finding=4),
        ),
        metadata=RunMetadata(
            commit_hash="abc12345", branch="main",
            models={"researcher": "haiku", "judge": "opus"},
        ),
        final_output=FinalOutput(
            format_detected="markdown summary",
            content="X is foo. Y is bar.",
            conditions=[
                ConditionCheck(condition="under 500 words", status="satisfied"),
                ConditionCheck(condition="cite peer-reviewed",
                               status="not_satisfied"),
            ],
            unmet_conditions=["cite peer-reviewed"],
            output_file_path=str(tmp_path / "final_X.md"),
        ),
    )
    eval_results = {
        "grounding": {"rate": 0.5, "details": [
            {"claim_index": 0, "grounded": True, "reason": "evidence backs claim"},
            {"claim_index": 1, "grounded": False, "reason": "no supporting findings cited"},
        ]},
        "calibration": {"buckets": [
            {"range": [0.0, 0.3], "n": 0, "grounded_rate": None,
             "mean_stated_confidence": None},
            {"range": [0.3, 0.6], "n": 1, "grounded_rate": 0.0,
             "mean_stated_confidence": 0.40},
            {"range": [0.6, 0.8], "n": 0, "grounded_rate": None,
             "mean_stated_confidence": None},
            {"range": [0.8, 1.01], "n": 1, "grounded_rate": 1.0,
             "mean_stated_confidence": 0.85},
        ], "n_claims": 2},
        "coverage": {"rate": 0.5, "covered": ["sq1"], "missed": ["sq2"]},
        "judge": {
            "correctness": 4.0, "completeness": 3.5, "calibration": 3.0,
            "source_quality": 3.5, "conflict_handling": 2.5, "overall": 3.3,
            "notes": "decent coverage; weak on conflict handling.",
        },
    }
    body = metrics_md.render_metrics_markdown(
        rr, eval_results=eval_results,
        paired_final_path=str(tmp_path / "final_X.md"),
    )
    assert "Compare X and Y" in body
    assert "final_X.md" in body
    assert "### Grounding" in body and "50%" in body
    assert "### Calibration" in body and "0.40" in body
    assert "### Coverage" in body and "`sq1`" in body and "`sq2`" in body
    assert "### LLM judge rubric" in body and "decent coverage" in body
    assert "## Final output audit" in body
    assert "cite peer-reviewed" in body
    # Operational footnote with mermaid pie (2+ stages).
    assert "<details>" in body and "</details>" in body
    assert "### Per-stage" in body
    assert "```mermaid" in body and "pie title" in body
    assert "### Tool mix" in body
    assert "abc12345" in body  # commit hash


def test_render_metrics_markdown_handles_degenerate_run():
    """Renderer produces a valid body when metrics/metadata/final_output/report are None."""
    from src import metrics_md
    rr = ResearchRun(
        user_prompt="p",
        decomposition=Decomposition(subquestions=[]),
        findings=FindingsStore(),
    )
    body = metrics_md.render_metrics_markdown(rr, eval_results={})
    assert body.startswith("# Run metrics")
    assert "_not run_" in body
    # Operational footnote still rendered, cost/per-stage subsections skipped.
    assert "<details>" in body
    # Final-output audit silently omitted when there's no FinalOutput.
    assert "## Final output audit" not in body


def test_render_metrics_markdown_renders_eval_failures():
    """An eval result with an `error` key surfaces as an inline marker."""
    from src import metrics_md
    rr = ResearchRun(
        user_prompt="p",
        decomposition=Decomposition(subquestions=[]),
        findings=FindingsStore(),
    )
    eval_results = {
        "grounding": {"error": "judge timeout"},
        "calibration": {"error": "skipped: grounding failed"},
        "coverage": {"error": "boom"},
        "judge": {"error": "rate limited"},
    }
    body = metrics_md.render_metrics_markdown(rr, eval_results=eval_results)
    assert "judge timeout" in body
    assert "skipped: grounding failed" in body
    assert "boom" in body
    assert "rate limited" in body
    # Each section carries its own failure marker.
    assert body.count("(eval failed:") == 4


def test_write_metrics_markdown_pairs_with_final(tmp_path):
    """write_metrics_markdown places its file next to the paired final.md."""
    from src import metrics_md
    rr = ResearchRun(
        user_prompt="example prompt",
        decomposition=Decomposition(subquestions=[]),
        findings=FindingsStore(),
    )
    final_path = tmp_path / "final_20260505T120000_abcdef12_example_prompt.md"
    final_path.write_text("placeholder", encoding="utf-8")

    out = metrics_md.write_metrics_markdown(
        rr, eval_results={},
        out_dir=str(tmp_path),
        paired_final_path=str(final_path),
    )
    assert out.exists()
    assert out.parent == tmp_path
    assert out.name == "metrics_20260505T120000_abcdef12_example_prompt.md"
    assert out.read_text(encoding="utf-8").startswith("# Run metrics")


def test_write_metrics_markdown_falls_back_when_final_missing(tmp_path):
    """With no paired_final_path, the writer generates metrics_<stamp>_<uuid8>_<slug>.md."""
    from src import metrics_md
    rr = ResearchRun(
        user_prompt="another example prompt",
        decomposition=Decomposition(subquestions=[]),
        findings=FindingsStore(),
    )
    out = metrics_md.write_metrics_markdown(
        rr, eval_results={},
        out_dir=str(tmp_path), paired_final_path=None,
    )
    assert out.exists()
    assert out.name.startswith("metrics_")
    assert out.suffix == ".md"
    # Prompt slug ends up in the filename for easy directory grepping.
    assert "another_example_prompt" in out.name


def test_render_markdown_writes_a_real_md_file(tmp_path):
    """_render_markdown writes a real .md file with the expected preamble + content."""
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
    # Filename pattern: final_{stamp}_{uuid8}_{slug}.md
    assert out_path.name.startswith("final_")
    assert out_path.suffix == ".md"
    assert "comparative_effectiveness" in out_path.name
    text = out_path.read_text(encoding="utf-8")
    # Preamble + body preserved verbatim.
    assert "Original prompt:" in text
    assert "Detected format:" in text
    assert "comparative effectiveness of X vs Y" in text
    assert "```mermaid" in text
    assert "flowchart LR" in text
    assert "# Heading" in text
