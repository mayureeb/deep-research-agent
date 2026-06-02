"""Final exporter: post-critic Report → user-format renderer.

Detects the user-requested format and conditions from the original
prompt, renders the structured Report into that format, and audits
each condition against the rendering.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import Path

from ..config import Config
from ..llm import chat_json
from ..state import Decomposition, FindingsStore, FinalOutput, Report


EXPORTER_SYSTEM = """You are the FINAL EXPORTER in a deep-research pipeline. The pipeline has already produced a fully-grounded structured Report (claims + cited findings) that has passed a critic loop. Your job is twofold:

1) Read the user's ORIGINAL prompt to determine:
   * What FORMAT the user asked for (e.g. memo, table, bullet list, JSON, narrative summary, comparison matrix). If the prompt doesn't specify, default to a clear narrative summary in markdown.
   * What CONDITIONS the prompt imposes (e.g. "compare on three axes", "at least 5 sources", "under 500 words", "include limitations", "cite peer-reviewed work", "use specific section headers"). Be exhaustive — surface every condition you can find, including implicit ones if they're clearly load-bearing.

2) Render the report's content into the requested format. The content of the rendered output must be drawn from the structured Report's claims (and the supporting findings). Do NOT invent new claims. Do NOT drop claims that are needed to satisfy a condition. You may reword for the format, but the substance comes from the Report.

   The `content` field will be written verbatim to a markdown (.md) file, so emit valid GitHub-flavored markdown. If the format requires a chart, flowchart, sequence diagram, or any other visual, render it as a fenced ```mermaid``` code block (e.g. ```mermaid\\nflowchart LR\\nA-->B\\n```). Do NOT reference external image files or non-mermaid diagram syntaxes — the output is a single markdown file and mermaid is the only diagram language the renderer understands. If the user asked for JSON or another non-markdown format, wrap that payload in a fenced code block with the appropriate language tag.

3) Audit each condition against your rendered content. For each condition emit a ConditionCheck with:
   * status = "satisfied" if the rendered content clearly meets it,
            "partial"   if it partially meets it (explain in notes),
            "not_satisfied" if it fails to meet it,
            "not_applicable" if you can't verify it (e.g. subjective like "compelling argument").
   * evidence: a SHORT pointer into the rendered content (a quote or section name) showing where the condition is met / should have been.
   * notes: optional free-form clarification, especially for partial / not_satisfied.

4) Populate `unmet_conditions` as a flat list of the condition strings whose status is "not_satisfied" OR "partial" — this is the user's at-a-glance "what's missing?" line.

5) Write a one-sentence `notes` field at the top describing the format you inferred and any judgment call you made (e.g. "Original report had no explicit table; synthesized one by grouping claims by sub-question.").

Output a single FinalOutput JSON. Do not output anything outside the JSON."""


def export_final(
    cfg: Config,
    user_prompt: str,
    decomposition: Decomposition,
    findings: FindingsStore,
    report: Report,
) -> FinalOutput:
    """Render the post-critic Report into the user-requested format and
    audit prompt-derived conditions against the rendering."""
    finding_list = findings.findings
    claim_blocks: list[str] = []
    for i, c in enumerate(report.claims):
        cited_evidence_lines: list[str] = []
        for fi in c.supporting_finding_indices:
            if 0 <= fi < len(finding_list):
                f = finding_list[fi]
                cited_evidence_lines.append(
                    f"    [F{fi}] {f.evidence!r} (source: {f.source_url})"
                )
        block = f"  CLAIM {i} (confidence={c.confidence:.2f}): {c.claim}"
        if cited_evidence_lines:
            block += "\n" + "\n".join(cited_evidence_lines)
        else:
            block += "\n    (no cited findings)"
        claim_blocks.append(block)

    sub_q_lines = "\n".join(
        f"  - [{sq.id}] {sq.question}" for sq in decomposition.subquestions
    )
    summary_block = (
        f"\n\nReport summary (writer's prose):\n{report.summary}"
        if report.summary else ""
    )
    caveats_block = (
        "\n\nReport caveats (already surfaced by the pipeline):\n"
        + "\n".join(f"  - {c}" for c in report.caveats)
        if report.caveats else ""
    )

    user_message = (
        f"ORIGINAL USER PROMPT:\n{user_prompt}\n\n"
        f"SUB-QUESTIONS THE PIPELINE RESEARCHED:\n{sub_q_lines}\n\n"
        f"STRUCTURED REPORT (post-critic, all claims grounded):\n"
        + "\n".join(claim_blocks)
        + summary_block
        + caveats_block
    )

    final_output = chat_json(
        cfg.anthropic_api_key,
        cfg.writer_model,
        EXPORTER_SYSTEM,
        user_message,
        FinalOutput,
        max_retries=2,
        max_tokens=16384,
    )

    try:
        md_path = _render_markdown(
            content=final_output.content,
            user_prompt=user_prompt,
            format_detected=final_output.format_detected,
            out_dir=cfg.exporter_md_dir,
        )
        final_output.output_file_path = str(md_path)
    except Exception as e:
        final_output.notes = (
            (final_output.notes + " | " if final_output.notes else "")
            + f"Markdown render failed: {e}"
        )

    return final_output


def _render_markdown(
    *,
    content: str,
    user_prompt: str,
    format_detected: str,
    out_dir: str,
) -> Path:
    """Write `content` to a markdown file under `out_dir`."""
    out_dir_path = Path(out_dir)
    out_dir_path.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "_", user_prompt.lower())[:40].strip("_")
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    filename = f"final_{stamp}_{uuid.uuid4().hex[:8]}_{slug}.md"
    out_path = out_dir_path / filename

    preamble = (
        "<!--\n"
        f"Original prompt: {user_prompt}\n"
        f"Detected format: {format_detected}\n"
        "-->\n\n"
        f"> **Original prompt:** {user_prompt}\n"
        f"> **Detected format:** {format_detected}\n\n"
        "---\n\n"
    )
    out_path.write_text(preamble + content, encoding="utf-8")
    return out_path
