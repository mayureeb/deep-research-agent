"""Hierarchy-aware metrics for taxonomy outputs.

Three diagnostics: ``consolidation_score`` (avg claims per leaf +
singleton fraction), ``path_diversity`` (distinct root-to-leaf paths,
depth), and ``hierarchy_summary`` (combined view + over-segmented flag).
Pure-Python, $0 LLM cost.
"""
from __future__ import annotations

from src.state import TaxonomyNode


def consolidation_score(tree: TaxonomyNode | None) -> dict:
    """Avg claims per leaf + singleton fraction.

    Returns:
        avg_per_leaf:    higher = more consolidated. Healthy ≈ 2-5.
        singleton_frac:  fraction of leaves with exactly 1 claim. High = fragmented.
        n_leaves:        leaf count.
        n_claims:        total claims under tree.
    """
    if tree is None:
        return {"avg_per_leaf": 0.0, "singleton_frac": 0.0,
                "n_leaves": 0, "n_claims": 0}

    leaves = _collect_leaves(tree)
    if not leaves:
        return {"avg_per_leaf": 0.0, "singleton_frac": 0.0,
                "n_leaves": 0, "n_claims": 0}

    claim_counts = [len(leaf.claims) for leaf in leaves]
    n_claims = sum(claim_counts)
    n_singletons = sum(1 for c in claim_counts if c == 1)
    return {
        "avg_per_leaf": n_claims / len(leaves),
        "singleton_frac": n_singletons / len(leaves),
        "n_leaves": len(leaves),
        "n_claims": n_claims,
    }


def path_diversity(tree: TaxonomyNode | None) -> dict:
    """Distinct root-to-leaf path count + max depth.

    A meaningful taxonomy has multiple distinct paths and modest depth.
    Trees that are essentially flat (all paths length 2) are degenerate;
    trees that are essentially linear (one long chain) are also bad.
    """
    if tree is None:
        return {"n_paths": 0, "max_depth": 0, "avg_depth": 0.0}
    paths = tree.all_paths()
    if not paths:
        return {"n_paths": 0, "max_depth": 0, "avg_depth": 0.0}
    depths = [len(p) for p in paths]
    return {
        "n_paths": len(paths),
        "max_depth": max(depths),
        "avg_depth": sum(depths) / len(depths),
    }


def hierarchy_summary(tree: TaxonomyNode | None) -> dict:
    """One call returning the full hierarchy diagnostic set."""
    cons = consolidation_score(tree)
    div = path_diversity(tree)
    return {
        **cons,
        **div,
        # Over-segmentation flag: triggers if singleton_frac > 0.5
        # OR avg_per_leaf < 1.5
        "over_segmented": (
            cons["n_leaves"] > 0
            and (cons["singleton_frac"] > 0.5 or cons["avg_per_leaf"] < 1.5)
        ),
    }


def _collect_leaves(tree: TaxonomyNode) -> list[TaxonomyNode]:
    if tree.is_leaf():
        return [tree]
    out: list[TaxonomyNode] = []
    for ch in tree.children:
        out.extend(_collect_leaves(ch))
    return out
