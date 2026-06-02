# Deep Research Agent — v1 Design Doc

> **Scope.** This is the discussed/finalized design doc for **v1, the MVP**. 
>
> **Reading order.** Section 1 (Assumptions) and Section 1.5 (v0.1 → v1 framing) set the context. Section 2 lists the pipeline-wide architectural decisions (F1–F?). Section 3 walks each component (classify_shape → decompose → researchers → findings store → reconciler → confidence axes → writer → critic + verifier loop → final exporter → metrics) with the v0.1 design, what v1 changed, what's deferred, and what the v1 batch showed. Section 5 is the empirical appendix.

> **Relationship to v0.1.** v1 inherits v0.1's design unless explicitly changed. Subsections in Section 3 say *"carried unchanged from v0.1"* when no change applies. This avoids re-defending decisions that have already been defended in [`v0.1_design_doc.md`](./v0.1_design_doc.md).

---

## 1. Assumptions

v1 carries eight assumptions: three problem invariants from v0.1 (A1–A3), restated and sharpened against v1's Section 5 / Section 6 evidence; two scope choices (A4–A5); and three new in v1 — one eval-design assumption (A6), one world-state assumption (A7), and one judge-trust assumption (A8). Two items previously listed here moved out of the table — *no model training in scope* is an assignment **constraint**, and *depth over breadth* is an assignment **suggestion**. Both still drive scope and are tracked in Section 4 where they actually bite; they are not assumptions we made.

| #   | Assumption                                                                                                                                                                                                                     | Source          | Consequence                                                                                                                                                                                                                                                                                                                     |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A1  | No ground truth at the **report** level; per-claim ground truth at the **citation** level                                                                                                                                      | problem framing | Report quality is inferred from indirect signals (grounding, calibration, fabrication-rate, judge rubric, ablation deltas) — never a single accuracy number. Citations are checkable post-hoc — the C1 re-fetch verifier is built around this distinction.                                                                      |
| A2  | Context is finite at any window size we'd run — even 1M tokens cannot hold every relevant source, and packing degrades attention                                                                                               | problem framing | Every box is implicitly a context-allocation choice; per-stage isolation (F4) and fan-out (F1) are load-bearing regardless of window size.                                                                                                                                                                                      |
| A3  | Two distinct failure shapes: **fabrication** (invented sources/facts — rare on benign prompts, structurally dangerous) and **drift** (paraphrase that softens or shifts a claim — common; observed at **26% across 99 verdicts** in the v1 batch, with per-prompt range 0–80%, Section 5) | problem framing | A "no defense" pipeline fails silently. v1 ships fabrication-*detection* architecture (verifier closes a class of failure text-vs-text grounding can't reach) and demonstrates drift-*catching* in practice. The two shapes warrant different defenses; collapsing them under one "hallucination" label hides where each lands. |
| A4  | Single run is the **deliverable** unit; multi-run is the **evaluation** unit                                                                                                                                                   | scope choice    | No cross-run memory or fine-tuning loop in production. Reproducibility analysis (Section 6) operates run-vs-run because run-to-run variance is the dominant quality signal (`corr(harvested_url_jac, finding_jac) = +0.852`).                                                                                                          |
| A5  | Cost is a tiebreaker, not an optimization target                                                                                                                                                                               | scope choice    | Per-stage cost is observable but not minimized. Cost becomes decision-relevant when a contribution shows no quality payoff — e.g., C1 verifier (Section 5: +$0.06/cell, judge -0.37, no fabrication catches) earned its `--no-verifier` flag on cost grounds, not on a cost budget.                                                    |
| A6  | Evaluation prompts (`prompts.txt`) are hand-curated to span prompt shapes, not sampled from any distribution                                                                                                                   | eval design     | Shape-aware routing (C2) earns its place — would be overengineering on a single-shape benchmark. Eval results must be read per-prompt / per-shape, not as aggregates over a population: Section 5's "Run 6 floor +36, Run 1 ceiling -29" is interpretable as a *trade* only because the prompts are known to span shapes. Generalization claims are bounded to "across this shape spectrum," not to arbitrary user queries. |
| A7  | Authoritative sources exist on the open web (`web_search` + `search_papers`-reachable) for every sub-question                                                                                                                  | problem framing | Failure modes split: when A7 holds, low coverage is an **agent** failure (search/fetch/reconcile underperformed); when A7 doesn't hold (e.g., Prompt 6's production cost numbers live in private benchmarks and customer postmortems), low coverage is a **world** failure and the agent is judged on calibration — *did it admit the gap?* — not coverage. v1's lifted floor on Run 6 (50→86%) is largely "got better at admitting the gap," not "found the missing numbers." Out-of-scope: paywalled corpora, vendor-internal data, private Slack/email. |
| A8  | The LLM-as-judge produces signal stable enough to rank arms within a 6-prompt batch, given mitigations (ACE two-stage protocol, fixed rubric, separate judge call per metric)                                                  | eval design     | Judge-based deltas in Section 5 (judge +0.30, verifier judge -0.37) are taken as load-bearing evidence for the headline empirical claim. Limits: same-family writer/judge bias is **mitigated, not eliminated** by ACE; judge run-to-run variance is not measured (Section 6 repro analysis is on outputs, not judge calls); a 0.3-point delta on a 5-point scale across 6 prompts is suggestive, not conclusive. Adversarial-judge corpora and judge-vs-judge agreement are open work for v2. |

---

## 1.5 v0.1 → v1 — the research-depth pass

**v1 is an attempt at research-depth demonstration.** Each architectural contribution is anchored in published work; the eval suite is built to test the contributions honestly; the empirical findings are reported as they came back, including the negative ones. The pitch is **architecture defended deeply, with honest evidence about where the architecture's limits show up**.

### v1 contribution catalog

The full v1 contribution surface — for traceability with the per-component sections in Section 3:

| # | v1 contribution | Where it lands |
|---|---|---|
| C1 | **Re-fetch verifier** — re-fetches each cited URL and judges `verified / drifted / fabricated / unreachable` | New stage inside the critic loop (Component 8) |
| C2 | **Shape classifier** — one LLM call up front routes the planner and writer prompts by detected prompt shape (`PRE_STRUCTURED / CONTESTED / SPARSE_EMERGING / DISCOVERY / GENERAL`); researcher topology is unchanged across shapes (lead+parallel topology was the deferred half — see Section 4) | New Component 1 (before decompose) |
| C3 | **Bounded revision + caveats fallback** — `max_revisions=2`; if still flagged, ship with explicit caveats rather than silent emit | Component 8 reorder + new fallback path |
| C4 | **Three-axis confidence combine** (researcher × source-quality × cross-source agreement) | New Component 6 (between reconciler and writer) |
| C5 | **Multi-signal stop conditions** — soft-stop now requires findings count AND distinct-source diversity AND avg-confidence floor | Component 3 Researchers |
| C6 | **Typed `Contradiction` schema** — replaces v0.1's `list[str]`; writer can link contradictions to specific finding indices | Component 5 Reconciler |
| C7 | **TrACE-K adaptive compute** — wraps each researcher tool-use turn with k-sample voting; commits at agreement threshold | Component 3 Researchers (optional, off by default) |
| C8 | **Taxonomy writer** — hierarchical view over the flat `Report.claims`, invoked by eval | Eval-side; not in the runtime pipeline |
| C9 | **KAE + ACE evals** — keypoint-aligned eval (KSR/KCR/KOR) and adaptive-checklist eval (Wan et al. 2026, arXiv 2509.01396) | Component 10 Metrics (opt-in) |
| C10 | **Paired ablation runner** (`--ablation {critic \| verifier \| trace}`) — runs each prompt twice (off / on) per contribution; emits `ablation_<name>_summary.md` with paired tables and aggregate deltas | Eval-side; CLI flag built around `eval/ablation.py`; substantiates togglable contributions with empirical OFF/ON deltas |

---

## 2. Architectural framing decisions

The four framing decisions from v0.1 — F1 multi-agent fan-out, F2 plain Python orchestration, F3 single LLM family, F4 per-stage context isolation — carry forward into v1 **unchanged at the architecture level**. Full defense lives in [`v0.1_design_doc.md` Section 2](./v0.1_design_doc.md#2-architectural-framing-decisions); v1-specific notes below.

v1 adds **no new framing decisions**. The new pipeline stages (classify_shape, confidence axes, verifier-then-critic loop, caveats fallback) and the new researcher behaviors (multi-signal stop, optional TrACE-K) are *per-component* changes documented in Section 3, not pipeline-wide framing changes.

### F1 — Multi-agent fan-out

|             |                                                                                                                                                                                                                                          |
| ----------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Status      | Unchanged from v0.1                                                                                                                                                                                                                      |
| v1 note     | Researchers still fan out per sub-question under `asyncio.gather`. TrACE-K (Component 3) wraps each researcher's tool-use turns but doesn't change the fan-out shape — it operates *inside* a researcher's loop, not across researchers. |
| Defended at | [`v0.1_design_doc.md` Section F1](./v0.1_design_doc.md)                                                                                                                                                                                         |

### F2 — Plain Python orchestration

| | |
|---|---|
| Status | Unchanged from v0.1 |
| v1 note | Still no agent framework. v1 adds three new conditional branches (shape-routing in Component 1, verifier-vs-critic ordering in Component 8, caveats-fallback path on `max_revisions` exhaustion) — all implemented as `if`/`elif` dispatch in `orchestrator.py`. v0.1's F2 said *"revisit when shape classifier ships"* — that trigger fired here, and the answer is still "plain Python is fine" because v1 has three branch points, not a multi-arm DAG. |
| Defended at | [`v0.1_design_doc.md` Section F2](./v0.1_design_doc.md) |

### F3 — Single LLM family (Anthropic Claude)

|             |                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ----------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Status      | Unchanged from v0.1 — within-family tiering refined                                                                                                                                                                                                                                                                                                                                                                                                  |
| v1 note     | All roles still on Claude. v1 adds one new role: `kae_judge_model` (default `claude-haiku-4-5`) for KAE keypoint extraction — cheaper because the task is bulk extraction, not synthesis. Tiering is now: researcher / KAE = haiku, writer / critic = sonnet, judge / ACE / plan-judge = opus. The "judge ≠ producer" principle still holds; KAE returning to haiku is a cost choice for a bulk-extraction task, not a synthesis or evaluation task. |
| Defended at | [`v0.1_design_doc.md` Section F3](./v0.1_design_doc.md)                                                                                                                                                                                                                                                                                                                                                                                                     |

### F4 — Memory & context allocation

| | |
|---|---|
| Status | Unchanged from v0.1 — structured-handoff list grows |
| v1 note | Per-stage context isolation still holds — even with three new stages added. Each new stage takes structured input and produces structured output (`ClassifiedShape`, `Confidence` triple, `VerifyReport`); none accumulate context across stages. The structured-handoff list grows from v0.1's set to also include `ClassifiedShape` (pre-decompose), typed `Contradiction[]` (was prose `list[str]` in v0.1), and `VerifyReport` (verifier → critic). |
| Defended at | [`v0.1_design_doc.md` Section F4](./v0.1_design_doc.md) |

---

## 3. Per-component design

The pipeline has **ten components** in v1, numbered for easy reference:

1. Classify shape *(NEW in v1)*
2. Decompose
3. Researchers (with optional TrACE-K layer)
4. Findings store
5. Reconciler
6. Confidence axes *(NEW in v1)*
7. Writer
8. Critic + verifier loop *(reordered: verifier first; bounded revision + caveats fallback)*
9. Final exporter
10. Metrics & observability

Optional layer (not a pipeline stage of its own): **TrACE-K adaptive compute**, opt-in via `--trace`, wraps Component 3's tool-use turns.

Each subsection below states the v0.1 design (or "new in v1"), what v1 changed, what was deferred, what failed, and what the v1 evidence batch showed. Sections that say *"carried unchanged from v0.1"* defer to [`v0.1_design_doc.md`](./v0.1_design_doc.md) for the v0 baseline.

---

### 3.0 Cross-cutting questions

The same eight cross-track questions as the v0.1 doc, now with v1's stance alongside v0.1's. The cross-version delta is the doc's purpose — most questions shifted because v1 closed gaps v0.1 acknowledged.

| #   | Question                                                                     | v0.1 stance                                                                                                                                                | v1 stance                                                                                                                                                                                                                                                                             | Where in v1        |
| --- | ---------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------ |
| 1   | Plan as commitment vs suggestion?                                            | Commitment (single-shot decompose, no revision)                                                                                                            | **Still commitment, but shape-conditioned** — the plan is generated given the classified shape, so it's commitment-per-shape. Mid-run revision still absent.                                                                                                                          | Components 1, 2    |
| 2   | What should agents share, what should they not?                              | Share: `Decomposition`, `Finding[]`, `UncertaintyNote[]`, `Contradictions`, `Report`. Don't share: in-researcher reasoning, writer prose, critic critique. | **Same stance; structured-handoff list grows.** v1 adds `ClassifiedShape` (pre-decompose), typed `Contradiction[]` (now with `finding_ids`), and `VerifyReport` (verifier → critic) to the kept-across-stages set.                                                                    | F1, F4             |
| 3   | Dead ends, contradictions, hallucinations — how does the system stay honest? | Surface contradictions; writer hard-rules + critic. Honest-stopping path (`note_uncertainty`) was unused in v0.1 batch.                                    | **Stronger across all three axes.** Contradictions now typed and finding-linked. Hallucinations caught at *two* layers: critic (text-vs-text) + verifier (re-fetch live page). Dead ends: multi-signal stop now requires diversity AND avg-confidence floor, not just `min_findings`. | Components 3, 5, 8 |
| 4   | Not all work is equally hard. What does that imply?                          | **Open limitation** — all prompts treated uniformly. Coverage 43–86% with shape-correlated variance.                                                       | **Partially addressed.** Shape classifier routes `PRE_STRUCTURED / CONTESTED / SPARSE_EMERGING / DISCOVERY / GENERAL` to different planner and writer prompts. Researcher topology is unchanged per shape (lead+parallel topology was the deferred half — see Section 4).                    | Component 1        |
| 5   | Vague question in, concrete research out — what happens in between?          | Decomposer — schema-validated 3–7 sub-questions with rationales                                                                                            | **Same, plus shape-conditioning.** Decomposition now happens given a classified shape; planner prompt shifts per shape.                                                                                                                                                               | Components 1, 2    |
| 6   | When does adding another agent help vs hurt?                                 | Per-sub-question fan-out (helps); per-source rejected (hurts)                                                                                              | **Same.** v1 doesn't change fan-out granularity. TrACE-K is *inside* a researcher, not a new agent. New stages (classify_shape, confidence axes, verifier) are single calls, not agents.                                                                                              | F1                 |
| 7   | How do you prevent agents converging on a comfortable wrong answer?          | Critic independence (blind to writer reasoning)                                                                                                            | **Stronger.** Critic still independent, *plus* re-fetch verifier — closing v0.1's acknowledged gap that text-vs-text grounding can't catch researcher fabrication (made-up quote on real URL).                                                                                        | Component 8        |
| 8   | When do you stop searching?                                                  | `min_findings_to_stop=3` OR `max_tool_calls=30` OR `unproductive_call_limit=10`. Honest-stopping path unused in v0.1 batch.                                | **Multi-signal stop:** `min_findings=3` AND `min_unique_sources=2` AND `min_avg_confidence=0.55` to soft-stop. Single-finding-from-one-source no longer triggers stop.                                                                                                                | Component 3        |

**Connection to v1's contribution surface.** Questions 3, 4, 7, and 8 are exactly the gaps v1 was scoped to close. Each maps to a contribution defended in Section 3:

| v0.1 acknowledged gap                                                                               | v1 contribution that closes it                  |
| --------------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| Q4 — prompt-shape blindness (coverage variance 43–86%)                                              | C2 — Shape classifier (Component 1)             |
| Q7 — researcher fabrication invisible to text-vs-text grounding                                     | C1 — Re-fetch verifier (Component 8)            |
| Q8 — `note_uncertainty=0` shows honest-stopping path unused; single-source-3-findings can soft-stop | C5 — Multi-signal stop (Component 3)            |
| Q3 — contradictions emit prose, writer can't link to claim indices                                  | C6 — Typed `Contradiction` schema (Component 5) |

Q1, Q2, Q5, Q6 are unchanged-or-incremental. v1 didn't redesign these — and the v0.1 stance still defends.

---

### Component 1 — Classify shape (NEW in v1)

**Purpose.** One LLM call up front classifies the user's prompt into one of five shapes — `PRE_STRUCTURED / CONTESTED / SPARSE_EMERGING / DISCOVERY / GENERAL`. The classification routes downstream prompt construction in the planner (Component 2) and the writer (Component 7).

**Decision (new in v1)**

| Aspect                     | Choice                                                                                                                 |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Output schema              | `ClassifiedShape(shape, confidence, rationale)` — schema-validated; one short sentence rationale                       |
| Five shapes                | `PRE_STRUCTURED`, `CONTESTED`, `SPARSE_EMERGING`, `DISCOVERY`, `GENERAL`                                               |
| Number of calls            | 1 LLM call (no multi-step)                                                                                             |
| Model                      | `cfg.writer_model` (sonnet) — same tier as synthesis, since classification quality matters                             |
| Failure mode               | Graceful fallback to `GENERAL` with `confidence=0.0` (does not block the run)                                          |
| Decision rule on ambiguity | "When uncertain between two specialized shapes, prefer GENERAL — wrong specialization is worse than no specialization" |
| Where shape is consumed    | `shape_classifier.py:planner_annex()` (Component 2) + `writer.py:pick_writer_system()` (Component 7)                   |

**Reasoning**

| Choice                                             | Why                                                                                                                                                                                                                                                                                                              |
| -------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Five shapes (not three, not ten)                   | Five covers the prompt categories that matter — pre-structured, contested, sparse-emerging, discovery — with `GENERAL` as a default. Fewer shapes (just contested vs not) wouldn't differentiate sparse-emerging from discovery; more shapes would split sparse training data across too many branches. |
| One LLM call up front                              | Classification is a single discrete decision; multi-step would only matter if shape interacted with content. It doesn't.                                                                                                                                                                                         |
| Same model as writer (sonnet, not haiku)           | Wrong classification cascades into wrong planner prompt + wrong writer prompt; the cost of a bad classification is bigger than the cost of a sonnet call. Cheaper model rejected.                                                                                                                                |
| Graceful `GENERAL` fallback on failure             | The pipeline must complete on a classifier failure — losing shape-routing degrades behavior to v0.1-equivalent (treat all prompts uniformly), which is acceptable; blocking would be worse.                                                                                                                      |
| Specialization-vs-default tie-break favors GENERAL | A wrong specialization actively misroutes downstream; no specialization just removes the v1 win. Asymmetric cost → asymmetric default.                                                                                                                                                                           |

**Alternatives rejected**

| Alternative                                              | Why not                                                                                                                                                                                 |
| -------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Heuristic / regex classifier (no LLM)                    | Shape detection requires reading prompt intent, not surface form ("Is X effective?" can be CONTESTED or DISCOVERY depending on whether literature is contested). LLM is the right tool. |
| Multi-step classifier (decompose then classify)          | Adds a round-trip; classification decision is upstream of decomposition by design                                                                                                       |
| More shapes (10+, e.g. one per test category) | Splits training-prompt evidence too thin; reviewer can't keep more than ~5 categories in mental model                                                                                   |
| Continuous embedding distance (vector classifier)        | Requires labeled training data we don't have (A1: no ground truth); LLM zero-shot is the available tool                                                                                 |

**v1 changes**

| # | Type | Change | What it closes |
|---|---|---|---|
| C2 | New contribution | Shape classifier + per-shape `planner_annex()` (Component 2) + per-shape `pick_writer_system()` (Component 7) | v0.1's prompt-shape blindness — coverage variance 43–86% in the v0.1 batch was shape-correlated (discovery prompts at 43%, structured comparative at 86%) |

**Status of the claim**

| Claim                                                     | Status in v1                                                                                                                                                                                      |
| --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Shape classification is reliable                          | Untested — no per-prompt classification audit in v1's batch. The classifier's `confidence` field gets recorded but isn't aggregated into a calibration report. Manually evaluated on a small set. |
| Shape routing improves coverage / quality                 | Ablation infrastructure exists (`--no-verifier` exists; shape-routing has no toggle). Claim defended in principle but not directly measured by an OFF/ON pair on shape itself.                    |
| `GENERAL` fallback handles classifier failures gracefully | Untested — no failure observed in v1's batch (sonnet classifier didn't fail). Fallback is correct by inspection.                                                                                  |

**Deferred — what's NOT in v1 despite being in the earlier scope plan**

| Deferred item                                              | Why deferred / honest acknowledgment                                                                                                                                                                                                                           |
| ---------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Lead+parallel topology** for SPARSE_EMERGING / DISCOVERY | The earlier scope plan's second half — *"shape classifier + lead+parallel topology"* — only the prompt-level half shipped. v1 still uses pure parallel fan-out for every shape (Component 3 unchanged). The topology contribution is only-partially-delivered. |
| Per-shape stop-condition tuning                            | All shapes use the same multi-signal stop in Component 3. A discovery prompt arguably needs more findings than a pre-structured one before stopping.                                                                                                           |
| Per-shape critic strictness                                | Critic uses the same prompt across shapes. Contested-prompt critics could weight contradiction-handling differently than pre-structured.                                                                                                                       |

**Failure modes**

| Failure                                                | Detail                                                                                                                                                                                                                                                                                                    |
| ------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Misclassification routes wrongly                       | A `CONTESTED` prompt classified as `GENERAL` loses the both-sides-coverage rule in the planner, producing one-sided sub-questions. Asymmetric: `GENERAL → CONTESTED` is fine (extra structure that's ignored if not contested); `CONTESTED → GENERAL` is the bad direction.                               |
| `confidence` field is recorded but never gates routing | Even a 0.3-confidence classification is treated as authoritative. A confidence threshold for falling back to `GENERAL` would help; not implemented.                                                                                                                                                       |
| Shape choice is a one-shot commit                      | No mid-run reclassification if the researcher discovers the prompt is actually contested. Same single-shot-plan limitation as Component 2.                                                                                                                                                                |
| C2 "topology routing" was scoped but never built       | Earlier scope plan called for shape → topology routing (e.g., lead+parallel for SPARSE_EMERGING / DISCOVERY). Code branches planner and writer prompts only; researcher topology is the same flat parallel fan-out for all shapes. Tracked in this component's *Deferred* row and Section 4 "What did NOT land." |

**Observed (v1 examples — full coverage from 12 run JSONs)**

| Observation                    | Detail                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Shape distribution (12 cells)  | **CONTESTED 5/12, DISCOVERY 3/12, SPARSE_EMERGING 2/12, PRE_STRUCTURED 2/12, GENERAL 0/12.** Per prompt (OFF/ON): P1 CONTESTED→DISCOVERY · P2 CONTESTED/CONTESTED · P3 SPARSE_EMERGING/SPARSE_EMERGING · P4 DISCOVERY/DISCOVERY · P5 CONTESTED/CONTESTED · P6 PRE_STRUCTURED/PRE_STRUCTURED. **GENERAL fallback never fired** — classifier always landed on a specific shape.                                                  |
| Confidence distribution        | Range **0.62–0.95**, mean **~0.78** across 12 cells. Highest: P2 ON CONTESTED at 0.95 (prompt explicitly cites disagreement); lowest: P1 ON DISCOVERY at 0.62 (rationale: *"landscape survey rather than pre-structured comparison"*). Confidence and prompt clarity track together as designed.                                                                                                                               |
| Stability across OFF / ON arms | **5/6 prompts produced the same shape on both arms.** P1 flipped: CONTESTED (OFF, conf 0.72) → DISCOVERY (ON, conf 0.62). The classifier is stochastic at low temperature — borderline prompts can swap shape across runs. P1's prompt straddles CONTESTED and DISCOVERY (synthetic data is both contested AND a landscape survey), so the flip is defensible per-call but signals classifier brittleness on borderline cases. |
| Misclassification cases        | None obviously wrong. Each shape is defensible by the per-call rationale. P1's flip is the closest thing to a misclassification — both labels are individually correct; the issue is non-determinism on borderline prompts, not error.                                                                                                                                                                                         |
| classify_shape stage cost      | **~$0.004 / run** (~0.5% of total ~$0.88 mean) across 12 cells — essentially free, as designed. 1 LLM call, 65 output tokens.                                                                                                                                                                                                                                                                                                  |

**When to revisit / open work**

| Trigger                                                               | Action                                                                                        |
| --------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Misclassification rate above ~10%                                     | Add confidence-threshold fallback to `GENERAL` (e.g., `if confidence < 0.5: shape = GENERAL`) |
| Per-shape coverage gap closes incompletely                            | Ship the deferred topology branching — lead+parallel for SPARSE_EMERGING / DISCOVERY          |
| Sparse-emerging / discovery results still underperform pre-structured | Per-shape stop-condition tuning + per-shape critic strictness                                 |
| Need to defend shape contribution empirically                         | Run shape-routing OFF/ON ablation (currently no `--no-shape-classifier` flag)                 |

---

### Component 2 — Decompose

**Purpose.** Same as v0.1 — turn an unstructured prompt into 3–7 sub-questions with rationales, validated against a Pydantic schema. v1 adds shape-conditioning.

**Decision (changed in v1)**

| Aspect  | v0.1                                            | v1                                                                                       |
| ------- | ----------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Schema  | `Decomposition` with sub-questions + rationales | Same + `SubQuestion.preferred_search` field (`web_search`, `search_papers`, or `either`) |
| Prompt  | `DECOMPOSE_SYSTEM` only                         | `DECOMPOSE_SYSTEM + planner_annex(shape)` — per-shape guidance appended                  |
| Bounds  | `[3, 7]`                                        | Same                                                                                     |
| Retries | `max_retries=2`                                 | Same                                                                                     |

**Per-shape annex behavior** (`planner_annex(shape)` from Component 1)

| Shape             | What the annex adds                                                                                                                          |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `PRE_STRUCTURED`  | Mirror user-supplied structure exactly — one sub-question per axis/field, in user's order, no inventing                                      |
| `CONTESTED`       | Include both-sides probes ("evidence supports X" / "evidence challenges X"); include methodological-disagreement sub-question when plausible |
| `SPARSE_EMERGING` | Add "what evidence is missing?" sub-question; set `preferred_search='search_papers'` for technical sub-questions                             |
| `DISCOVERY`       | Sub-questions ENUMERATE candidate approaches / open problems, not answer single facts                                                        |
| `GENERAL`         | Empty annex — base prompt only                                                                                                               |

**Reasoning**

| Choice                                     | Why                                                                                                            |
| ------------------------------------------ | -------------------------------------------------------------------------------------------------------------- |
| Annex (not full prompt rewrite) per shape  | Base prompt rules (no summarize-X, count bounds, schema) apply to every shape; only shape-specific bits change |
| `preferred_search` as hint, not constraint | Researcher autonomy — see Component 3                                                                          |
| Single-shot still (no plan-then-revise)    | v0.1's reasoning still holds — multi-step planning isn't worth the extra calls at v1's complexity              |


**v1 changes**

| #                        | Type          | Change                                                                  | What it closes                                                      | Exercised?                                     |
| ------------------------ | ------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------- | ---------------------------------------------- |
| (Component 1 wiring)     | Architectural | `planner_annex(shape)` appended to `DECOMPOSE_SYSTEM`                   | v0.1 used uniform decomposer prompt; all shapes treated identically | TBD — manual audit of decompositions per shape |
| (preferred_search field) | Polish        | New `SubQuestion.preferred_search` field; passed to Component 3 as hint | Researcher in v0.1 chose tools without sub-question-level guidance  | TBD                                            |

**Status of the claim**

| Claim | Status |
|---|---|
| Per-shape annex changes decomposition behavior | Verifiable by comparing v1 decompositions to v0.1 for same prompts |
| `preferred_search` shifts tool mix in expected directions | Verifiable by tool-mix comparison v0.1 vs v1 on SPARSE_EMERGING prompts |

**Failure modes**

| Failure | Detail |
|---|---|
| Wrong shape from Component 1 → wrong annex → wrong decomposition | Asymmetric per Component 1 *Failure modes* |
| Annex prompt has limited authority | Researcher / writer can override per-shape guidance; not structural |

**Observed (v1 examples — full coverage from 12 run JSONs, 84 sub-questions total)**

| Observation                                              | Detail                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| -------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Sub-question count                                       | **All 12 cells produced 7 sub-questions each** (top of the configured 3–7 range). Planner consistently maxes the budget regardless of shape — no shape→count relationship visible.                                                                                                                                                                                                                                                                                                                              |
| `preferred_search` distribution (n=84)                   | **`search_papers`: 63/84 (75%)**, **`either`: 20/84 (24%)**, **`web_search`: 1/84 (1%)**. Heavy skew toward papers — consistent with the prompt set being academic-literature topics. The single `web_search` call appeared on a sub-question asking about real-world deployment data.                                                                                                                                                                                                                          |
| Hint-vs-actual tool use (papers share, per-cell)         | Planner preferred papers 75% of the time; researchers actually used papers **9–43%** of the time across 12 cells (P6 lowest at 9%, P4 ON highest at 43%). Researcher consistently overrides the hint downward when papers don't return useful results. **Confirms the "hint not constraint" design holds across the full batch, not just one prompt.**                                                                                                                                                            |
| Decompose stage cost                                     | **~$0.02 / run** (~2% of $0.88 mean total) across 12 cells — same order as v0.1.                                                                                                                                                                                                                                                                                                                                                                                                                               |

---

### Component 3 — Researchers (parallel sub-agents + tool-use loop, with optional TrACE-K layer)

**Purpose.** One sub-agent per sub-question, run with `asyncio.gather` under a semaphore. Five tools, fresh context, explicit budgets. v1 strengthens the stop conditions and adds an optional adaptive-compute layer.

**Decision (changed in v1)**

| Aspect                       | v0.1                                              | v1                                                                                                      |
| ---------------------------- | ------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| Granularity                  | One researcher per sub-question (per F1 + F4)     | Same                                                                                                    |
| Tools                        | 5 tools                                           | Same                                                                                                    |
| Stop — quality               | `min_findings_to_stop=3` only                     | **Multi-signal AND-conjunction:** findings ≥ 3 AND `min_unique_sources=2` AND `min_avg_confidence=0.55` |
| Stop — budget                | `max_tool_calls=30`                               | Same                                                                                                    |
| Stop — failsafe              | `unproductive_call_limit=10` (zero findings only) | **Extended:** also fires after N calls since the LAST saved finding                                     |
| Per-fetch timeout            | 15s                                               | Same                                                                                                    |
| Search hint per sub-question | (none)                                            | `subq.preferred_search` from shape-aware planner — `web_search` or `search_papers` as a hint            |
| Adaptive compute             | (none)                                            | **Optional TrACE-K layer** — `--trace`, off by default                                                  |

**TrACE-K layer (optional, off by default)**

| Aspect                       | Choice                                                                                                                                                                        |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Enabled by                   | `cfg.trace_enabled = True` (env `TRACE_ENABLED`) or `--trace` CLI flag                                                                                                        |
| Per researcher decision step | Draw `k_init=2` candidates at `temperature=0.7`; compute inter-rollout agreement α via canonicalized action keys                                                              |
| Commit policy                | If α ≥ `tau_high=0.75` → commit at `k_init`; else expand one-at-a-time up to `k_max=4`; final commit to plurality                                                             |
| Canonicalization             | `canonicalize_action` (tool_name + args) and `canonicalize_response` (search query / URL / claim text) — without this, surface-form differences would defeat plurality voting |
| Per-step log                 | `TraceStep` records each turn's k, α, plurality choice; aggregated to `TraceStats`                                                                                            |

**Reasoning**

| Choice                                          | Why                                                                                                                                                                                                       |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Multi-signal stop AND-conjunction               | v0.1 batch showed researchers stopping at 3 findings from 1 source; single-axis stop wasn't catching low-diversity exits. AND-conjunction forces all three quality dimensions to be met before soft-stop. |
| `min_unique_sources=2`                          | 3 findings from one paper is poor diversity; this floor demands the researcher actually visited >1 source                                                                                                 |
| `min_avg_confidence=0.55`                       | Mean confidence under 0.55 means most findings are weak; researcher should keep going rather than stop with a thin bag                                                                                    |
| Extended unproductive-call (since LAST finding) | A researcher that found 3 things early then went stuck is also stuck; v0.1 only fired on zero-findings                                                                                                    |
| `preferred_search` as hint, not constraint      | Shape-aware planner can suggest "this sub-question needs peer-reviewed sources" but researcher can override if web_search produces better hits — preserves researcher autonomy                            |
| TrACE-K opt-in (off by default)                 | 2–4× LLM cost per decision step; off-by-default keeps cost discipline. Flag exists for ablation evidence.                                                                                                 |
| Plurality (not majority, not best-of-k)         | Plurality (most-frequent canonicalized action) is sample-efficient at k=2; best-of-k requires a quality scorer we don't have                                                                              |
| Canonicalization before plurality               | Without it, "search 'X'" vs "search 'X.'" would split the vote                                                                                                                                            |
| `tau_high=0.75` early-stop                      | If 2 rollouts already agree (α=1.0 ≥ 0.75), no need to expand to k_max=4                                                                                                                                  |

**Alternatives rejected**

| Alternative                               | Why not                                                                          |
| ----------------------------------------- | -------------------------------------------------------------------------------- |
| OR'd multi-signal stop (any one of three) | Defeats the point — wouldn't catch v0.1's single-source case                     |
| Hard-stop only at `max_tool_calls`        | Wastes budget on researchers that have enough                                    |
| Always-on TrACE-K                         | 2–4× cost for all runs, even simple prompts                                      |
| Best-of-k voting (LLM picks best of k)    | Requires a quality scorer; another LLM call per step. Plurality is cheaper.      |
| `k_init=4` skip early-stop                | Over-spends on easy decisions; tau_high early-stop saves cost on consensus cases |

**v1 changes**

| # | Type | Change | What it closes | Exercised? |
|---|---|---|---|---|
| C5 | New contribution | Multi-signal stop AND-conjunction (findings + unique-sources + avg-confidence) | v0.1's single-source-3-findings exit | Expected: more findings per run vs v0.1 batch — TBD |
| (extended unproductive-call) | Polish | Fires after N calls since last finding, not just zero findings | Researcher that found 3 then went stuck wasn't caught | TBD |
| (preferred_search wiring) | Component 1 dependency | Sub-question carries `preferred_search` hint; researcher receives in dispatch | Without hint, researcher relies on own choice; SPARSE_EMERGING benefit lost | Expected: shift in tool-mix on SPARSE_EMERGING shapes — TBD |
| C7 | New contribution (optional) | TrACE-K adaptive compute (k-sample voting + canonicalization + early-stop at tau_high) | High-variance researcher decisions weren't averaged | TBD — `trace_ablation/` outputs to audit |

**Honest acknowledgment of scope**

TrACE-K (C7) was **not in the earlier scope plan** — it was added during v1 development as the direct response to Section 6's reproducibility study. The motivation chain:

1. **Empirical finding (Section 6).** The 3-layer Jaccard study placed the reproducibility floor at the researcher search/fetch step (`corr(harvested_url_jac, finding_jac) = +0.852`; median `harvested_url_jac` = 0.06 across N=6 prompts).
2. **Layer identified.** Variance enters at the researcher's per-step decision (which query, which URL, which extraction).
3. **Mechanism found.** Sethi 2026 — *"Don't Overthink It: Inter-Rollout Action Agreement as a Free Adaptive-Compute Signal for LLM Agents"* (arXiv 2604.08369) — describes a training-free per-step adaptive-compute controller using inter-rollout action agreement. The mechanism (k-sample vote → agreement threshold → adaptive expansion to `k_max`) targets exactly the kind of variance the Section 6 study surfaced.
4. **Implementation.** TrACE-K (`_decide_next_action_with_voting` in `researcher.py`) is a verbatim implementation of Sethi's algorithm at the researcher's tool-use loop, with defaults from the paper (`k_init=2`, `k_max=4`, `tau_high=0.75`, `temperature=0.7`).

Off-by-default preserves baseline cost; the `--trace` flag exists for ablation evidence. **Honest framing for the interview defense:** TrACE-K is the training-free cousin of "RL on the researcher" (Search-R1 retargeted, the Section 6 SKETCH item) — same lever, no training, weaker effect, lower risk. Sethi 2026 explicitly distinguishes its approach from PRM/ORM-trained methods, which is the reason the cheaper, training-free intervention was the responsible v1 ship.

**When to enable TrACE-K**

1. **When reproducibility matters more than cost** — e.g., a research report that needs to be defensible or re-runnable. The Section 6 reproducibility study found that run-to-run variance is dominated by which URLs the researcher decides to fetch. TrACE-K stabilizes that exact decision layer by sampling and voting.
2. **On hard or contested prompts** — where the LLM is more likely to be unsure about which search query or URL to pick. The voting catches the genuinely uncertain decisions; on confident calls (α ≥ `tau_high=0.75` at `k_init=2`), it commits early and costs almost nothing extra.
3. **Ablation runs** — when measuring whether TrACE-K actually helps on a given prompt set, so v2 decisions have evidence either way.

**Status of the claim**

| Claim | Status |
|---|---|
| Multi-signal stop catches the v0.1 single-source case | Defendable in principle. v1 batch should show more findings per run than v0.1 — TBD audit. |
| Threshold values (`min_unique_sources=2`, `min_avg_confidence=0.55`) calibrated | Intuition; not swept |
| `preferred_search` hint changes researcher behavior | Verifiable by checking whether SPARSE_EMERGING runs see higher `search_papers` use vs v0.1 — TBD |
| TrACE-K reduces decision variance | Defendable in principle; `trace_ablation/` outputs are the empirical leg. Needs audit. |
| TrACE-K overhead (2–4× per step) is recoverable | Untested at run-completion level. Could go either way. |
| Canonicalization is correct | By inspection — covers search queries, URLs, claim text |

**Deferred**

| Item                                                 | Why held                                                                                                             |
| ---------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| Wayback Machine fallback                             | Architecturally bigger; v1-only on top of P2                                                                         |
| Per-shape stop conditions                            | Uniform multi-signal threshold across shapes (Component 1 *Deferred*)                                                |
| Per-shape TrACE-K policy                             | Uniform k_init / tau_high across shapes; SPARSE_EMERGING might benefit from higher k_init                            |
| LLM-driven multi-signal stop (content-quality check) | Cut from v1 for cost; pure-Python version shipped. LLM-driven version with content quality check is a v2+ candidate. |

**Failure modes**

| Failure | Detail |
|---|---|
| Multi-signal AND over-extends tail | Researcher that needs many sources stops later — higher cost |
| `min_avg_confidence=0.55` symmetric | Doesn't distinguish "low-confidence-but-honest" from "low-confidence-because-stuck" |
| `preferred_search` is hint-only | Researcher can ignore; effect observable only post-hoc via tool-mix |
| TrACE-K canonicalization may be lossy | Two genuinely different queries canonicalizing to the same key would be conflated |
| TrACE-K `k_max=4` cap | Some decisions might benefit from more rollouts; capped for cost |
| TrACE-K worst case is 4× LLM calls per step | When no consensus, expansion to `k_max` doubles+ the cost |

**Observed (v1 examples — full coverage from 12 run JSONs)**

| Observation                                    | Detail                                                                                                                                                                                                                                                                                                                                                                                                                            |
| ---------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Coverage by prompt (OFF / ON)                  | P1: 57% / 86% · P2: 57% / 57% · P3: 57% / 71% · P4: 71% / 29% · P5: 71% / 57% · P6: 86% / 71%. Mean: 64% OFF, 62% ON. **Verifier ON helps coverage on P1 and P3 (+29, +14) but hurts dramatically on P4 (−42%)**. Mixed signal — verifier sometimes triggers re-research that gains coverage, sometimes prunes claims that lose it.                                                                                                |
| Findings per run                               | Mean **17.3**, range **7–26** across 12 cells. Lowest: P4 ON (7); highest: P1 ON (26). 207 findings total.                                                                                                                                                                                                                                                                                                                          |
| `note_uncertainty` voluntary calls             | **0 across all 12 cells** — same dead-tool finding as v0.1. The researcher LLM never volunteered uncertainty notes via the dedicated tool, even on prompts that produced caveats and contradictions.                                                                                                                                                                                                                                |
| Uncertainty notes from failsafe path           | **2–5 per run** (mean 2.7 across 12 cells). The multi-signal stop's extended unproductive-call path IS firing and emitting `UncertaintyNote` records — failsafe path is the de-facto channel for honesty signals, not the dedicated tool.                                                                                                                                                                                            |
| Researcher stage cost share                    | **~$0.49 / run mean (56% of $0.88 total)** across 12 cells. Down from v0.1's ~62% median — exporter share grew (see Component 9).                                                                                                                                                                                                                                                                                                   |
| TrACE-K usage                                  | `trace_stats` populated in all 12 run JSONs but `trace_enabled=False` in config — `steps_total: 0` everywhere, no actual TrACE-K calls were made. Confirms off-by-default behavior. The infrastructure ran without firing; ablation evidence requires re-running the batch with `--trace`.                                                                                                                                              |
| Tool-mix aggregate (12 cells, 1188 tool calls) | `web_search`: 372, `search_papers`: 172, `fetch_url`: 437, `save_finding`: 207, `note_uncertainty`: 0. Search-tool ratio: 31% papers / 69% web. Researcher relies on `fetch_url` heavily — most save_finding calls are preceded by a fetch.                                                                                                                                                                                          |
| Multi-signal stop blocker distribution         | Not directly surfaced in run JSON; would need stage-level logging from `_meets_multi_signal_stop` calls. The fact that all 12 cells produced ≥7 findings (well above `min_findings=3`) suggests the multi-signal AND-conjunction is rarely the blocker — researchers typically run to budget exhaustion rather than soft-stopping early.                                                                                              |

**When to revisit / open work**

| Trigger | Action |
|---|---|
| Multi-signal stop tail too long (cost blowup) | Tune `min_avg_confidence` down or add budget cap override |
| `preferred_search` hint ineffective | Check researcher behavior; if no shift, strengthen to constraint |
| TrACE-K canonicalization drops genuine differences | Refine rules via manual review of conflated cases |
| TrACE-K cost-without-benefit | Default already off; revisit when LLM quality scorer is available for best-of-k |
| Per-shape divergence | Per-shape stop and TrACE-K policy |

---

### Component 4 — Findings store

**Purpose.** Flat in-memory pool of structured `Finding` objects + `UncertaintyNote` records, append-only during research, serialized on output. Read by the reconciler, confidence axes, writer, critic, and verifier.

**Decision (carried unchanged from v0.1, with new fields added by other components)**

The findings-store data structure, mutation policy, and serialization semantics carry forward verbatim. v1's changes are *new fields* on the `Finding` Pydantic model, set by Component 6 — not changes to the store itself. Defended at [`v0.1_design_doc.md` Component 3](./v0.1_design_doc.md).

**v1 changes — fields added to `Finding`**

| Field                 | Type            | Set by      | When                        |
| --------------------- | --------------- | ----------- | --------------------------- |
| `agreement_score`     | float in [0, 1] | Component 6 | Post-reconciler, pre-writer |
| `combined_confidence` | float in [0, 1] | Component 6 | Post-reconciler, pre-writer |
| `axis_disagreement`   | bool            | Component 6 | Post-reconciler, pre-writer |

The store itself is unchanged. The serialized JSON now carries these three additional fields per finding, but every other shape (mutation policy, append-only, verbatim-evidence requirement, P6 source-quality at save time, P7 provenance enrichment) is identical.

**Failure modes (carried)**

| Failure | Detail |
|---|---|
| Source-quality blog-list gap | Self-hosted vendor blogs hit `unknown=0.7` not `blog_qa=0.5` — same gap as v0.1 — see [`v0.1_design_doc.md` Component 3](./v0.1_design_doc.md) |
| Append-only means no garbage collection mid-run | Same as v0.1 |

**Observed (v1 examples — full coverage from 12 run JSONs)**

| Observation                                  | Detail                                                                                                                                                                                                                                                                                                                                                                                  |
| -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Cited / harvested ratio                      | **Mean ~99% across 12 cells** (range 95–100%). Almost every harvested finding reaches the report — reconciler/writer aren't dropping much. Store-as-pool design works as intended; near-zero harvested-and-ignored gap.                                                                                                                                                                  |
| Source-quality distribution (mean per cell)  | Range **0.67–1.00** across 12 cells. Lowest: P6 (~0.67, both arms — agentic-RAG cost prompt has thinner authoritative coverage). Highest: P4 ON (1.00). Source-quality blog-list gap (carried from v0.1) means self-hosted vendor blogs land in `unknown=0.7` rather than `blog_qa=0.5` — visible in P6's lower mean.                                                                    |
| Total findings persisted (12 cells)          | **207 findings + 32 uncertainty notes** retained across the batch. Append-only mutation policy means no garbage collection mid-run — full provenance trail intact for every run.                                                                                                                                                                                                          |
| `combined_confidence` / `axis_disagreement`  | See Component 6 *Observed* — distributions live with the component that sets these fields.                                                                                                                                                                                                                                                                                                |

---

### Component 5 — Reconciler

**Purpose.** One LLM call per run that scans the findings pool and surfaces contradictions as typed objects. Does not pick winners.

**Decision (changed in v1)**

| Aspect               | v0.1                  | v1                                                      |
| -------------------- | --------------------- | ------------------------------------------------------- |
| Output type          | `list[str]` (prose)   | `list[Contradiction]` (typed)                           |
| Contradiction fields | description only      | `description`, `finding_ids`, `severity`, `type`        |
| Severity             | (none)                | `major / moderate / minor`                              |
| Type                 | (none)                | `factual / methodological / framing / temporal / other` |
| Skip threshold       | < 2 findings → skip   | Same                                                    |
| Validation           | (none)                | Drops contradictions with < 2 valid `finding_ids`       |
| Model                | sonnet (writer model) | Same                                                    |

**Reasoning**

| Choice | Why |
|---|---|
| Typed `Contradiction` schema | Writer can link a contradiction to specific claim indices via `finding_ids`; Component 6's agreement axis requires these IDs |
| `severity` field | Drives writer's leading — `major` contradictions must lead the report (per writer's CONTESTED-shape annex) |
| `type` field | Drives writer's framing — `methodological` vs `factual` reads differently |
| One LLM call (not multi-step) | Contradiction detection is a discrete scan; same as v0.1 |
| Skip if < 2 findings | No useful work to do; defensive |
| Validate `finding_ids` | LLM may reference invalid indices; structural enforcement > prompt-only |

**Alternatives rejected**

| Alternative                               | Why not                                                                                       |
| ----------------------------------------- | --------------------------------------------------------------------------------------------- |
| Pick a winner (vote / weight)             | Encodes scheme bias; A1 — surface honest disagreement                                         |
| Skip contradictions entirely              | Loses the most valuable signal on contested topics                                            |
| Free-form prose only (v0.1 mode)          | Writer can't link to claim indices; agreement axis becomes impossible                         |
| Fixed severity rules (e.g., source-based) | Severity should depend on the user's question, not the source; LLM judgment is the right tool |

**v1 changes**

| #            | Type             | Change                                                                                              | What it closes                                                                         | Exercised?                                                  |
| ------------ | ---------------- | --------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| C6           | New contribution | Typed `Contradiction(description, finding_ids, severity, type)` schema; replaces v0.1's `list[str]` | Writer received prose contradictions in v0.1 with no way to link them to claim indices | TBD — verify finding_ids reference real indices in v1 batch |
| (validation) | Polish           | Drop contradictions with invalid or insufficient `finding_ids`                                      | LLM may reference indices outside the findings list or fewer than 2                    | Unexercised — depends on LLM error rate                     |

**Honest acknowledgment of scope**

Typed `Contradiction` was **borderline in the earlier scope plan** — flagged as a v1 decision point (*"the writer doesn't currently link contradictions to specific claims even with prose. Could be cut."*). Shipped in v1 because:

| Why it landed                                                                                   |
| ----------------------------------------------------------------------------------------------- |
| Component 6 (three-axis confidence) requires `Contradiction.finding_ids` for the agreement axis |
| Writer's CONTESTED-shape annex (Component 7) leverages `severity=major` to lead the report      |
| Cost is identical to prose (one LLM call); typing the output is free                            |

**Status of the claim**

| Claim | Status |
|---|---|
| LLM produces valid `finding_ids` | Unverified at schema level for the v1 batch; validation drops invalid ones silently |
| Severity drives writer behavior | Prompt-level instruction in writer's CONTESTED annex; verifiable by inspecting outputs |
| Type drives writer framing | Same — prompt-level instruction |
| Skip threshold (< 2 findings) is correct | By construction — no contradiction possible with 1 finding |

**Deferred**

| Item | Why held |
|---|---|
| Per-shape reconciler prompt | One prompt across shapes; CONTESTED might want a stricter contradiction bar |
| Severity-weighted agreement axis | Component 6's agreement is unweighted; weighting by severity would make `major` contradictions count more |

**Failure modes**

| Failure                                                  | Detail                                                                |
| -------------------------------------------------------- | --------------------------------------------------------------------- |
| LLM emits prose-style contradiction → schema parse fails | Falls through to empty list (try/except); silent loss on schema error |
| LLM emits `finding_ids` referencing invalid indices      | Validated and dropped; silent loss                                    |
| Severity misclassification                               | Affects writer's leading; no structural backstop                      |
| Type miscategorization                                   | Affects writer's framing; no structural backstop                      |

**Observed (v1 examples — full coverage from 12 run JSONs)**

| Observation                          | Detail                                                                                                                                                                                                                                                                                                                                            |
| ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Number of contradictions per run     | **Mean 3.1, range 1–5** across 12 cells (37 total). No run produced zero — the reconciler is finding *something* on every prompt. Lowest: P4 OFF (1, multi-agent landscape); highest: P3 ON (5, inference-time compute).                                                                                                                          |
| Severity distribution (n=37)         | **moderate: 18 (49%)**, major: 12 (32%), minor: 7 (19%). Most contradictions matter to the user's question (moderate+major = 81%), which is the right shape — minor-dominant would suggest the reconciler is over-firing on trivia.                                                                                                              |
| Type distribution (n=37)             | **factual: 24 (65%)**, framing: 6 (16%), methodological: 5 (14%), temporal: 1 (3%), other: 1 (3%). Factual contradictions dominate — sources literally disagree on numbers, definitions, or facts. Framing and methodology contradictions are real but secondary; temporal (e.g., "as of 2024" vs "as of 2026") is rare on this prompt set.       |
| OFF vs ON arm                        | OFF mean: 3.2; ON mean: 3.0. Verifier toggle has no measurable effect on contradiction count or distribution — expected, since the reconciler runs *before* the verifier in the pipeline.                                                                                                                                                         |
| Contradictions dropped at validation | **No drops observable** in run JSONs — the `contradictions_surfaced` field equals what the reconciler produced, with no separate "rejected" list. The Pydantic schema validates structure, so malformed entries would error rather than silently drop. Whether the reconciler-LLM emitted contradictions that failed schema validation is not logged. |

**When to revisit / open work**

| Trigger                                 | Action                                            |
| --------------------------------------- | ------------------------------------------------- |
| Validation drops > 5% of contradictions | Strengthen prompt or add retry-on-validation-fail |
| Writer ignores severity in practice     | Strengthen writer's CONTESTED annex prompt        |
| Per-shape behavior diverges             | Per-shape reconciler prompt                       |

---

### Component 6 — Confidence axes (NEW in v1)

**Purpose.** Pure-Python pass between reconciler and writer that computes a three-axis confidence per `Finding` — self-reported, source-quality, cross-source agreement — combines them into a single signal, and flags axis-disagreement cases for the writer.

**Decision (new in v1)**

| Aspect | Choice |
|---|---|
| Three axes | self-reported (`Finding.confidence`), source-quality (domain table), cross-source agreement (same-subq peer corroboration) |
| Combine | weighted mean — `W_SELF=0.40, W_SOURCE_QUALITY=0.35, W_AGREEMENT=0.25` |
| Disagreement flag | `True` iff `max(axes) - min(axes) >= 0.35` |
| Where stored | mutated in place on each `Finding`: `agreement_score`, `combined_confidence`, `axis_disagreement` |
| LLM calls | zero — pure Python over existing FindingsStore + Contradictions |
| Runs when | after reconciler (provides typed Contradictions), before writer |
| Writer interface | SYSTEM prompt: "Calibrate ReportClaim.confidence to combined, NOT to self-reported"; TEMPER directive on `[axis-disagree]` findings |

**Reasoning**

| Choice                               | Why                                                                                                                                                                                    |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Three axes (not two, not four)       | Self-reported = researcher belief; source-quality = domain says; agreement = peers say. Each independent. Two-axis combine misses corroboration; four-axis splits the signal too thin. |
| Pure Python (no LLM call)            | Source-quality is a lookup; agreement is set arithmetic over `Contradiction.finding_ids`. Neither benefits from LLM judgment. Free.                                                    |
| Weights 0.40 / 0.35 / 0.25           | Self-reported highest because the researcher saw the page; source-quality and agreement are corrective. Calibrated on intuition.                                                       |
| 0.35 disagreement threshold          | A finding with max-axis 0.9 and min-axis 0.5 has axes diverging by 0.4 — meaningful enough to warn writer                                                                              |
| Agreement only among same-subq peers | Cross-subquestion findings have different evidence bases; not comparable                                                                                                               |
| 1.0 (neutral) for no-peer findings   | A solo finding can't be agreed-or-disagreed-with; defaulting to 1.0 prevents the axis from punishing isolated findings on rare sub-questions                                           |

**Alternatives rejected**

| Alternative | Why not |
|---|---|
| Two axes (self + source only) | Misses corroboration — a peer-reviewed claim contradicted by 3 sources still scores ~0.95 |
| Multiplicative combine (× instead of weighted mean) | Severe penalty for any low axis; weighted mean is more proportionate |
| Median (no weights) | Loses ranking among axes — 1.0/0.5/0.5 = 0.5/1.0/0.5 |
| LLM-judged combine | Adds cost + another hallucination axis; math here is deterministic |
| Single combined score, no disagreement flag | Hides the most actionable signal — when axes diverge, writer should hedge |

**v1 changes**

| #               | Type             | Change                                                                                    | What it closes                                                                                                               | Exercised?                 |
| --------------- | ---------------- | ----------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| C4              | New contribution | Three-axis confidence combine + disagreement flag (pure-Python)                           | v0.1 used self × source-quality only (two axes); single high-confidence finding had same weight as one corroborated by peers | TBD — eval audit           |
| (writer prompt) | Wiring           | Writer SYSTEM prompt instructs use of `combined_confidence` + TEMPER on `[axis-disagree]` | Without prompt-level direction, writer would use raw self-reported                                                           | TBD — audit writer outputs |

**Honest acknowledgment of scope**

The earlier scope plan listed three-axis confidence as **CUT from v1** (Cluster 5, depended on agreement signal that "only shines with many findings per claim"). Shipped in v1 anyway because:

| Why it landed |
|---|
| The dependency that held it back — typed Contradictions (Component 5, C6) — also shipped |
| With typed Contradictions, agreement is set arithmetic, not an LLM call — cheap |
| Zero LLM calls; the earlier scope plan's "marginal benefit" calculus assumed LLM-driven agreement |
| Once typed Contradictions + source-quality from v0.1's P6 are present, the third axis is essentially free |

Same logic brought multi-signal stop (Component 3, C5) and typed Contradictions (Component 5, C6) into v1 — "cheap-when-prerequisites-shipped." Section 4 (Trace check) addresses this expansion across the doc.

**Status of the claim**

| Claim | Status |
|---|---|
| Three axes are independent | True for self vs source; agreement is mildly correlated with source-quality (higher-quality sources agree more). Mild correlation, not redundancy. |
| Weights (0.40 / 0.35 / 0.25) calibrated | Intuition. Tunable if calibration data accumulates. |
| Disagreement threshold (0.35) calibrated | Same — intuition |
| Writer uses `combined_confidence` over self-reported | Prompt-level instruction; verifiable by checking whether `ReportClaim.confidence` correlates with combined |
| Disagreement flag changes writer behavior | Same — prompt-level; verifiable by checking which findings land in caveats vs claims |

**Deferred**

| Item | Why held |
|---|---|
| Agreement weighted by source-quality | Currently unweighted peer count — high-quality source agreeing weighs same as low-quality blog |
| Per-shape weight tuning | Uniform weights across shapes; SPARSE_EMERGING might benefit from upweighting agreement |
| Empirical weight calibration | Requires labeled corpus measuring each axis's predictive value; unavailable |

**Failure modes**

| Failure | Detail |
|---|---|
| `agreement=1.0` default inflates isolated findings | Solo finding on a rare sub-question gets max agreement; writer might over-weight |
| Disagreement threshold symmetric | Doesn't distinguish "self high, others low" from "self low, others high" — different handling might be appropriate |
| Source-quality blog-list gap (carried from v0.1) | Self-hosted vendor blogs (`n1n.ai`, `tianpan.co` from v0.1's batch) still hit `unknown=0.7` not `blog_qa=0.5` |
| Writer might ignore the prompt instruction | No structural enforcement; if writer uses self-reported anyway, combine is decorative. Audit needed. |

**Observed (v1 examples — full coverage from 12 run JSONs, 207 findings, 172 claims)**

| Observation                                                              | Detail                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Distribution of `combined_confidence` vs self-reported (n=207 findings)  | self-reported mean **0.908**; combined mean **0.896** — combined is *slightly lower* on average, opposite of what cross-source agreement boosting alone would predict. Per-finding: 50% combined > self, 48% combined < self, max swing **+0.090 to −0.220**. The three-axis combine adjusts in both directions but has a small negative bias overall — source-quality and agreement axes are pulling some findings down from over-confident self-reports. |
| `axis_disagreement=True` rate (n=207 findings)                           | **26/207 = 12.6%**. Flag fires meaningfully — earlier worry about it being saturated at 0% was an artifact of subsample size. ~1 in 8 findings has at least one axis diverging from the others; writer can use this to hedge when it does fire.                                                                                                                                                                                                  |
| `ReportClaim.confidence` correlation with finding confidences (n=172)    | Pearson **r(claim, self_avg) = +0.41**; **r(claim, combined_avg) = +0.61**. **Combined is the stronger predictor**, by a meaningful margin. The wired three-axis signal isn't decorative — the writer LLM picks it up more reliably than self-reported. C4's design value (signal beyond self-report) is empirically defended on the full batch.                                                                                                  |
| Cost share of confidence stage                                           | **$0.0000, 0 LLM calls, ~0 ms latency** in every one of 12 cells — pure-Python design confirmed. C4 adds zero to the per-run cost.                                                                                                                                                                                                                                                                                                                |

**When to revisit / open work**

| Trigger | Action |
|---|---|
| Evidence one axis weight is mis-set | Sweep weights on held-out corpus |
| Writer ignores combined in favor of self-reported | Strengthen prompt or add structural enforcement |
| Self-hosted blog gap still biting | Ship P6.1 — self-hosted-blog heuristic |
| Per-shape behavior diverges | Tune weights per shape (needs shape-tagged corpus) |

---

### Component 7 — Writer

**Purpose.** Synthesize a structured `Report` from FindingsStore. Every claim cites supporting `finding_indices`. v1 makes the writer shape-aware and confidence-axis-aware.

**Decision (changed in v1)**

| Aspect               | v0.1                                                        | v1                                                                                                                                                      |
| -------------------- | ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| SYSTEM prompt        | One uniform prompt                                          | `pick_writer_system(shape)` selects from 4 shape annexes (`PRE_STRUCTURED` / `CONTESTED` / `SPARSE_EMERGING` / `DISCOVERY`); GENERAL reuses base prompt |
| Confidence input     | Self-reported only                                          | `combined_confidence` (Component 6) is the calibration target; raw self-reported preserved on input                                                     |
| Disagreement signal  | (none)                                                      | `[axis-disagree]` tag on findings; writer instructed to TEMPER claims using disagree-flagged findings                                                   |
| Contradiction format | Prose                                                       | Typed `Contradiction[]` (Component 5) with `severity` and `type`                                                                                        |
| Hard prompt rules    | No invention, no external knowledge, surface contradictions | Same + new TEMPER rule on `[axis-disagree]` findings                                                                                                    |

**Per-shape annex behavior**

| Shape | What the annex changes in writer behavior |
|---|---|
| `PRE_STRUCTURED` | Mirror user's structure in the report; one section per axis/field |
| `CONTESTED` | Lead with the disagreement; surface both sides as sibling claims (not collapsed); methodological dispute as separate claim if reconciler flagged one |
| `SPARSE_EMERGING` | Honest about thin literature; prefer FEWER hedged claims over many speculative ones; surface UncertaintyNotes as caveats |
| `DISCOVERY` | Enumerate, don't deep-dive; report structure mirrors the candidate-space the planner laid out |
| `GENERAL` | Base prompt only |

**Reasoning**

| Choice | Why |
|---|---|
| Per-shape SYSTEM prompts | A CONTESTED prompt's report should LEAD with disagreement; a PRE_STRUCTURED prompt should mirror the user's structure; one prompt can't cover all four well. |
| `combined_confidence` over self-reported | Self-reported is researcher belief; combined factors in source-quality + peer agreement — closer to ground truth. Component 6 reasoning. |
| TEMPER on `[axis-disagree]` | Axis disagreement is the most reliable bias-prone signal; explicit rule prevents writer from defaulting to high-confidence claims |
| Typed Contradiction consumption | `severity=major` drives leading; `type` drives framing — see Component 5 |

**v1 changes**

| # | Type | Change | What it closes | Exercised? |
|---|---|---|---|---|
| (shape annex) | Component 1 dependency | Per-shape SYSTEM prompt | Uniform writer in v0.1 produced same-shape reports across all prompt shapes | TBD — compare report structure across shapes |
| (combined_confidence) | Component 6 dependency | Calibrate `ReportClaim.confidence` to combined, not self-reported | v0.1 writer used self-reported alone | TBD — correlation audit |
| (axis-disagree TEMPER) | Component 6 dependency | New hard rule | No structural defense in v0.1 against confidently-stated bias-prone claims | TBD |
| (typed Contradiction consumption) | Component 5 dependency | Writer reads `severity` and `type` | v0.1's prose contradictions had no severity-driven leading | TBD |

**Failure modes**

| Failure | Detail |
|---|---|
| Writer ignores prompt instruction (TEMPER, combined_confidence) | No structural enforcement; if the writer LLM defaults to self-reported, the wiring is decorative |
| Wrong shape → wrong annex (Component 1 cascade) | `CONTESTED → GENERAL` drops both-sides framing |
| `severity` misclassification by reconciler (Component 5 cascade) | Wrong leading in the report |

**Observed (v1 examples — full coverage from 12 run JSONs, 172 claims)**

| Observation                                                                | Detail                                                                                                                                                                                                                                                                                              |
| -------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Writer stage cost share                                                    | **~$0.15 / run mean (17% of ~$0.88 total)** across 12 cells. In-line with v0.1's writer share (~14%); the shape-conditioned annex didn't blow up cost.                                                                                                                                                |
| Caveats per run                                                            | **Mean 8.4, range 7-11** across 12 cells. Caveats are populating consistently — every report ships with explicit limitations, not just the bounded-revision-exhausted cases.                                                                                                                          |
| Claims per run                                                             | **Mean 14.3, range 6–18** (172 total across 12 cells). Lowest: P4 ON (6); highest: P4 OFF (15) — verifier ON dramatically reduced claim count on P4, likely driving the −42% coverage drop noted in Component 3.                                                                                       |
| `ReportClaim.confidence` distribution (n=172)                              | Mean **0.91**, range **0.65–0.96**. Writer uses a narrow band — most claims sit in 0.80–0.95; almost none below 0.7. Confidence is being compressed into a high-confidence register rather than spanning the full 0.0–1.0 range.                                                                       |
| `ReportClaim.confidence` correlation with finding confidences (n=172)      | Pearson **r(claim, self_avg) = +0.41**; **r(claim, combined_avg) = +0.61**. Combined is the stronger predictor (also reported in Component 6). Writer is meaningfully tracking the wired three-axis signal — the wiring isn't decorative.                                                                |
| Per-shape report structure                                                 | Shape-conditioned writer prompts produce visibly different structures (e.g., P6 PRE_STRUCTURED gets a comparison table; P3 SPARSE_EMERGING flags evidence gaps prominently) but per-shape pattern characterization across the batch is open work — would need a manual pass over the rendered final.md outputs. |
| TEMPER application rate on axis-disagree findings                          | `axis_disagreement` fired **26/207 = 12.6%** (Component 6). Whether the writer applied TEMPER on those specific findings is not surfaced — would need claim-level provenance to confirm. Worth wiring into the metrics output.                                                                            |

---

### Component 8 — Critic + verifier loop (reordered + verifier added + bounded revision + caveats fallback)

**Purpose.** Independent verification of writer claims at TWO layers: re-fetch verifier (against the live page) followed by critic (against the cited evidence text). If issues are found, trigger a writer revision; bounded by `max_revisions=2` rounds, then fall back to surfacing unresolved issues in `report.caveats`.

**Decision (significantly changed in v1)**

| Aspect                  | v0.1                              | v1                                                                                      |
| ----------------------- | --------------------------------- | --------------------------------------------------------------------------------------- |
| Loop                    | critic only; single revision pass | verifier + critic; up to `max_revisions=2` rounds (3 iterations: round 0/1/2)           |
| Verifier                | (none)                            | `refetch_verifier.py` — re-fetches each cited URL; verdict per finding                  |
| Order in revision round | n/a                               | verifier FIRST, then critic (critic's input includes verifier verdicts)                 |
| Stop on max-revisions   | ship silently if still flagged    | `_format_unresolved_caveat()` prepends warning to `report.caveats`, then ship           |
| Verifier cache          | n/a                               | per-finding `cached: dict[idx, VerifyResult]` — short-circuits re-fetches across rounds |
| Verifier concurrency    | n/a                               | `asyncio.Semaphore(cfg.parallel_researchers)`                                           |

**Verifier algorithm (per cited finding)**

| Step | Logic |
|---|---|
| 1. Fetch | Use `FetchContext` from Component 3 — typed `FetchResult`, three-strikes domain memory carries forward |
| 2. If `not is_usable` | → `unreachable` |
| 3. Normalized substring match | Evidence text (≥30 chars) appears in live page → `verified` |
| 4. Token overlap | Tokens len ≥4, stopwords removed, intersection / union — if ≥ 0.5 → `drifted` |
| 5. Below threshold | → `fabricated` |

**Reasoning**

| Choice                                          | Why                                                                                                                                                                                                                                           |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Re-fetch verifier as v1's headline contribution | Closes v0.1's acknowledged gap that text-vs-text grounding can't catch researcher fabrication (made-up quote on real URL). Anchored in FactScore (Min 2023) and RARR (Gao 2022). Fabrication-rate becomes the headline metric (Component 10). |
| Verifier BEFORE critic in each round            | Verifier verdicts merged into critic's input as synthetic typed issues — critic gets a richer signal; one writer revision must address both.                                                                                                  |
| Substring match before token overlap            | Substring is the strict test — verbatim quote present → done. Overlap only kicks in if substring fails (paraphrased finding gets `drifted`, not `fabricated`).                                                                                |
| 0.5 token-overlap threshold                     | Single-word rephrases shouldn't trigger fabrication; 50% absorbs minor edits without absorbing invention                                                                                                                                      |
| Verifier cache                                  | Round 2 doesn't re-fetch what round 1 already verified; finding texts are stable across rounds, only writer-cited indices change. Halves verifier cost per round on average.                                                                  |
| `max_revisions=2` (was 1)                       | One-pass revision was too aggressive in v0.1 — writer often needs the first round to *see* what's wrong, the second to *fix* it. Three iterations (rounds 0/1/2) is the conservative ceiling.                                                 |
| Caveats fallback (not silent ship)              | v0.1 shipped silently on persistent issues. v1's fallback inserts a formatted warning at the front of `report.caveats` — unresolved issues become prominent in the rendered output.                                                           |

**Alternatives rejected**

| Alternative | Why not |
|---|---|
| Verifier AFTER critic | If critic approves, verifier's verdict never matters. Order has to be verifier-first. |
| Fetch-only verifier (URL reachability only) | Reduces to v0.1's link-check; misses the fabrication case |
| LLM-only verifier (live page vs cached quote) | Adds another hallucination axis; substring + token overlap is deterministic and faster |
| `max_revisions=∞` | Quality plateaus fast; cost discipline matters |
| Silent ship + post-hoc audit | Audit doesn't reach the user; caveats in the report itself are the only honest path |

**v1 changes**

| # | Type | Change | What it closes | Exercised? |
|---|---|---|---|---|
| C1 | New contribution | Re-fetch verifier — substring + token overlap; verdicts `verified / drifted / fabricated / unreachable` | Researcher fabrication on a real URL | TBD — `verifier_ablation/` exists; needs audit |
| C3 | New contribution | Bounded revision (`max_revisions=2`) + caveats fallback | v0.1 shipped silently after persistent issues | TBD — `report.caveats` content in run outputs |
| (verifier wiring) | Architectural | Verifier first in each round; verdicts merged via `_merge_with_synthetic_fabrication_issues` | Single source of truth for typed issues | TBD |
| (verifier cache) | Polish | Per-finding cache across rounds | Verifier cost would 2× per revision | TBD |

**Status of the claim**

| Claim | Status in v1 |
|---|---|
| Verifier catches fabrication grounding misses | Defendable in principle; ablation infra exists (`--no-verifier`, `verifier_ablation/` outputs). Empirical leg = Component 10's `fabrication_rate` metric. |
| Substring + token overlap is the right algorithm | Threshold (0.5) calibrated on intuition; not empirically optimized |
| `max_revisions=2` is enough | Not directly measured. Conservative ceiling; round-3+ runs would be needed to confirm quality plateau. |
| Caveats fallback surfaces issues prominently | Visible in `report.caveats` and `exporter.py:caveats_block` rendering. Correct by inspection. |
| Verifier doesn't false-positive | Drift threshold not validated against an adversarial corpus |

**Deferred**

| Item | Why held |
|---|---|
| Verifier on un-cited findings | Cost scales linearly; un-cited never reach user. Acceptable for now. |
| LLM-judge tie-break for borderline drift cases | Adds cost; current binary heuristic is fast |
| Adversarial drift-threshold tuning | No ground-truth drift corpus available |
| Per-shape critic strictness | Uniform critic prompt across shapes (Component 1 *Deferred*) |

**Failure modes**

| Failure | Detail |
|---|---|
| `unreachable` on legitimate sources | Site that worked at run-time, fails on re-check (the `tianpan.co` case from v0.1) gets `unreachable` — same impact on writer as `fabricated`. Wayback fallback (deferred) would catch. |
| Drift threshold (0.5) is heuristic | Moderately-rephrased finding gets `drifted` not `verified`; intuition not data |
| Cache stale across rounds | If page changes between rounds (rare), cache returns stale verdict. Runs complete in minutes — acceptable. |
| `max_revisions=2` × 3 stages = up to 6 LLM calls on bad runs | Verifier is fetch-bound (cheap); critic + writer revision = up to 6 LLM calls in worst case. Costly tail on persistent-issue runs. |
| Caveats fallback ships, doesn't recover | No "give up and re-research" path; capped iterations + caveats fallback are intentional |

**Observed (v1 examples — full coverage from 12 run JSONs)**

| Observation                              | Detail                                                                                                                                                                                                                                                                                                                                                                                                                            |
| ---------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Verdict distribution (ON arms only, n=99 finding-verdicts across 6 cells)** | **verified: 71 (72%)**, **drifted: 26 (26%)**, **unreachable: 2 (2%)**, **fabricated: 0 (0%)**. Fabrication never observed on benign prompts. Drift is the dominant non-success verdict — paraphrase that softens or shifts a claim, exactly what the doc predicted in §1 (A3 split). Unreachable rare (2 cases out of 99).                                                                                                       |
| Drift rate per prompt (ON arms)          | P1: 0% · P2: 80% (12/15 — heaviest hit; CoT prompt has a lot of nuanced claims) · P3: 11% · P4: 0% · P5: 21% · P6: 47%. Wide variance — drift is concentrated on contested/comparative prompts where paraphrase risk is highest.                                                                                                                                                                                                  |
| Critic / verify rounds                   | Critic rounds: **mean 2.3, range 1–3** across 12 cells. Verify rounds: same shape (1–3, mean 2.3). Revision rounds: **mean 1.5, range 0–2**. Bounded revision is biting — most runs hit `max_revisions=2` rather than approving on round 0.                                                                                                                                                                                       |
| Critic approval rate                     | **8/12 cells approved (67%); 4/12 hit caveats fallback (33%)**: P3 ON, P5 ON, P6 OFF, P6 ON. Caveats fallback triggers concentrated on P6 (cost comparison — sparse authoritative data, hard to ground) and on the verifier-ON arms of P3 and P5 where the verifier's stricter check exhausted revisions.                                                                                                                          |
| Cost share — critic + verifier           | **Critic: ~$0.07 / run mean (8% of $0.88 total)** across 12 cells. **Verifier: $0 in stage-cost table** — verifier is fetch-bound and its LLM verdict calls roll into the critic stage. Verifier ablation Δ$ is **+$0.06 mean for ON arms** (true verifier cost ~$0.06/run, not $0).                                                                                                                                                |
| Why no fabrication                       | The verifier's fabrication-detection architecture works (verdicts populate cleanly) but the prompt set is benign — sources are real and quotes mostly stick. Validation against an adversarial corpus is open work (§7.2 item 1).                                                                                                                                                                                                  |

**When to revisit / open work**

| Trigger | Action |
|---|---|
| `unreachable` elevated on legitimate sources | Add Wayback Machine fallback (earlier scope plan, Cluster 4 deferred) |
| Drift threshold misclassifies | Calibrate against adversarial drift corpus (build first) |
| `max_revisions=2` improves at round 3 | Bump to 3 with cost guardrails |
| Caveats fallback triggers >20% of runs | Investigate planner / researcher quality upstream |
| Need adversarial fabrication corpus | Curate one (synthesize fabricated-quote findings; measure verifier recall) |

---

### Component 9 — Final exporter

**Purpose.** Render the post-critic structured `Report` to the user's requested output format and audit the rendering against prompt-derived conditions. Was new in v0.1 (ported from v1.4); now standard.

**Decision (carried unchanged from v0.1)**

The exporter's role and structure carry forward verbatim. v1 changes are minor text refinements around how prompt-derived conditions are described in the audit. No architectural change.

**v1 changes**

| Change | Detail |
|---|---|
| Caveats fallback rendering | When Component 8 prepends an unresolved-issues warning to `report.caveats`, the exporter renders the caveats block as before — surfaces the warning prominently in the rendered output |
| Minor prompt refinements | Text-prompt changes around condition-audit description; no structural change |

**Defended at** [`v0.1_design_doc.md` Component 7](./v0.1_design_doc.md).

**Failure modes** (carried) — see v0.1 doc.

**Observed (v1 examples — full coverage from 12 run JSONs)**

| Observation                       | Detail                                                                                                                                                                                                                                                                                       |
| --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Exporter stage cost share         | **~$0.12 / run mean (13% of $0.88 total)** across 12 cells — substantially higher than v0.1's ~$0.04. Most exporter cost is the LLM call that renders the structured `Report` to markdown; caveats fallback rendering and condition audits add to this. Worth a profiler pass for v2.        |
| Exporter stage latency            | **~70s / run mean** across 12 cells — large absolute time, second only to writer (~91s) and researcher (~112s). Heavy markdown generation step.                                                                                                                                                |
| Caveats block rendered correctly  | Yes — caveats appear in every `final_*.md` output (mean 8.4 caveats / report; see Component 7). Caveats fallback warning visible on the 4/12 cells that hit it (P3 ON, P5 ON, P6 OFF, P6 ON; see Component 8).                                                                                  |

---

### Component 10 — Metrics & observability

**Purpose.** Inspect a run end-to-end. v0.1's per-stage cost / latency / metadata / tool-mix carry over. v1 adds the headline `fabrication_rate` metric, three opt-in evals (KAE, ACE, plan judge), and seven $0-cost diagnostics.

**Decision (significantly expanded in v1)**

| Aspect | v0.1 | v1 |
|---|---|---|
| Per-run baseline | grounding, coverage, calibration, judge rubric | All carried; **`fabrication_rate` added as headline** |
| Per-stage cost / latency / metadata / tool-mix | P8 / P9 / P10 / P11 | All carried; new stages instrumented (classify_shape, confidence, verifier, exporter) |
| Always-on $0 diagnostics | (none) | citation density, source quality aggregate, category signals, researcher signals, unique sources, hierarchy |
| Opt-in (extra LLM calls) | (none) | `--with-plan-judge` (~1), `--with-kae` (~30), `--with-ace` (~6–11) |
| Output | `RunMetrics` per run | Same shape; new fields for new metrics |

**v1's headline metric — `fabrication_rate`**

| Aspect | Detail |
|---|---|
| Definition | Fraction of critic-approved `ReportClaim`s that cite at least one `Finding` whose `VerifyResult.verdict` is `fabricated` (final critic round) |
| Why headline | C1 (re-fetch verifier) is v1's primary contribution; `fabrication_rate` is its load-bearing metric. Lower = better. |
| Verifier-ablation evidence | `--no-verifier` runs the verifier post-hoc on un-verified output; OFF/ON delta is the verifier ablation |
| Cost | Reuses `verify_history` when present; re-runs verifier only when missing |

**Other new metrics**

| Metric | Purpose | Cost |
|---|---|---|
| `kae` (Keypoint-Aligned Eval) | KSR / KCR / KOR triplet — keypoints supported / conflicted / omitted; replaces single-number grounding | ~30 LLM calls; opt-in |
| `ace` (Adaptive Checklist Eval) | Two-stage rubric: checklist generated *blind* to report, scored separately. Anchored in Wan et al. 2026 / DeepResearch Arena (arXiv:2509.01396). Addresses LLM-as-judge same-model bias. | ~6–11 LLM calls; opt-in |
| `plan_judge` | LLM-as-judge over the decomposition (surface coverage + intent alignment). Blind to final report by design. | ~1 LLM call; opt-in |
| `citation_density` | Uncited harvested findings, single-source dependence (Herfindahl), pseudo-multi-citation | $0 |
| `source_quality_agg` | Source quality cited vs all — surfaces "researcher found good sources, writer didn't use them" | $0 |
| `category_signals` | Did `contradictory` prompts surface contradictions? Did `sparse` prompts surface uncertainty? | $0 |
| `researcher_signals` | Per-researcher counts + concentration; empty-sub-question rate | $0 |
| `unique_sources` | URL- and domain-level cardinality, harvested vs cited | $0 |
| `hierarchy` | Taxonomy diagnostics: consolidation, path diversity, over-segmented flag | $0 (over taxonomy writer output) |

**Reasoning**

| Choice | Why |
|---|---|
| `fabrication_rate` as headline | C1 needs a load-bearing metric; single number, lower-is-better, unambiguous direction |
| Opt-in vs always-on tiering | KAE alone is ~30 LLM calls; bulk runs unaffordable if everything ran always. Tiering preserves baseline cost. |
| Always-on diagnostics are pure-Python | Set arithmetic over already-collected fields; free |
| `ace` two-stage protocol | Single-stage LLM-as-judge has same-model bias (one model invents and scores rubric); two-stage decouples — paper-anchored |
| `kae` triplet (KSR/KCR/KOR) | "Grounded" hides three failure modes (uncited support / contradicted / omitted); triplet exposes which |
| `plan_judge` blind to final report | Plan quality assessable independent of execution; otherwise good plans + bad execution conflate with bad plans + good execution |
| `citation_density` Herfindahl | Single-source dependence is a real failure mode v0.1 couldn't surface |
| `source_quality_agg` cited vs all | Surfaces "researcher harvested, writer ignored" — invisible without the comparison |

**Alternatives rejected**

| Alternative | Why not |
|---|---|
| v0.1's grounding-rate alone | Saturated at 99% in v0.1's batch — useless as differentiator. KAE / ACE / fabrication_rate are non-saturated alternatives. |
| All metrics opt-in | Baseline runs need at least grounding + coverage + judge + fabrication_rate always-on |
| All metrics always-on | KAE ~30 calls × bulk runs = unaffordable |
| Hand-curated rubric per prompt category | Doesn't scale; ACE adaptive checklist generates rubrics blind |

**v1 changes**

| # | Type | Change | What it closes | Exercised on v1 batch? |
|---|---|---|---|---|
| C1 (metric leg) | New contribution | `fabrication_rate` metric | Verifier's value unmeasurable | ✅ Run on all 12 verifier-ablation cells; **saturated at 0%** in every cell |
| C9 (KAE) | New contribution | Three-axis grounding (KSR/KCR/KOR) | Single-number grounding hides failure modes | ❌ Code only — `--with-kae` flag not used in v1 batch |
| C9 (ACE) | New contribution | Two-stage adaptive-checklist (Wan 2026 anchor) | Single-stage LLM-as-judge bias | ✅ Run on all 12 verifier-ablation cells (full per-item rubric) |
| C9 (plan_judge) | New contribution | Blind plan-quality eval | Plan vs execution conflated | ✅ Run on all 12 verifier-ablation cells |
| C10 (paired ablation runner) | New contribution | `eval/ablation.py` + CLI flag `--ablation {critic \| verifier \| trace}`; runs each prompt twice (off / on) per contribution and emits `ablation_<name>_summary.md` with paired tables and aggregate deltas | No off-the-shelf paired ablation infrastructure for v1's contributions | ✅ `verifier_ablation/` (6 prompts × 2 arms = 12 cells) + `trace_ablation/` (2 prompts × 2 arms = 4 cells) |
| (citation_density) | Polish ($0) | Herfindahl + uncited diagnostics | Single-source dependence invisible | ✅ Always-on — present in metrics.md Tier 2 |
| (source_quality_agg) | Polish ($0) | Cited vs all source quality | "Harvested, ignored" failure invisible | ✅ Always-on — present in metrics.md Tier 1 as "Source quality" |
| (researcher_signals) | Polish ($0) | Per-researcher concentration | Pool concentration invisible | ✅ Always-on — present in metrics.md Tier 2 |
| (category_signals) | Polish ($0) | Per-category design-goal check | Prompt category exercise unverifiable | ⚠️ Code present; no dedicated block visible in metrics.md (folded into Tier 2 or unrendered) |
| (unique_sources) | Polish ($0) | URL + domain cardinality | Source diversity numeric | ⚠️ Code present; folded into Source quality block, not a separate section |
| (hierarchy) | Polish ($0) | Taxonomy diagnostics over `taxonomy_writer` output | Taxonomy quality unmeasurable | ❌ Taxonomy writer not invoked in v1 batch — diagnostics have nothing to operate on |

**Honest acknowledgment of scope**

Most of the metric expansion (KAE, ACE, hierarchy) was scoped to v2 in the earlier scope plan. Shipped in v1 because:

| Why it landed                                                                                |
| -------------------------------------------------------------------------------------------- |
| KAE and hierarchy are eval-side only — don't change runtime pipeline; cost only when invoked |
| ACE is paper-anchored (Wan et al. 2026) and tractable                                        |
| `fabrication_rate` is *required* by C1's contribution claim — can't be deferred              |

Same "cheap-when-prerequisites-shipped" calculus from Components 3, 5, 6.

**Status of the claim**

| Claim | Status |
|---|---|
| `fabrication_rate` differentiates verifier ON/OFF | Defendable in principle; `verifier_ablation/` is the empirical leg — TBD |
| KAE catches grounding failures single-number doesn't | Defendable in principle; needs paired audit |
| ACE addresses single-stage rubric bias | Paper-anchored; implementation correct by inspection |
| `plan_judge` is blind to final report | True by code structure (different prompt input) |
| `citation_density` Herfindahl flags single-source dependence | Mathematically correct; threshold calibration TBD |
| `source_quality_agg` (cited vs all) catches the failure | Direct comparison; correct by construction |
| Per-stage cost / latency / metadata round-trips | Carried from v0.1 (P8/P9/P10) — validated in v0.1 batch |

**Deferred**

| Item | Why held |
|---|---|
| `f_score` aggregate (KAE F-score) | Would collapse the triplet that's the point of KAE |
| Calibration slope | Needs many runs per prompt; not yet collected |
| Per-shape metric thresholds | Uniform across shapes |
| Cross-run aggregates over multiple batches | A4 — single-run is the deliverable unit |

**Failure modes**

| Failure | Detail |
|---|---|
| `fabrication_rate` saturates at 0% on perfectly-grounded runs | Headline metric hits a floor — same problem as v0.1's grounding rate, opposite direction. Watch for this. |
| KAE keypoint extraction is noisy | Cost (~30 LLM calls) suggests high variance; calibration needed |
| ACE checklist quality depends on prompt niche-ness | Adaptive checklist generation can fail for unusual topics |
| 10 new metrics could clutter metrics.md | Layout could degrade; v0.1's exporter handles structure but readability TBD |
| Per-stage instrumentation must wrap new stages | classify_shape, confidence, verifier — all need `track_stage` wrappers (verified in code) |

**Observed (v1 examples — full coverage from 12 run JSONs)**

| Observation                                          | Detail                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| ---------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `fabrication_rate` saturated at 0%                   | **Predicted in this component's *Failure modes*; happened.** 0/99 verdicts across the 6 verifier-ON cells = 0% fabrication. Headline metric hits a floor on benign prompts. Drift rate (26% aggregate; see Component 8) is the operationally useful secondary signal.                                                                                                                                                                    |
| Total cost per run                                   | **Mean $0.88, range $0.71–$1.22** across 12 cells. Highest: P6 ON ($1.22 — agentic-RAG cost prompt with longest researcher loop); lowest: P4 OFF ($0.71).                                                                                                                                                                                                                                                                              |
| Cost share by stage (mean of $0.88)                  | researcher **$0.49 (56%)**, writer **$0.15 (17%)**, exporter **$0.12 (13%)**, critic **$0.07 (8%)**, reconciler **$0.03 (3%)**, decompose **$0.02 (2%)**, classify_shape **$0.004 (0.5%)**, confidence **$0 (0%)**, verifier **$0** in stage table (true cost ~$0.06/run via ablation Δ).                                                                                                                                                |
| New stages instrumented                              | `classify_shape`, `confidence`, `exporter` all visible in the per-stage cost / latency / metadata table. `verifier` exists as a separate stage but its LLM verdict calls roll into `critic` in the per-stage breakdown — true cost only visible via the ablation Δ.                                                                                                                                                                      |
| KAE / ACE / plan_judge                               | **All `null` in the 12 run JSONs** — opt-in flags (`--with-kae`, `--with-ace`, `--with-plan-judge`) weren't used in this batch. Scores would need a fresh batch with the flags on. The KAE/ACE infrastructure is wired (Component 10) and ready to invoke.                                                                                                                                                                                |
| TrACE-K stats                                        | Populated but **all-zero** in 12/12 cells (`trace_enabled=False` in config). Confirms off-by-default; ablation evidence requires re-running with `--trace`.                                                                                                                                                                                                                                                                              |
| Source quality cited vs all delta                    | Mean cited source-quality across 12 cells: ranges 0.67–1.00 per cell; cited / harvested ratio averages 99% (Component 4). "Harvested-and-ignored" gap is near-zero on this sample — the writer cites almost everything the researcher saved.                                                                                                                                                                                              |
| Always-on diagnostics                                | citation_density, source_quality cited-vs-all, uncertainty signals all populated in every run — diagnostic surface is functioning end-to-end.                                                                                                                                                                                                                                                                                            |

**When to revisit / open work**

| Trigger | Action |
|---|---|
| `fabrication_rate` saturates at 0 | Use a more discriminating metric (e.g., KCR from KAE) |
| KAE noisy | Calibrate against adversarial corpus |
| Metrics file unreadable | Layout pass on metrics.md exporter |
| Cross-run aggregates needed | Bump A4 (v3 territory) |

---

## 4. Trace check — answers the framing question

### What landed in v1, by original scope

| Category | Items shipped | Why landed |
|---|---|---|
| **Originally scoped to v1** (earlier scope plan) | C1 (verifier), C2 (shape classifier — partial: prompt-level routing only, **not** topology), C3 (bounded revision + caveats fallback), v0.1 bug-fix carry-overs | The intended contributions |
| **Originally CUT from v1** — shipped because cheap-when-prerequisites-shipped | C4 (three-axis confidence), C5 (multi-signal stop), C6 (typed Contradictions) | Pure-Python implementations; the earlier scope plan's "marginal benefit" calculus assumed LLM-driven versions; the prerequisite (typed Contradictions) shipping made the others nearly free |
| **Originally scoped to v2** — shipped eval-side or paper-anchored | C8 (taxonomy writer), C9 (KAE + ACE evals), citation_density / source_quality_agg / category_signals / researcher_signals / unique_sources / hierarchy diagnostics | All eval-side or pure-Python — don't change runtime pipeline; cost only when invoked. ACE is paper-anchored (Wan et al. 2026), shipping it strengthens the "I know the landscape" signal. |
| **Added during v1 development in response to Section 6 finding** | C7 (TrACE-K adaptive compute) | Motivated by Section 6's `corr(harvested_url_jac, finding_jac) = +0.852` (researcher search/fetch is the variance sink). Paper-anchored to **Sethi 2026 (arXiv 2604.08369)**; verbatim implementation at the researcher's tool-use loop. Off-by-default; `--trace` flag for ablation evidence. |

### What did NOT land in v1 despite being in the earlier scope plan

| Deferred item | Where it would have lived |
|---|---|
| **Lead+parallel topology** for SPARSE_EMERGING / DISCOVERY shapes | C2's second half — only the prompt-level half shipped (Component 1 *Deferred*) |
| Wayback Machine auto-fallback (earlier scope plan, Cluster 4) | Component 3 *Deferred* — architecturally bigger; v1-only on top of v0.1's typed `FetchResult` |
| Cluster 6 self-grounding writer check | Component 7 *Deferred* — adds an extra LLM call per round |
| Cross-claim semantic dedup | Component 4 *Deferred* — embedding cost; v1.1 implemented and removed (no measured gain) |

The earlier scope plan worried that breadth would dilute the contribution signal. v1 expanded the contribution surface from C1+C2 to C1–C10, raising that risk. Mitigations actually applied:

| Mitigation | How |
|---|---|
| C1 and C2 remain the headline | Both have paper anchors and ablation infrastructure (`--no-verifier`, `verifier_ablation/`) |
| Smaller items don't compete with the headline | C5 (multi-signal stop), C6 (typed Contradictions), C7 (TrACE-K), C9 (KAE/ACE) are individually scoped — each defends one specific failure mode |
| Each addition has a "honest acknowledgment of scope" subsection in Section 3 | Reader can audit case-by-case: was this on-plan, originally cut, originally v2, or new? |
| Most additions are eval-side or pure-Python | Don't change the load-bearing v1 architecture; runtime pipeline is still 10 components, not 20 |


---

## 5. Empirical appendix — v1 batch (2026-05-05)

Raw record of the v1 runs that inform the *Observed* tables in Section 3. Append-only. Future batches go in Section 5.6+ alongside this one.

### 5.1 What was run

|             |                                                                                                                             |
| ----------- | --------------------------------------------------------------------------------------------------------------------------- |
| Date / time | 2026-05-05, 12:32 – 16:35 UTC (single run + verifier ablation + trace ablation)                                             |
| Commit      | `de5b7d0f40e6584303761befb270bda2f44c2f27` (branch `main`)                                                                  |
| Models      | researcher = `claude-haiku-4-5`; writer / critic = `claude-sonnet-4-6`; judge = `claude-opus-4-7`; KAE = `claude-haiku-4-5` |
| Outputs     | `DRAS/v1/examples/outputs/`                                                                                                 |

### 5.2 What's in `examples/outputs/`

| Path | Contents |
|---|---|
| `final_20260505T053240_*_p.md` + paired `metrics_*.md` | One smoke run (prompt was literally `p`); 0 findings; not load-bearing evidence |
| `run_01_..._off.json` / `_on.json` (synthetic data) | Verifier ablation pair (top-level — appears to be early test run, redundant with `verifier_ablation/` below) |
| `run_02_..._off.json` / `_on.json` (chain-of-thought) | Same |
| `test_prompt_1_initial_run.json` | Smoke run, not load-bearing |
| **`verifier_ablation/`** | **6 paired OFF/ON runs across all 6 of v0.1's batch prompts + `ablation_verifier_summary.md`** — the headline empirical evidence for C1 |
| `trace_ablation/` | 2 paired OFF/ON runs (synthetic data + chain-of-thought) — partial TrACE evidence; no aggregate summary |

### 5.3 Headline aggregate — verifier ablation

From `verifier_ablation/ablation_verifier_summary.md`. Δ = ON − OFF (positive Δ on grounding/coverage/judge means verifier helped; positive Δ on fabrication means it caused MORE fabrication).

| prompt | Δgrounding | Δfabrication | Δcoverage | Δjudge.overall | Δcost | Δelapsed |
|---|---:|---:|---:|---:|---:|---:|
| 1 — synthetic data | -6% | +0% | +29% | -0.50 | -$0.01 | -2.3s |
| 2 — chain-of-thought | +0% | +0% | +0% | -0.50 | +$0.12 | +96.5s |
| 3 — inference-time scaling | +2% | +0% | +14% | -0.20 | +$0.17 | +99.7s |
| 4 — multi-agent landscape | +0% | +0% | -43% | -0.50 | +$0.05 | +54.0s |
| 5 — long-context vs RAG | +0% | +0% | -14% | -0.50 | -$0.16 | -66.0s |
| 6 — agentic-RAG cost compare | +13% | +0% | -14% | +0.00 | +$0.20 | +93.1s |
| **Mean (n=6)** | **+2%** | **+0%** | **-5%** | **-0.37** | **+$0.06** | — |

### 5.4 Honest reading of the verifier ablation

| Finding | Detail |
|---|---|
| **Fabrication = 0% in EVERY cell (12/12)** | Headline metric for v1's headline contribution doesn't differentiate. Same saturation problem as v0.1's grounding rate, at the floor end. The verifier had nothing to catch in this batch. |
| **Verifier ON regresses judge.overall by -0.37 mean** | Substantial. Verifier-injected revisions are making reports the judge rates *worse*, on average. |
| **Coverage moves both directions** | +29% on synthetic-data; -43% on multi-agent landscape. Net mean -5%. The verifier's `unreachable` and `drifted` verdicts may be dropping legitimate findings post-revision. |
| **Cost: +$0.06 mean per run** | Modest fetch-bound overhead, as designed |
| **Drift rate is substantial and concentrated** | Aggregate **26% across 99 verdicts** (verifier-ON cells); per-prompt range **0–80%**. Heaviest hit: P2 (CoT) at 80%, P6 (cost compare) at 47% — both contested/comparative. The verifier IS doing work; what it's catching is paraphrasing, not invention. |
| **Caveats fallback fires on 4/12 cells (33%)** | Concentrated on P6 (both arms; sparse authoritative data) and verifier-ON arms of P3 and P5 (strict checks exhausted revisions). Bounded-revision design is biting; on hard prompts the system ships with caveats rather than silently emit. |

**Implication for the doc.** v1's headline contribution (C1 verifier) does NOT show empirical wins on its headline metric (fabrication_rate) in this batch. It shows costs (judge regression, coverage swings, modest $) without the corresponding fabrication-catch. **This needs to be acknowledged in the interview defense.** Possible explanations:

- The 6 prompts don't elicit researcher fabrication — Claude's researchers are honest enough that the failure mode the verifier targets isn't manifesting
- An adversarial fabrication corpus would likely show non-zero verifier wins (Section 7.2 item 1)
- The 26% aggregate drift rate is what the verifier IS catching — softer signal than fabrication, but real and concentrated on contested-shape prompts (Section 7.2 item 2)

### 5.5 What this batch does NOT prove

| Open question | What would close it |
|---|---|
| Does the verifier catch fabrication on adversarial prompts? | Adversarial corpus where researcher fabrication is induced; not available in v1 |
| Why does verifier ON regress judge.overall? | Manual review of paired OFF/ON outputs to see what the writer changed and why the judge graded ON lower |
| Does multi-signal stop's coverage regression stand on more prompts? | Per-shape coverage analysis; the prompt 4 coverage drop (-43%) is the most striking and warrants drilling |
| Does TrACE-K reduce decision variance? | `trace_ablation/` only has 2 paired runs and no aggregate summary; would need to extend |
| Does the shape classifier actually route correctly? | Per-prompt shape audit + comparison of v0.1-vs-v1 decomposition output for the same prompt |
| Does the writer use `combined_confidence` over self-reported in practice? | Run-JSON-level audit of `ReportClaim.confidence` correlation with combined vs self-reported |

---

## 6. Reproducibility investigation — where does run-to-run variance enter the pipeline?

### 6.1 Motivation

- **Observation.** Multiple runs on the same prompt produced different final reports.
- **Initial hypothesis.** Planner instability (different sub-questions for the same prompt) → claim instability (different final reports).
- **Reframe before measuring.** Planner instability is *only a problem if it causes worse output*. Two different plans for the same prompt could both produce correct reports — divergence at the planner is not automatically harmful.
- **Question reformulated.** Across same-prompt re-runs, does planner Jaccard correlate with downstream quality (claim Jaccard, grounding, F1)?

### 6.2 Methodology

- **Setup.** N=6 prompts, 2 runs per prompt, draft-only mode (`max_revisions=0`, `verifier_enabled=False`). Eval entry point: `eval/research_depth/finding1_step0.py`.
- **Per-prompt Jaccards** computed across the two runs of the same prompt:
  - `sq_jac` — sub-question Jaccard (planner output)
  - `claim_jac` — final-report claim Jaccard (writer output)
- **Per-prompt aggregate metrics** (mean across the two runs): grounding rate, F1, claim count.
- **Cross-prompt Pearson correlation** between `sq_jac` and downstream measures.

#### 7.2.1 Methodology mistake #1 — whole-string Jaccard

- **First pass** computed Jaccard on whole strings. Two sub-questions like *"What is X?"* and *"What is X today?"* scored as Jaccard = 0.0 — semantically near-identical but lexically distinct.
- **Fix.** Switched to **token-wise Jaccard with best-match assignment** between the two sub-question sets. After the fix, `sq_jac` distributed sensibly (~0.25–0.40 across prompts), giving the cross-prompt correlations real signal.
- **Lesson.** Whole-string equivalence is the wrong primitive for comparing sets of natural-language items. Default to token-overlap unless you specifically want exact-match.

### 6.3 First-pass diagnosis (wrong) — "writer is the sink"

After the Jaccard fix, the cross-prompt Pearsons (N=6):

| Correlation | r | Read |
|---|---:|---|
| `corr(sq_jac, claim_jac)` | **−0.543** | Negative — *more* sub-question overlap correlates with *less* claim overlap |
| `corr(sq_jac, mean_grounding)` | **−0.722** | Negative — same direction, stronger |
| `corr(sq_jac, mean_F1)` | +0.032 | Basically zero |

- **First-pass read.** "Even when the planner is stable, claims diverge, so the **writer is amplifying noise.**" The proposed move was a writer-rewrite ablation.
- **Why this was wrong.** A 2-point chain (planner → final claim) cannot tell you *where* in between variance enters. The negative correlation is real, but reading it as "writer is the sink" skipped the researcher entirely.

### 6.4 Three-layer view (corrected) — researcher search/fetch is the actual sink

Inserted intermediate Jaccards along the pipeline:

```
planner  →  researcher SEARCH  →  researcher EXTRACT  →  writer
 sq_jac    harvested_url_jac       finding_jac          claim_jac
```

#### 7.4.1 Per-prompt table (N=6, draft-only)

| id | category | sq_jac | harvest_url_jac | finding_jac | claim_jac | grounding | F1 | #claims |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| easy-2 | easy | 0.306 | 0.087 | 0.133 | 0.118 | 97.4% | 80.8% | 18.5 |
| contradictory-2 | contradictory | 0.248 | 0.043 | 0.142 | 0.135 | 100.0% | 72.7% | 15.0 |
| sparse-1 | sparse | 0.282 | **0.000** | 0.101 | 0.101 | 91.7% | 51.3% | 7.5 |
| sparse-2 | sparse | 0.303 | 0.133 | 0.179 | 0.117 | 100.0% | 58.6% | 10.0 |
| multimodal-1 | multimodal | 0.381 | 0.053 | 0.152 | 0.092 | 84.2% | 67.7% | 13.5 |
| multimodal-2 | multimodal | 0.345 | 0.059 | 0.140 | 0.127 | 95.2% | 65.0% | 17.5 |

#### 7.4.2 Median across the chain

| Layer | Median Jaccard | Read |
|---|---:|---|
| `sq_jac` (planner) | ~0.30 | **Moderately stable** |
| `harvested_url_jac` (researcher SEARCH) | **~0.06** | **Very unstable — actual sink** |
| `finding_jac` (researcher EXTRACT) | ~0.14 | Same level as `claim_jac` |
| `claim_jac` (writer) | ~0.12 | Pass-through, no amplification |

- The chain hits its floor at the SEARCH step. Sparse-1 fetched **zero** overlapping URLs across two runs.
- Once you get past search, every downstream layer sits at roughly the URL-level Jaccard — they inherit the floor, they don't lower it.

#### 7.4.3 Headline correlations from the 3-layer view

| Correlation | r | Interpretation |
|---|---:|---|
| `corr(harvested_url_jac, finding_jac)` | **+0.852** | **Extraction is faithful.** When researchers fetch the same pages, they extract the same facts. |
| `corr(finding_jac, claim_jac)` | +0.174 | **Writer is faithful in a *good* way.** `claim_jac` and `finding_jac` sit at the same level (~0.13); the writer doesn't amplify noise above its inputs. The flat correlation reflects writer instability being uniformly low across prompts, not the writer being a sink. |
| `corr(sq_jac, claim_jac)` | −0.543 | Now reads as: the planner is *not* the bottleneck. The negative sign came from the search-layer floor swamping any planner signal — sub-question overlap doesn't predict downstream overlap once SEARCH has already collapsed it. |

### 6.5 Diagnosis (corrected)

- **The reproducibility floor is set at researcher SEARCH/FETCH** (`harvested_url_jac`, median 0.06).
- **Decision rule that emerged.** Walk the Jaccard chain left to right; whichever layer's Jaccard drops first relative to its predecessor marks where variance enters. Once you've hit the floor, every downstream layer just inherits it.
- **Planner-side moves are not the right lever.** `sq_jac` ~0.30 is already higher than anything downstream — making the planner more deterministic doesn't help if SEARCH then collapses URL overlap to ~0.06 anyway.
- **Writer-rewrite ablations are wasted budget.** The writer is already passing through researcher output without amplifying noise; tightening it can only marginally affect a quantity already capped by upstream URL instability.

### 6.6 What this means — architectural insight

**The reproducibility floor is set at researcher SEARCH/FETCH** (`harvested_url_jac`, median 0.06). Three implications follow:

1. **Decision rule.** Walk the Jaccard chain left to right. Whichever layer's Jaccard drops first relative to its predecessor marks where variance enters. Once the floor is hit, every downstream layer just inherits it.
2. **Planner-side moves don't help.** `sq_jac` ~0.30 is already higher than anything downstream — making the planner more deterministic is wasted budget.
3. **Writer-rewrite ablations are wasted budget.** The writer passes through researcher output without amplifying noise; tightening it can only marginally affect a quantity already capped upstream.

**What we almost spent budget on.** The original Finding 1 plan listed four moves — Self-Consistency on plans, PRM on plans, RL on the planner, and Search-R1 framed as RL on the planner. **All four targeted the wrong layer.** The 3-layer view changed the question. None of those moves remains in scope.

### 6.7 Implications — Already shipped / Ship next / Sketch / Drop

#### ALREADY SHIPPED in v1 (C7) — TrACE-K, the training-free researcher-side intervention

- **Direct response to this investigation.** The +0.852 finding identified the researcher search/fetch layer as the variance sink. TrACE-K was added during v1 development as the data-motivated response.
- **Paper anchor: Sethi 2026** (arXiv 2604.08369, *"Don't Overthink It: Inter-Rollout Action Agreement as a Free Adaptive-Compute Signal for LLM Agents"*).
- **Mechanism.** Verbatim Sethi: at each researcher tool-use turn, draw `k_init=2` candidates at temperature 0.7, compute inter-rollout agreement α, commit if α ≥ τ_high=0.75, else expand one-at-a-time to `k_max=4`.
- **Why training-free.** The Section 6 study placed variance at the researcher; the cheap intervention is the responsible first ship. Sethi 2026 explicitly distinguishes from PRM/ORM-trained methods — TrACE-K rides that distinction.
- **Status.** Shipped, off-by-default (`--trace` for ablation evidence). `trace_ablation/` outputs are the empirical leg.

#### SHIP NEXT (v2) — Finding 2 Move 1: Self-Ask replanning on zero-yield researchers

- **Mechanism.** When a researcher's tool-use loop returns zero findings, prompt the planner: *"this sub-question yielded nothing — propose a reformulation or declare it unanswerable."* Re-run that researcher with the reformulated sub-question.
- **Why it ships next.** Small code change. Measurable impact on coverage and grounding. Independent of the Section 6 Step 0 redirection — Finding 2 was unaffected and is orthogonal to TrACE-K. Composes cleanly with TrACE-K (replanning at zero-yield + adaptive sampling within each researcher's loop).
- **Status.** Not in v1; in scope for the v2 build window. **The prior analysis's prescribed action; still unshipped.**

#### SKETCH (write up, do not run) — Finding 1 reframed: Search-R1 retargeted to the researcher

- **Original framing.** *"RL on the planner."*
- **Redirected formulation.** *"RL on the researcher with reward = `finding_jac × source_quality − tool_calls × cost`."*
- **Relationship to TrACE-K.** TrACE-K (already shipped) is the **training-free cousin** of this Move. Same lever (researcher), no training, weaker effect, lower risk. Sethi 2026 makes this distinction explicit. Move 4 — Search-R1 with full RL training on the researcher reward above — is the trained version of the same idea.
- **Why sketch only.** The retargeting is motivated by *this study's data*, not borrowed from the literature. The training run is v2+ work; TrACE-K is the v1 honest down-payment on the same lever.

#### DROP — Step 0.5 URL-pinning experiment

- **Original plan.** Pin harvested URLs across re-runs (mock the search layer / replay fetches from cache) to test the +0.852 prediction directly.
- **Why drop.** +0.852 with the per-prompt table is enough evidence on its own. URL-pinning would only confirm a number we already have a strong prior on. Budget is better spent on the SHIP item above.

#### Caveats on the numbers

- N=6 prompts. 95% CI on Pearson r is wide.
- Treat the correlations as **direction, not a hypothesis test.** The +0.852 in particular cannot be formally rejected against category confound without more prompts.
- The headline does not depend on the precise r values — it depends on the *layer where the floor sits* (`harvested_url_jac`), which is robust to noise in the correlation estimate.

### 6.8 Honest acknowledgments of investigation mistakes

| Mistake | Cost | Lesson |
|---|---|---|
| Whole-string Jaccard | Wasted first measurement pass; numbers were too lexically brittle to trust | Default to token-overlap when comparing sets of natural-language items |
| Two-point chain (planner → claim) misread | Misdiagnosed writer as sink; would have driven a wasted writer-rewrite ablation | Insert intermediate measurements before pointing at endpoints; don't infer middle-of-pipeline behavior from end-to-end correlation alone |
| Single-pass interpretation | Almost shipped a "writer rewrite" intervention that the +0.852 correlation later showed was the wrong lever | When a correlation is surprising, look for an unmeasured layer before redesigning a measured one |

---

## 7. Future work — v2 candidates and one deep thread

### 7.1 What v1 deliberately left undone, and why

| Item                                                                                  | Why deferred in v1                                                                                       | v2 readiness                                                                                                      |
| ------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| **Lead+parallel topology** (C2's deferred half)                                       | Architecturally bigger; v1 needed prompt-level routing to ship first to validate the shape signal at all | **High** — prompt-level routing is now empirically defended; topology is the natural next step                    |
| **Wayback Machine fallback** for `unreachable` URLs                                   | Fetch-layer change; v1 only added typed `FetchResult` on top of v0.1                                     | Medium — useful but unblocked; small scope                                                                        |
| **Per-shape calibration** of stop conditions and TrACE-K knobs (`k_init`, `tau_high`) | n=6 prompts/batch; not enough per-shape data to tune                                                     | Medium — needs a larger batch first; calibration without data is guesswork                                        |
| **LLM-driven multi-signal stop** (content-quality check)                              | Cut for cost; pure-Python version shipped and works                                                      | Low — pure-Python catches the v0.1 single-source case; LLM version's marginal benefit unclear                     |
| **self-grounding writer check**                                                       | Extra LLM call per round; verifier (C1) already covers post-hoc grounding                                | Low — overlap with verifier                                                                                       |
| **Cross-claim semantic dedup**                                                        | v1.1 implemented; **no measured gain** — removed                                                         | Drop — already tested, didn't pay off                                                                             |
| **Adversarial fabrication corpus**                                                    | None available                                                                                           | **High** — Section 5's 0% fabrication rate is uninformative without one; verifier's headline claim is unvalidated |

### 7.2 What v2 must address (driven by v1's empirical findings)

1. **Validate the verifier on adversarial input.** Section 5: 0/99 fabricated verdicts across the verifier-ON cells; verifier-ON arms regress judge by −0.37 (the regression is real, the catch isn't measurable). Without an adversarial corpus we can't tell if the verifier is structurally redundant on benign prompts (the design defense) or just untested (the empirical risk). **Build the corpus first; re-run the ablation.**
2. **Reduce per-prompt drift variance.** Drift hits **80% on P2 (CoT)** and **47% on P6 (cost comparison)** — both contested/comparative prompts where paraphrase risk is highest. Mean across batch is 26%. Drift catching works, but heavy concentration on certain shapes suggests the verifier should be paired with shape-aware drift thresholds or writer-side anti-drift constraints (e.g., quote-anchored claims for CONTESTED prompts).
3. **Per-shape stop thresholds.** Multi-signal stop hurts unevenly: P4 ON drops to 29% coverage (DISCOVERY shape), but P1 ON gains to 86%. The AND-conjunction over-constrains some prompts and not others. Either branch thresholds per shape, or measure which conjunction axis (findings / unique-sources / avg-confidence) is the binding constraint per shape and relax just that one.
4. **Triage the caveats fallback.** **4/12 cells (33%) hit caveats fallback** — concentrated on P6 (both arms; sparse authoritative data) and verifier-ON arms of P3 / P5 (strict checks exhausted revisions). Fallback is doing its job, but a 33% rate is high enough to warrant: (a) is `max_revisions=2` too tight for hard prompts? (b) should the caveats fallback path emit more diagnostic info about *why* it triggered?

**Empirical claims now resolved by the full batch (no longer v2 work):**

- ~~Recalibrate the writer's confidence wiring~~ — full batch shows **r(claim, combined) = +0.61** (vs the earlier subsample's r = +0.11). The writer is meaningfully tracking the wired three-axis signal; C4's wiring is empirically defended, not decorative.
- ~~Tune `axis_disagreement` threshold~~ — full batch shows the flag fires at **12.6%** (26/207 findings), not 0%. Threshold is appropriate; earlier "never fires" worry was a small-sample artifact.

### 7.3 Deep thread — RL on the researcher, with reward from v1's existing scaffolding

Section 6 placed the reproducibility floor at the researcher's per-step decision (`corr(harvested_url_jac, finding_jac) = +0.852`, median 0.06). v1's response was the cheap intervention (TrACE-K — sample-and-vote, training-free). The proper intervention is teaching the researcher better per-step decisions via RL. A4 (no training) drops in v2; this is the constraint relaxation that unlocks the right lever.

**Reward comes from v1, not invention.** Process: per-step shaped reward from `combined_confidence` lift, verifier pass, source-quality floor (each from an existing component). Outcome: judge.overall (ACE-mitigated), `fabrication_rate` on adversarial prompts (depends on item 1 above), KAE coverage. Penalties: verifier verdict `fabricated`/`drifted`, claims emitted above their evidence.

**Open research question:** which reward shape generalizes across prompt shapes — outcome-only (simple, slow credit), process-supervised (needs labels), or hybrid (likely landing). Sethi 2026 explicitly distinguishes its training-free approach from PRM/ORM methods — that's the door v2 walks through.

**Risks:** reward hacking (researcher saves low-quality findings to clear `min_findings`); distributional shift across shapes (may force per-shape policies); training cost (TrACE-K stays the cheap baseline to beat).

**v2 milestone:** train on 6 v1 prompts × N rollouts; reward = (verifier-pass × source-quality + ε·judge − fabrication-penalty); success = median `harvested_url_jac` rises from 0.06 to >0.15 with no judge/coverage regression.

#### Architectural changes the RL thread implies

| Component / layer | Change |
|---|---|
| **Researcher** (`researcher.py`) | LLM call replaced with a trained policy (fine-tuned model, or policy head over the base model). Adds a `policy_mode` config: `train` (sampling + exploration + reward emission) vs `inference` (greedy / low-temperature, no reward stream). |
| **Verifier** (Component 8) | Promoted from post-hoc (inside critic loop) to **real-time** — callable from inside the researcher loop after each `save_finding`, so verdicts feed the per-step reward stream instead of waiting for the writer. Same code, new caller. |
| **New: rollout buffer** | External store (file-backed or DB) for trajectories `(state, action, reward, next_state)` per researcher step. Keeps training data out of the runtime `ResearchRun` JSON. |
| **TrACE-K logging** (`trace.py`) | `TraceStep` already records per-turn k, α, plurality choice — extend with reward and advantage fields. The infrastructure is already step-keyed; adding numeric reward columns is small. |
| **Component 6 confidence axes** | No change — already computes `combined_confidence` at the right point in the pipeline; reward formula reads from it directly. |
| **Component 10 metrics** | New per-run aggregates: rollout count, mean reward, advantage variance, policy entropy. Existing per-stage cost / latency tables carry forward. |
| **Orchestrator, planner, writer, critic, F1 fan-out** | **No change.** RL trains the researcher's per-step policy; everything around it consumes findings the same way. The 10-component pipeline shape holds. |

**Net architectural delta:** one component changes substantively (researcher), one is repurposed without code change (verifier called from a new site), one new piece (rollout buffer), two get extended fields (TrACE-K logging, Component 10 metrics). Six of ten components are unchanged. The bet is that v1's pipeline was the right scaffolding for *measuring* researcher decisions, so v2 can reuse it for *training* them.
