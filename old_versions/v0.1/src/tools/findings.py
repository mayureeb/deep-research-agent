"""save_finding + note_uncertainty tool schemas.

The actual side effect (appending to the FindingsStore) happens in
`agent/researcher.py:_dispatch_tool`, which attaches researcher_id and
subquestion_id at dispatch time.
"""
from __future__ import annotations


def tool_schema() -> dict:
    """Anthropic tool_use schema for save_finding."""
    return {
        "name": "save_finding",
        "description": (
            "Record a single claim with its supporting evidence. Use this when "
            "you have read a source and identified a concrete answer to your "
            "sub-question.\n\n"
            "Guidelines:\n"
            "- claim: one sentence, specific, falsifiable. NOT 'the field is complex'.\n"
            "- evidence: a verbatim or near-verbatim quote from the source. "
            "  This is what the critic will check against. Be exact.\n"
            "- source_url: the URL you fetched it from.\n"
            "- confidence: 0.0–1.0. Be honest. 0.9+ only for primary sources with "
            "  unambiguous statements. 0.5–0.7 for inference/synthesis. "
            "  Below 0.5 if the source itself is uncertain.\n\n"
            "Save a finding only after fetching the source — do NOT save findings "
            "based only on search snippets."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "claim": {"type": "string"},
                "evidence": {"type": "string"},
                "source_url": {"type": "string"},
                "source_title": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
            "required": ["claim", "evidence", "source_url", "confidence"],
        },
    }


def uncertainty_tool_schema() -> dict:
    """Anthropic tool_use schema for note_uncertainty."""
    return {
        "name": "note_uncertainty",
        "description": (
            "Record explicitly that you searched for evidence on a topic but "
            "couldn't find anything concrete. Use this WHEN:\n"
            "- you ran 2+ searches on a sub-question and got no useful sources, OR\n"
            "- the sources you found were paywalled / 404 / unusable, OR\n"
            "- the sources you found genuinely don't address the question.\n\n"
            "Do NOT use this as an excuse to skip work — only after you've "
            "actually tried. A note_uncertainty is far more honest than "
            "silently producing no findings on a sub-question.\n\n"
            "topic: what you tried to find evidence on (specific, not vague)\n"
            "reason: why you couldn't (e.g., 'all top results were marketing "
            "blogs, not technical sources', 'arxiv search returned no papers "
            "on X after Y', 'paywalled')"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["topic", "reason"],
        },
    }
