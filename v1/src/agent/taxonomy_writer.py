"""Hierarchical taxonomy writer.

Builds an additional hierarchical view over the flat Report.claims.
Invoked by the eval runner after run_research, not by the orchestrator.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from .. import events
from ..config import Config
from ..llm import chat_json
from ..state import (
    Decomposition,
    FindingsStore,
    Report,
    ReportClaim,
    TaxonomyNode,
)


class _TaxonomyDraft(BaseModel):
    """LLM output: a tree where each leaf names claim indices."""
    label: str
    summary: str = ""
    children: list["_TaxonomyDraft"] = Field(default_factory=list)
    claim_indices: list[int] = Field(
        default_factory=list,
        description="Indices into the flat Report.claims list, for leaf nodes only",
    )


_TaxonomyDraft.model_rebuild()


SYSTEM = """You organize a flat list of report claims into a HIERARCHICAL TAXONOMY.

You receive: the user prompt, the decomposition, and a numbered list of claims (each with confidence).

Produce a tree where:
- The ROOT label is a short version of the user prompt's topic.
- INTERNAL NODES are CATEGORIES grouping related claims. Each has a 1-sentence summary.
- LEAF NODES carry the actual claims (via claim_indices). Every claim from the input MUST appear in exactly one leaf.

CRITICAL design rules — from empirical study of how LLMs organize literature:

1. CONSOLIDATE, don't fragment. Models tend to over-segment (one node per claim).
   Aim for 2-4 categories at the top level, each containing 2-5 claims. A category
   with one claim usually means the category is too narrow.

2. Categories should reflect TOPICAL/METHODOLOGICAL structure, not surface keywords.
   "Approaches" + "Limitations" + "Open problems" beats "RAG techniques" +
   "Vector database approaches" + "Embedding-based retrieval" (those overlap).

3. Tree depth: 2 levels is usually enough. 3 if the topic genuinely has nested
   subcategories. NEVER 4+.

4. Leaf labels should be NOUN PHRASES, not sentences.

5. EVERY claim from the input must end up in exactly ONE leaf's claim_indices.
   No orphans, no duplicates."""


def write_taxonomy(
    cfg: Config,
    user_prompt: str,
    decomp: Decomposition,
    report: Report,
    findings: FindingsStore,
) -> TaxonomyNode | None:
    """Build a hierarchical taxonomy over the report's existing claims."""
    if not report or not report.claims:
        return None

    events.writer("organizing claims into hierarchical taxonomy...")

    decomp_text = "\n".join(
        f"  - [{sq.id}] {sq.question}" for sq in decomp.subquestions
    )
    claims_text = "\n".join(
        f"[{i}] (conf {c.confidence:.2f}) {c.claim}"
        for i, c in enumerate(report.claims)
    )

    user = (
        f"USER PROMPT:\n{user_prompt}\n\n"
        f"DECOMPOSITION (sub-questions):\n{decomp_text}\n\n"
        f"FLAT CLAIMS TO ORGANIZE (use these indices):\n\n{claims_text}\n\n"
        f"Produce a hierarchical taxonomy. Bias toward CONSOLIDATION — "
        f"prefer 2-4 broad categories over many narrow ones."
    )

    try:
        draft = chat_json(
            cfg.anthropic_api_key, cfg.writer_model, SYSTEM, user,
            _TaxonomyDraft, max_retries=2,
        )
    except Exception as e:
        events.writer(f"taxonomy generation failed: {e}")
        return None

    tree = _splice_claims(draft, report.claims)

    seen = _collect_claim_indices(draft)
    expected = set(range(len(report.claims)))
    missing = expected - seen
    duplicates = [i for i in seen if list(_collect_claim_indices(draft)).count(i) > 1]
    if missing:
        events.writer(f"  ⚠ taxonomy missed {len(missing)} claims — appending as 'Other'")
        other = TaxonomyNode(
            label="Other",
            summary="Claims that didn't fit cleanly into the main taxonomy",
            claims=[report.claims[i] for i in sorted(missing)],
        )
        tree.children.append(other)
    if duplicates:
        events.writer(f"  ⚠ taxonomy duplicated {len(duplicates)} claims (kept first occurrence)")

    events.writer(
        f"taxonomy: {tree.n_nodes()} nodes, {len(tree.all_paths())} root-to-leaf paths"
    )
    return tree


def _splice_claims(draft: _TaxonomyDraft, all_claims: list[ReportClaim]) -> TaxonomyNode:
    children = [_splice_claims(ch, all_claims) for ch in draft.children]
    claims = []
    if not children:
        for idx in draft.claim_indices:
            if 0 <= idx < len(all_claims):
                claims.append(all_claims[idx])
    return TaxonomyNode(
        label=draft.label,
        summary=draft.summary,
        claims=claims,
        children=children,
    )


def _collect_claim_indices(draft: _TaxonomyDraft) -> set[int]:
    out = set(draft.claim_indices)
    for ch in draft.children:
        out |= _collect_claim_indices(ch)
    return out
