"""KAE — Keypoint-Aligned Evaluation.

Replaces single-number grounding with a triplet:
  KSR (Keypoint Supported Rate) — % of source keypoints the report covers
  KCR (Keypoint Conflict Rate)  — % of source keypoints the report contradicts
  KOR (Keypoint Omission Rate)  — % of source keypoints the report omits

Pipeline: extract atomic keypoints from each cited URL → dedupe across
sources → classify the report's stance on each unified keypoint.

Uses ``cfg.kae_judge_model`` (defaults to a cheaper model than the
eval-wide ``cfg.judge_model``); override via KAE_JUDGE_MODEL env var.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from src.config import Config
from src.llm import chat_json
from src.state import FindingsStore, KAEResult, Report
from src.tools.fetch import fetch as fetch_url


class _KeypointList(BaseModel):
    keypoints: list[str] = Field(
        description="Atomic factual claims the source makes. Each is one "
                    "sentence, specific, verifiable. Skip framing/opinion."
    )


_EXTRACT_SYSTEM = """You extract atomic factual keypoints from a source page.

A keypoint:
- Is ONE specific factual claim the source makes
- Could be checked against other sources
- Is self-contained (no pronoun references)

Skip:
- Framing, opinions, transitions
- Definitions of common terms
- Marketing copy

Aim for 5-12 keypoints per source. Quality over quantity."""


class _StanceCheck(BaseModel):
    stance: Literal["supported", "contradicted", "omitted"]
    reason: str


_STANCE_SYSTEM = """You decide the report's stance on a single source-derived KEYPOINT.

Three possible stances:
- "supported": the report makes a claim consistent with this keypoint
- "contradicted": the report makes a claim that DISAGREES with this keypoint
- "omitted": the report does not address this keypoint at all (neither agrees nor disagrees)

Be strict on contradiction — only mark contradicted if there's a clear factual disagreement, not just different emphasis. Be honest on omission — if the keypoint is genuinely about a topic the report doesn't touch, that's omission, not support."""


def kae_score(
    cfg: Config,
    report: Report,
    findings: FindingsStore,
    max_keypoints_per_source: int = 8,
    fetch_max_chars: int = 16_000,
) -> KAEResult:
    """Run the full KAE pipeline. Returns KSR/KCR/KOR + per-keypoint detail."""
    if not report or not report.claims:
        return KAEResult(ksr=0.0, kcr=0.0, kor=0.0, n_keypoints=0, n_sources=0)

    # 1. Collect cited URLs (deduplicated)
    cited_urls: list[str] = []
    seen = set()
    for c in report.claims:
        for fi in c.supporting_finding_indices:
            if 0 <= fi < len(findings.findings):
                u = findings.findings[fi].source_url
                if u and u not in seen:
                    seen.add(u)
                    cited_urls.append(u)

    if not cited_urls:
        return KAEResult(ksr=0.0, kcr=0.0, kor=1.0, n_keypoints=0, n_sources=0)

    # 2. Extract keypoints per source (UEK = union, dedup'd loosely by string)
    uek: list[tuple[str, str]] = []  # (source_url, keypoint)
    seen_kp: set[str] = set()
    for url in cited_urls:
        page = fetch_url(url, max_chars=fetch_max_chars)
        if page.error or not page.text.strip():
            continue
        try:
            kps = chat_json(
                cfg.anthropic_api_key, cfg.kae_judge_model, _EXTRACT_SYSTEM,
                f"SOURCE PAGE:\n\n{page.text}\n\nExtract up to "
                f"{max_keypoints_per_source} atomic keypoints.",
                _KeypointList, max_retries=1,
            )
        except Exception:
            continue
        for kp in kps.keypoints[:max_keypoints_per_source]:
            norm = " ".join(kp.lower().split())[:120]
            if norm in seen_kp:
                continue
            seen_kp.add(norm)
            uek.append((url, kp))

    if not uek:
        return KAEResult(ksr=0.0, kcr=0.0, kor=1.0, n_keypoints=0,
                         n_sources=len(cited_urls))

    # 3. For each keypoint, classify the report's stance
    report_text = _report_to_prose(report)
    counts = {"supported": 0, "contradicted": 0, "omitted": 0}
    per_kp = []
    for url, kp in uek:
        try:
            check = chat_json(
                cfg.anthropic_api_key, cfg.kae_judge_model, _STANCE_SYSTEM,
                f"REPORT:\n\n{report_text}\n\nKEYPOINT (from {url}):\n{kp}",
                _StanceCheck, max_retries=1,
            )
            counts[check.stance] += 1
            per_kp.append({"source": url, "keypoint": kp,
                           "stance": check.stance, "reason": check.reason})
        except Exception as e:
            counts["omitted"] += 1
            per_kp.append({"source": url, "keypoint": kp,
                           "stance": "omitted", "reason": f"check failed: {e}"})

    n = len(uek)
    return KAEResult(
        ksr=counts["supported"] / n,
        kcr=counts["contradicted"] / n,
        kor=counts["omitted"] / n,
        n_keypoints=n,
        n_sources=len(cited_urls),
        per_keypoint=per_kp,
    )


def _report_to_prose(report: Report) -> str:
    """Flatten the structured report to prose so the stance check can scan it."""
    lines = [report.summary, ""]
    for c in report.claims:
        lines.append(f"- {c.claim} (confidence: {c.confidence:.2f})")
    if report.contradictions_surfaced:
        lines.append("\nContradictions surfaced:")
        for x in report.contradictions_surfaced:
            lines.append(f"- {x.description}")
    if report.caveats:
        lines.append("\nCaveats:")
        for x in report.caveats:
            lines.append(f"- {x}")
    return "\n".join(lines)
