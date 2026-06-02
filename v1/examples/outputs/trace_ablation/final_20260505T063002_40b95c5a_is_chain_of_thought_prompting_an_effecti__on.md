<!--
Original prompt: Is chain-of-thought prompting an effective reasoning strategy for LLMs, or does it primarily improve output formatting? The literature disagrees — find the real fault lines and explain what accounts for the conflicting results.
Detected format: Narrative summary in markdown, with explanatory prose organized around the fault lines and conflicting results in the CoT literature — inferred from the analytical, open-ended nature of the prompt.
-->

> **Original prompt:** Is chain-of-thought prompting an effective reasoning strategy for LLMs, or does it primarily improve output formatting? The literature disagrees — find the real fault lines and explain what accounts for the conflicting results.
> **Detected format:** Narrative summary in markdown, with explanatory prose organized around the fault lines and conflicting results in the CoT literature — inferred from the analytical, open-ended nature of the prompt.

---

# Chain-of-Thought Prompting: Effective Reasoning or Output Formatting? A Research Note

> **⚠️ Pipeline Retrieval Failure — No Empirical Claims Can Be Made**

## What Was Asked

This report was commissioned to resolve a genuine controversy in the large language model (LLM) literature: does chain-of-thought (CoT) prompting improve *genuine reasoning*, or does it primarily polish *output formatting and surface structure*? The goal was to identify the real fault lines between conflicting studies and explain what methodological, mechanistic, and empirical factors account for divergent conclusions.

Seven sub-questions were investigated:

1. Empirical evidence that CoT improves task accuracy beyond formatting (and on which task types / model scales)
2. Evidence that CoT primarily improves output structure rather than reasoning
3. Methodological differences between studies (prompting style, benchmarks, metrics, model family, scale)
4. Mechanistic/interpretability evidence on whether CoT causally influences internal computations or is post-hoc rationalization
5. Unfaithfulness and hallucination in CoT chains as evidence against genuine reasoning
6. Conditions under which CoT fails or backfires
7. Theoretical frameworks proposed to explain *why* CoT works

## Why No Answer Can Be Given

Unfortunately, **the research pipeline failed to retrieve a single citable finding across all seven sub-questions**. Each sub-question exhausted its full tool-call budget (10–12 calls each) without successfully extracting papers, abstracts, or empirical results from the literature. This appears to be a retrieval infrastructure problem — likely an inability to access paywalled sources, arXiv, Semantic Scholar, or the ACL Anthology during the run — rather than an absence of relevant literature.

Because no findings were retrieved, **zero evidence-grounded claims can be made** about:

- Whether CoT improves accuracy beyond formatting on any task type or model scale
- Whether formatting confounds explain CoT gains when answer format or output length is controlled
- Which methodological variables (zero-shot vs. few-shot prompting, benchmark choice, evaluation metrics, model family, scale thresholds) drive conflicting results
- Whether CoT reasoning traces causally influence LLM computations or are post-hoc rationalizations
- How unfaithfulness and hallucination in reasoning chains undermine CoT's claims to genuine reasoning
- Under what conditions CoT fails (non-symbolic tasks, small models, perturbed rationales)
- Which theoretical framework — scratchpad computation, implicit search, length-generalization — is best supported by evidence

## What This Means for the User

This is a **null result from the pipeline**, not a null result from the literature. The controversy the prompt identifies is real and active in the research community. A meaningful answer requires a fresh retrieval attempt with:

- Direct API access to **arXiv** (e.g., searching for Wei et al. 2022, Kojima et al. 2022, Turpin et al. 2023, Lanham et al. 2023, Merrill & Sabharwal 2023, and related work)
- Access to **Semantic Scholar** or **ACL Anthology** for peer-reviewed NLP venue papers
- Possibly direct PDF retrieval for key papers identified by DOI or arXiv ID

## Recommendation

Please **retry this query** with a retrieval backend that has confirmed access to at least one of the above sources. The structured sub-questions above remain valid and provide a strong scaffolding for a well-grounded answer once empirical material is accessible.

---
*Report status: **No findings retrieved.** All claims in this document are meta-commentary on the pipeline failure, not substantive claims about the CoT literature.*
