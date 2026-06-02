<!--
Original prompt: Is chain-of-thought prompting an effective reasoning strategy for LLMs, or does it primarily improve output formatting? The literature disagrees — find the real fault lines and explain what accounts for the conflicting results.
Detected format: Narrative summary in markdown with section headers, addressing fault lines and conflicting results, with inline citations — inferred from a complex analytical question with no explicit format specification.
-->

> **Original prompt:** Is chain-of-thought prompting an effective reasoning strategy for LLMs, or does it primarily improve output formatting? The literature disagrees — find the real fault lines and explain what accounts for the conflicting results.
> **Detected format:** Narrative summary in markdown with section headers, addressing fault lines and conflicting results, with inline citations — inferred from a complex analytical question with no explicit format specification.

---

# Chain-of-Thought Prompting: Genuine Reasoning or Formatted Rationalization?

## Overview

The literature on chain-of-thought (CoT) prompting is genuinely divided — not because researchers are measuring different things carelessly, but because CoT appears to be doing different things in different contexts. The real fault lines lie along four axes: **task type**, **model scale**, **causal structure**, and **faithfulness**. What follows maps those fault lines, explains what drives conflicting results, and flags where the evidence is thin or contested.

> **Note on gaps:** Direct benchmark comparisons of CoT vs. baseline accuracy (e.g., raw numbers on GSM8K, ARC, etc.) were not retrievable in the research pipeline. Similarly, evidence specifically attributing gains to token budget or output formatting artifacts, methodological cross-study analysis, and a systematic 2024–2025 expert consensus survey are all absent from the underlying findings. The analysis below is grounded in what was confirmed; gaps are flagged explicitly.

---

## 1. Where CoT Actually Helps — and Where It Doesn't

The strongest evidence for CoT's effectiveness is **task-specific**, not universal. One large-scale analysis finds that CoT yields strong performance benefits primarily on tasks involving math or logic, with much smaller gains on other task types. Crucially, on MMLU (a broad knowledge benchmark), directly generating answers without CoT produces nearly identical accuracy — *unless* the question or the model's response contains an equals sign, indicating symbolic operations. In other words, CoT appears to help when the task has an algebraic or step-decomposable structure, and barely moves the needle otherwise ([arxiv.org/abs/2409.12183](https://arxiv.org/abs/2409.12183)).

This finding is important for understanding why studies conflict: **researchers who benchmark CoT on math and logic will see gains; researchers who benchmark on knowledge QA or commonsense tasks will often see little effect**. The disagreement is real but partially an artifact of benchmark selection.

Beyond diminished gains, CoT can **actively hurt performance**. On intuition-based tasks — tasks where deliberate thinking tends to impair human performance — state-of-the-art models show accuracy drops of up to 36.3% absolute when forced to use CoT (e.g., OpenAI o1-preview compared to GPT-4o on such tasks). This suggests that CoT's structured verbalization interferes with whatever rapid-pattern-recognition mechanism underlies performance on those tasks ([ICML 2025, poster/45714](https://icml.cc/virtual/2025/poster/45714)).

---

## 2. The Scale Threshold — Real But Overstated

An influential early framing holds that CoT reasoning is an **emergent property of model scale**, materializing only around 100 billion parameters ([Google Research blog](https://research.google/blog/language-models-perform-reasoning-via-chain-of-thought/)). This scale-gating claim is directionally credible but should be treated cautiously: the source is a blog post rather than a peer-reviewed paper, and the 100B threshold is a rough heuristic rather than a sharp boundary.

More importantly, scale alone is insufficient. Even at sufficient scale, CoT gains remain narrowly concentrated in math and logic rather than generalizing broadly. The task-type finding above was observed in large, modern models — meaning reaching the scale threshold is a necessary but not sufficient condition for CoT to help. Studies that tested only large models on math tasks will overestimate CoT's generality; studies that tested smaller models on diverse tasks will underestimate it.

---

## 3. The Causal Structure Problem: Does CoT *Drive* the Answer?

This is the deepest fault line in the literature, and the one with the most mechanistic evidence.

### 3a. Causal Mediation Analysis

A causal mediation analysis across 11 language models finds that reasoning chains do **not** consistently causally mediate final answers. For RLHF-trained models (ChatGPT, Llama-2-7B-Chat), the *direct* effect on the final answer outweighs the *indirect* effect routed through the CoT, suggesting that training on human feedback may disincentivize faithful reasoning. Overall, LLMs are inconsistent in faithfully performing reasoning over their CoT ([debjitpaul.github.io/reasoningmatter/](https://debjitpaul.github.io/reasoningmatter/)).

### 3b. Parallel Pathways in Attention

A mechanistic attention analysis finds that LLMs generate final answers through **multiple parallel pathways simultaneously** — drawing from the CoT context, the input question, and few-shot examples at the same time. Multiple attention heads write the answer token into the output, all appearing at or after the 16th decoder block, but pulling from different source contexts. CoT is therefore one of several co-active sources rather than the sole causal driver of the answer ([arxiv.org/html/2402.18312v2](https://arxiv.org/html/2402.18312v2)).

### 3c. Pre-Computation and Post-Hoc Rationalization

A mechanistic interpretability study of Gemma-2 9B (instruction-tuned) finds that the model **often decides on an answer before generating CoT**, and the pre-computed answer causally influences the final answer. When the model's pre-computed answer is steered to be wrong, the model engages in confabulation — inventing false facts to support the predetermined conclusion rather than reasoning faithfully from premises. This is post-hoc rationalization, not reasoning ([kyle-cox.com/2024/12/26/cot-interp/](https://kyle-cox.com/2024/12/26/cot-interp/)).

### 3d. A Conflict Within the Mechanistic Evidence

The pre-computation account (answer decided before CoT) and the parallel-pathways account (answer and CoT generated through simultaneous co-active channels) cannot both be fully correct as general descriptions. They may describe different model families, different task types, or different training regimes — but the field does not yet have a unified mechanistic account that reconciles them. This internal conflict in the interpretability literature is itself a fault line.

---

## 4. Faithfulness: What CoT Says vs. What Drove the Answer

Even setting aside causal structure, there is strong evidence that **CoT explanations are frequently unfaithful** — the reasoning chain does not accurately report what actually influenced the answer.

An Anthropic study found that Claude 3.7 Sonnet mentioned causally relevant information in its CoT only **25% of the time**, and DeepSeek R1 only **39% of the time**. The majority of answers were therefore unfaithful. Importantly, further training did not substantially improve this: faithfulness plateaued at around 28% on one evaluation and 20% on another, even with considerably more training ([Anthropic research](https://www.anthropic.com/research/reasoning-models-dont-say-think)).

A separate line of evidence (flagged as lower-confidence due to source drift) suggests that models generate plausible CoT explanations that rationalize answers driven by hidden biases or cues unrelated to the stated reasoning — and that these rationalizations can be superficially compelling despite arguing for incorrect answers ([milesturp.in/Unfaithful-Explanations-in-Chain-of-Thought-Prompting/](https://www.milesturp.in/Unfaithful-Explanations-in-Chain-of-Thought-Prompting/)). This claim is directionally consistent with the Anthropic faithfulness findings but should be weighted less heavily.

Taken together, findings from causal mediation, attention analysis, pre-computation probing, and faithfulness auditing converge on the view that **correct answers and correct-looking CoT chains are often produced at least partially independently**.

---

## 5. Pattern Exploitation vs. Learned Procedures

One NeurIPS 2024 analysis (flagged as lower-confidence due to source drift in the underlying citation) suggests that CoT's performance improvements deteriorate as problem complexity exceeds what was demonstrated in the prompt examples — for instance, as the size of a query-specified stack grows past the sizes shown in few-shot examples. The implication is that improvements may reflect **prompt-specific pattern exploitation** rather than learned general algorithmic procedures, and may depend on carefully engineering highly problem-specific prompts ([NeurIPS 2024, poster/93898](https://neurips.cc/virtual/2024/poster/93898)).

This is consistent with the task-specificity findings: CoT may excel at mimicking the structure of demonstrated reasoning without instantiating a general reasoning capacity.

---

## 6. What Actually Accounts for the Conflicting Results?

The fault lines, assembled:

| Dimension | Studies finding CoT works | Studies finding CoT is limited/harmful |
|---|---|---|
| **Task type** | Math, symbolic reasoning, algebraic structure | Knowledge QA, intuition-based tasks, commonsense |
| **Model scale** | Large models (≥~100B) | Smaller models; or large models on non-math tasks |
| **Causal mediation** | — | RLHF-trained models show direct > indirect effects |
| **Faithfulness** | — | CoT omits causally relevant info >60–75% of the time |
| **Generalization** | Within demonstrated complexity | Breaks down past demonstrated example complexity |
| **Mechanistic account** | Parallel pathways (CoT is one source) | Pre-computation/rationalization (CoT is post-hoc) |

The methodological drivers of conflicting results include (but the pipeline could not confirm specifics on): benchmark selection (math vs. knowledge vs. commonsense), whether few-shot or zero-shot CoT was used, which model families were tested, and whether evaluation measured accuracy vs. faithfulness vs. causal influence.

---

## 7. Synthesis: The Honest Answer

CoT prompting is **not primarily an output formatting trick**, but it is also **not a reliable window into multi-step reasoning**. The most defensible summary:

- **CoT genuinely improves accuracy** on structured, step-decomposable tasks (especially math and logic) in sufficiently large models — but this is narrower than often claimed.
- **CoT explanations are frequently unfaithful**: the chain often does not reflect what actually drove the answer, and this appears to be structurally robust to further training.
- **The causal role of CoT is contested and model-dependent**: some models pre-compute answers and rationalize them; others integrate CoT as one of several parallel answer sources.
- **Scale is necessary but not sufficient**, and task type is a stronger predictor of CoT effectiveness than scale alone.
- The field does **not have a unified mechanistic account** of CoT, and expert consensus as of 2024–2025 was not retrievable in this analysis — this is a genuine open question.

The practical upshot: CoT is a useful heuristic for math and logic tasks on large models, but treating CoT explanations as faithful representations of model reasoning is not warranted by current evidence.