"""Reproducibility metrics — output stability across multiple runs of the same prompt.

Pairwise Jaccard helpers (claim, sub-question, finding, token-level
variants), plus per-metric variance (``metric_stats``), shape /
sub-question-count selection stability, and unique-source set overlap.
"""
from __future__ import annotations

from src.state import Decomposition, ResearchRun


def claim_jaccard(runs: list[ResearchRun]) -> dict:
    """Pairwise Jaccard over claim text sets across runs."""
    claim_sets = [
        {_normalize(c.claim) for c in (rr.report.claims if rr.report else [])}
        for rr in runs
    ]
    if len(claim_sets) < 2:
        return {"mean_jaccard": 0.0, "pairs": []}

    pairs = []
    for i in range(len(claim_sets)):
        for j in range(i + 1, len(claim_sets)):
            a, b = claim_sets[i], claim_sets[j]
            if not a and not b:
                jac = 1.0
            elif not a or not b:
                jac = 0.0
            else:
                jac = len(a & b) / len(a | b)
            pairs.append({"i": i, "j": j, "jaccard": jac})

    mean = sum(p["jaccard"] for p in pairs) / len(pairs)
    return {"mean_jaccard": mean, "pairs": pairs}


def subquestion_jaccard(runs: list[ResearchRun]) -> dict:
    """Pairwise Jaccard over sub-question text sets across runs."""
    sq_sets = [
        {_normalize(sq.question) for sq in rr.decomposition.subquestions}
        for rr in runs
    ]
    if len(sq_sets) < 2:
        return {"mean_jaccard": 0.0, "pairs": []}

    pairs = []
    for i in range(len(sq_sets)):
        for j in range(i + 1, len(sq_sets)):
            a, b = sq_sets[i], sq_sets[j]
            if not a and not b:
                jac = 1.0
            elif not a or not b:
                jac = 0.0
            else:
                jac = len(a & b) / len(a | b)
            pairs.append({"i": i, "j": j, "jaccard": jac})

    mean = sum(p["jaccard"] for p in pairs) / len(pairs)
    return {"mean_jaccard": mean, "pairs": pairs}


def _normalize(s: str) -> str:
    """Lowercase + collapse whitespace + cap at 200 chars."""
    return " ".join(s.lower().split())[:200]


def _token_set(s: str) -> set[str]:
    """Tokenize via _normalize().split()."""
    return set(_normalize(s).split())


def _best_match_pair_score(a_items: list[set[str]], b_items: list[set[str]]) -> float:
    """Symmetric best-match average between two lists of token sets."""
    if not a_items and not b_items:
        return 1.0
    if not a_items or not b_items:
        return 0.0

    def _jac(x: set[str], y: set[str]) -> float:
        if not x and not y:
            return 1.0
        if not x or not y:
            return 0.0
        return len(x & y) / len(x | y)

    def _best_avg(xs: list[set[str]], ys: list[set[str]]) -> float:
        scores = [max((_jac(x, y) for y in ys), default=0.0)
                  for x in xs if x]
        return sum(scores) / len(scores) if scores else 0.0

    return (_best_avg(a_items, b_items) + _best_avg(b_items, a_items)) / 2.0


def claim_token_jaccard(runs: list[ResearchRun]) -> dict:
    """Pairwise best-match token-Jaccard over the set of CLAIMS across runs."""
    item_lists = [
        [_token_set(c.claim) for c in (rr.report.claims if rr.report else [])]
        for rr in runs
    ]
    if len(item_lists) < 2:
        return {"mean_jaccard": 0.0, "pairs": []}

    pairs = []
    for i in range(len(item_lists)):
        for j in range(i + 1, len(item_lists)):
            jac = _best_match_pair_score(item_lists[i], item_lists[j])
            pairs.append({"i": i, "j": j, "jaccard": jac})

    mean = sum(p["jaccard"] for p in pairs) / len(pairs)
    return {"mean_jaccard": mean, "pairs": pairs}


def subquestion_token_jaccard(runs: list[ResearchRun]) -> dict:
    """Pairwise best-match token-Jaccard over the set of SUB-QUESTIONS across runs."""
    item_lists = [
        [_token_set(sq.question) for sq in rr.decomposition.subquestions]
        for rr in runs
    ]
    if len(item_lists) < 2:
        return {"mean_jaccard": 0.0, "pairs": []}

    pairs = []
    for i in range(len(item_lists)):
        for j in range(i + 1, len(item_lists)):
            jac = _best_match_pair_score(item_lists[i], item_lists[j])
            pairs.append({"i": i, "j": j, "jaccard": jac})

    mean = sum(p["jaccard"] for p in pairs) / len(pairs)
    return {"mean_jaccard": mean, "pairs": pairs}


def finding_token_jaccard(runs: list[ResearchRun]) -> dict:
    """Pairwise best-match token-Jaccard over the set of researcher findings.

    Operates on ``Finding.claim`` (the distilled atomic fact), not
    ``Finding.evidence`` (the verbatim quote).
    """
    item_lists = [
        [_token_set(f.claim) for f in (rr.findings.findings if rr.findings else [])]
        for rr in runs
    ]
    if len(item_lists) < 2:
        return {"mean_jaccard": 0.0, "pairs": []}

    pairs = []
    for i in range(len(item_lists)):
        for j in range(i + 1, len(item_lists)):
            jac = _best_match_pair_score(item_lists[i], item_lists[j])
            pairs.append({"i": i, "j": j, "jaccard": jac})

    mean = sum(p["jaccard"] for p in pairs) / len(pairs)
    return {"mean_jaccard": mean, "pairs": pairs}


def subquestion_overlap(decomp: Decomposition) -> dict:
    """Within-run pairwise text-Jaccard across the planner's sub-questions.

    Surfaces planners that duplicate themselves: ``mean_pairwise_jaccard``
    > 0.4 means two of N sub-questions are essentially asking the same
    thing.
    """
    sqs = decomp.subquestions if decomp else []
    n = len(sqs)
    if n < 2:
        return {
            "mean_pairwise_jaccard": 0.0,
            "max_pairwise_jaccard": 0.0,
            "n_pairs": 0,
            "n_subquestions": n,
        }

    token_sets = [set(_normalize(sq.question).split()) for sq in sqs]

    jaccards = []
    for i in range(n):
        for j in range(i + 1, n):
            a, b = token_sets[i], token_sets[j]
            if not a and not b:
                jac = 1.0
            elif not a or not b:
                jac = 0.0
            else:
                jac = len(a & b) / len(a | b)
            jaccards.append(jac)

    return {
        "mean_pairwise_jaccard": sum(jaccards) / len(jaccards),
        "max_pairwise_jaccard": max(jaccards),
        "n_pairs": len(jaccards),
        "n_subquestions": n,
    }


def shape_selection_stats(runs: list[ResearchRun]) -> dict:
    """Across N runs of the same prompt, distribution of which Shape the
    classifier picked, plus per-shape confidence stats."""
    import statistics
    from collections import Counter, defaultdict

    n_runs = len(runs)
    n_with_shape = sum(1 for rr in runs if rr.shape is not None)
    per_run_shapes = [
        {"shape": rr.shape.shape, "confidence": rr.shape.confidence}
        if rr.shape is not None else None
        for rr in runs
    ]

    if not n_with_shape:
        return {
            "distribution": {},
            "modal_shape": None,
            "modal_share": 0.0,
            "n_runs_with_shape": 0,
            "n_runs": n_runs,
            "mean_confidence": 0.0,
            "stdev_confidence": 0.0,
            "confidence_by_shape": {},
            "per_run_shapes": per_run_shapes,
        }

    shapes_present = [rr.shape for rr in runs if rr.shape is not None]
    counter: Counter[str] = Counter(s.shape for s in shapes_present)
    modal_shape, modal_count = counter.most_common(1)[0]
    confidences = [s.confidence for s in shapes_present]

    by_shape: dict[str, list[float]] = defaultdict(list)
    for s in shapes_present:
        by_shape[s.shape].append(s.confidence)
    confidence_by_shape = {
        shape: {
            "mean": sum(cs) / len(cs),
            "stdev": statistics.pstdev(cs) if len(cs) > 1 else 0.0,
            "min": min(cs),
            "max": max(cs),
            "n": len(cs),
        }
        for shape, cs in by_shape.items()
    }

    return {
        "distribution": dict(counter),
        "modal_shape": modal_shape,
        "modal_share": modal_count / n_runs if n_runs else 0.0,
        "n_runs_with_shape": n_with_shape,
        "n_runs": n_runs,
        "mean_confidence": sum(confidences) / len(confidences),
        "stdev_confidence": (
            statistics.pstdev(confidences) if len(confidences) > 1 else 0.0
        ),
        "confidence_by_shape": confidence_by_shape,
        "per_run_shapes": per_run_shapes,
    }


def subquestion_count_stats(runs: list[ResearchRun]) -> dict:
    """Across N runs of the same prompt, distribution + modal share of
    how many sub-questions the planner produced."""
    import statistics
    from collections import Counter

    n_runs = len(runs)
    counts = [
        len(rr.decomposition.subquestions)
        if rr.decomposition else 0
        for rr in runs
    ]
    if not counts:
        return {
            "distribution": {},
            "modal_count": None,
            "modal_share": 0.0,
            "mean": 0.0,
            "stdev": 0.0,
            "min": 0,
            "max": 0,
            "n_runs": 0,
            "per_run_counts": [],
        }

    counter: Counter[int] = Counter(counts)
    modal_count, modal_n = counter.most_common(1)[0]
    return {
        "distribution": dict(counter),
        "modal_count": modal_count,
        "modal_share": modal_n / n_runs,
        "mean": sum(counts) / len(counts),
        "stdev": statistics.pstdev(counts) if len(counts) > 1 else 0.0,
        "min": min(counts),
        "max": max(counts),
        "n_runs": n_runs,
        "per_run_counts": list(counts),
    }


def unique_source_stats(runs: list[ResearchRun]) -> dict:
    """Across N runs of the same prompt, set-overlap (Jaccard) on
    cited / harvested URL and domain sets."""
    from urllib.parse import urlparse

    def _domain(url: str) -> str:
        if not url:
            return ""
        try:
            host = (urlparse(url).hostname or "").lower()
        except (ValueError, TypeError):
            return ""
        return host[4:] if host.startswith("www.") else host

    def _cited_finding_indices(rr: ResearchRun) -> set[int]:
        if not rr.report or not rr.report.claims:
            return set()
        out: set[int] = set()
        for c in rr.report.claims:
            out.update(c.supporting_finding_indices)
        return out

    def _set_jaccard(sets: list[set[str]]) -> dict:
        if len(sets) < 2:
            return {}
        pairs = []
        for i in range(len(sets)):
            for j in range(i + 1, len(sets)):
                a, b = sets[i], sets[j]
                if not a and not b:
                    jac = 1.0
                elif not a or not b:
                    jac = 0.0
                else:
                    jac = len(a & b) / len(a | b)
                pairs.append({"i": i, "j": j, "jaccard": jac})
        return {
            "mean_jaccard": sum(p["jaccard"] for p in pairs) / len(pairs),
            "pairs": pairs,
        }

    n_runs = len(runs)

    cited_url_sets: list[set[str]] = []
    cited_domain_sets: list[set[str]] = []
    harvested_url_sets: list[set[str]] = []
    harvested_domain_sets: list[set[str]] = []
    per_run_counts: list[dict] = []

    for rr in runs:
        finding_list = rr.findings.findings if rr.findings else []
        h_urls = {f.source_url for f in finding_list if f.source_url}
        h_domains = {_domain(u) for u in h_urls if _domain(u)}
        cited_idx = _cited_finding_indices(rr)
        c_urls = {
            finding_list[i].source_url
            for i in cited_idx
            if 0 <= i < len(finding_list) and finding_list[i].source_url
        }
        c_domains = {_domain(u) for u in c_urls if _domain(u)}

        cited_url_sets.append(c_urls)
        cited_domain_sets.append(c_domains)
        harvested_url_sets.append(h_urls)
        harvested_domain_sets.append(h_domains)
        per_run_counts.append({
            "n_cited_urls": len(c_urls),
            "n_cited_domains": len(c_domains),
            "n_harvested_urls": len(h_urls),
            "n_harvested_domains": len(h_domains),
        })

    return {
        "n_runs": n_runs,
        "cited_url_jaccard":        _set_jaccard(cited_url_sets),
        "cited_domain_jaccard":     _set_jaccard(cited_domain_sets),
        "harvested_url_jaccard":    _set_jaccard(harvested_url_sets),
        "harvested_domain_jaccard": _set_jaccard(harvested_domain_sets),
        "per_run_unique_counts":    per_run_counts,
    }


def metric_stats(per_run: list[dict]) -> dict:
    """Per-metric mean / stdev / min / max across N runs.

    Booleans are aggregated as fraction-true. Metrics absent from every
    run are omitted from the output.
    """
    import statistics

    if not per_run:
        return {}

    keys = set()
    for r in per_run:
        keys.update(r.keys())

    out: dict[str, dict] = {}
    for k in sorted(keys):
        vals = [r.get(k) for r in per_run]
        present = [v for v in vals if v is not None]
        if not present:
            continue
        if isinstance(present[0], bool):
            true_count = sum(1 for v in present if v)
            out[k] = {
                "fraction_true": true_count / len(present),
                "n": len(present),
            }
            continue
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in present):
            continue
        out[k] = {
            "mean": statistics.fmean(present),
            "stdev": statistics.pstdev(present) if len(present) > 1 else 0.0,
            "min": min(present),
            "max": max(present),
            "n": len(present),
        }
    return out
