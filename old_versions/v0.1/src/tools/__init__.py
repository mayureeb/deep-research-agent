"""src.tools — the researcher's 5-tool kit.

Tools (each module exports a tool_schema() returning the Anthropic
tool-use schema):
  * search.py    — web_search       (Tavily)
  * scholar.py   — search_papers    (Semantic Scholar)
  * fetch.py     — fetch_url        (httpx + trafilatura)
  * findings.py  — save_finding + note_uncertainty (structured writes
                                     to the FindingsStore)

Tool dispatch (mapping tool_name → side effect) lives in
src/agent/researcher.py:_dispatch_tool — not in this package — because
each dispatch needs the researcher's context (researcher_id, subq id,
local findings/uncertainty lists) which doesn't generalize.
"""
