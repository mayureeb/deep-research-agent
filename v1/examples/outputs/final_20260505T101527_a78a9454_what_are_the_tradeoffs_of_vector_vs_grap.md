<!--
Original prompt: What are the tradeoffs of vector vs. graph RAG?
Detected format: Narrative summary in markdown with structured sections, as the prompt does not specify a format and asks an open-ended tradeoff question.
-->

> **Original prompt:** What are the tradeoffs of vector vs. graph RAG?
> **Detected format:** Narrative summary in markdown with structured sections, as the prompt does not specify a format and asks an open-ended tradeoff question.

---

# Vector RAG vs. Graph RAG: Tradeoffs

> **Note on evidence quality:** Many comparisons between vector and graph RAG rely on blog posts, vendor documentation, and small proprietary benchmarks with limited experimental controls. Where peer-reviewed evidence is available and contradicts industry sources, this is noted explicitly. Quantitative figures should be interpreted with caution.

---

## 1. Core Architecture and Retrieval Mechanism

The two approaches differ fundamentally in how they represent and retrieve knowledge.

**Vector RAG** converts documents into dense vector embeddings and stores them in a vector database. At query time, the question is embedded using the same model, and retrieval proceeds via cosine similarity (nearest-neighbour search) against stored vectors. The process is: *chunk → embed → store → query-embed → nearest-neighbour → generate*.

**Graph RAG** represents knowledge as a typed entity graph — nodes are entities, edges are typed relationships (e.g., "acquired," "caused," "mitigated"). At query time, the system identifies anchor entities in the query, then *traverses* the graph (often via structured queries such as Cypher) to collect a subgraph of supporting evidence, which is then compressed and passed to the LLM. The fundamental shift: *retrieval becomes traversal*, following paths that embedding similarity would never surface.

Graph RAG's offline indexing pipeline is substantially more complex. It requires LLM-driven entity and relationship extraction, community detection (e.g., the Leiden algorithm used in Microsoft's GraphRAG), and hierarchical summary generation. Vector RAG's pipeline requires only chunking and embedding.

---

## 2. Retrieval Accuracy and Answer Quality

### Query-type complementarity

The two approaches show **complementary strengths** rather than a consistent winner:

| Query Type | Vector RAG | Graph RAG |
|---|---|---|
| Single-hop factual | ✅ Better | ❌ Weaker |
| Detail-oriented QA | ✅ Better | ❌ Community search sacrifices detail |
| Multi-hop / relational | ❌ Cannot traverse relationships | ✅ Better |
| Thematic / corpus-wide summarization | ❌ Weaker | ✅ Better (community aggregation) |
| Temporal & numerical reasoning | ❌ Weaker | ✅ Better (where tested) |
| Tabular reasoning | ~Equal | ~Equal |

Community-based global-search Graph RAG can sacrifice query-specific detail — hurting detail-oriented QA — while providing corpus-level aggregation that benefits broad or diverse summarization tasks.

### Quantitative results (with caveats)

- A domain-specific telecom (ORAN) benchmark found that Hybrid GraphRAG improved factual correctness by **8%** and GraphRAG improved context relevance by **7%** over traditional vector RAG.
- An industry benchmark (Lettria) reported GraphRAG answering **81.67%** of queries correctly vs. **57.50%** for vector RAG, with especially large gaps on temporal and numerical reasoning — but this is a small-scale proprietary study with limited transparency.
- A peer-reviewed study found that large reported gains from GraphRAG are likely **artifacts of evaluation biases** (position bias, length bias, unrelated questions). After correcting for these, gains became "much more moderate or even vanish." For example, LightRAG's apparent win rate of 72% vs. 28% over NaiveRAG flipped after bias elimination, with NaiveRAG slightly outperforming LightRAG.

---

## 3. Indexing, Storage, and Computational Cost

### Vector RAG costs

- **Indexing** is fast and simple: chunk documents, embed them, store vectors.
- **Storage** is configurable. HNSW indexes add modest overhead (~4–5% on top of raw vectors). Product quantization (IVF+PQ) can compress a 10M-vector dataset from ~38 GB to ~3.5 GB with minimal accuracy loss.
- **Scaling RAM**: At billion-vector scale, HNSW clusters can require very large memory (one reported case: 3 TB of RAM across a cluster). Disk-based alternatives like DiskANN can serve comparable data from a single 96 GB RAM machine, dramatically reducing cost.

### Graph RAG costs

- **Indexing** requires multiple expensive LLM passes for entity/relationship extraction, community detection, and summary generation — significantly more compute than vector embedding.
- **Setup** demands specialized expertise in data modeling, ontology design, and knowledge graph management. This is cited as the primary reason knowledge graphs have seen slower adoption.
- **Maintenance** requires a metadata governance layer (entity lifecycle management, lineage tracking, quality monitoring) to prevent the graph from becoming a stale, incorrect snapshot.

> **Gap in evidence:** No findings were retrieved on absolute indexing time or latency costs of building knowledge graphs at scale vs. vector indexes. The computational cost comparison is partially uncharacterized by available research.

---

## 4. Knowledge Updates and Data Freshness

| Dimension | Vector RAG | Graph RAG |
|---|---|---|
| Update model | Incremental (re-embed the new document) | Batch (re-index entities and communities) |
| Suitability for dynamic data | ✅ Scales easily across large, evolving datasets | ❌ Not designed for real-time updates |
| Risk of staleness | Lower | Higher — requires governance layer to stay current |

Vector RAG supports per-document incremental updates. Graph RAG requires batch re-indexing of entities and communities whenever new content is added, making it less suited to rapidly changing corpora. Without active metadata governance, the knowledge graph risks becoming a stale snapshot that produces incomplete or incorrect answers.

---

## 5. Explainability and Provenance

This is the **most contested** dimension in existing evidence.

**Industry/vendor sources** argue that Graph RAG offers inherently higher explainability: the reasoning path through nodes and edges is transparent and traceable, whereas vector embeddings are abstract numerical representations that are not human-readable. This makes Graph RAG preferred in regulated domains (finance, healthcare, law) where auditing retrieved evidence matters.

**A peer-reviewed source** challenges this framing, arguing that all RAG systems — including Graph RAG — are essentially "black boxes" by default, and that genuine traceability requires an additional external XAI framework (e.g., KG-SMILE, which applies controlled perturbations to identify which graph nodes and edges influenced a response). On this view, Graph RAG's explainability advantage is not intrinsic but requires additional tooling to realize.

**Bottom line:** Graph RAG's graph structure *can* support better provenance tracking than vector similarity scores, but whether this translates to practical explainability depends on tooling and implementation. The confidence on this dimension is low (0.55–0.58); treat explainability claims from vendor sources with caution.

---

## 6. Hybrid Approaches

Hybrid systems that combine vector similarity search with graph traversal consistently outperform either method alone:

- A multi-sector study (finance, healthcare, industry, law) reported hybrid approaches improving answer correctness from ~50% (vector-only) to ~80%+, with especially large gains in complex technical domains (industry sector: ~91% vs. ~47% for vector-only).
- The ORAN telecom benchmark found Hybrid GraphRAG outperforming both standalone Graph RAG and vector RAG on factual correctness.

Hybrid approaches are most appropriate when queries span both precise factual recall (vector strength) and relational/multi-hop reasoning (graph strength). However, these results come from domain-specific benchmarks; generalizability to open-domain tasks is not established.

---

## 7. Methodological Limitations of Existing Benchmarks

This is the area with the **highest-confidence findings** — and the most important caveat on all quantitative claims above.

- **Heterogeneous protocols:** GraphRAG systems are evaluated with varying graph construction methods, retrieval configurations, and evaluation criteria, making cross-study comparisons unprincipled.
- **Position bias in LLM-as-a-Judge:** Summarization evaluations using LLM-as-a-Judge are highly sensitive to the order in which candidate summaries are presented, introducing strong position effects that may confound conclusions.
- **Inadequate benchmarks:** Current evaluations predominantly rely on traditional QA datasets whose limited scope fails to assess the reasoning capacity improvements Graph RAG may enable — meaning Graph RAG's advantages on complex reasoning tasks may be understated *and* its reported accuracy gains on simple tasks may be overstated.
- **Evaluation biases inflate gains:** A peer-reviewed study found that reported performance advantages of GraphRAG-style systems become "much more moderate or even vanish" after correcting for position bias, length bias, and inclusion of unrelated questions.

---

## Summary: When to Choose Which

| Factor | Favor Vector RAG | Favor Graph RAG | Favor Hybrid |
|---|---|---|---|
| Query type | Single-hop, factual | Multi-hop, relational, thematic | Mixed query portfolios |
| Data dynamism | High (frequent updates) | Low (stable domains) | Moderate |
| Setup cost | Low | High (expertise + governance) | High |
| Explainability needs | Low | High (regulated domains) | High |
| Corpus size/scale | Any (with quantization) | Domains with clear entity structure | Large, structured domains |
| Evaluation confidence | Moderate | **Low** (benchmarks are immature) | Low–Moderate |

---

## Key Caveats

1. Most industry/vendor comparisons lack rigorous experimental controls and may reflect promotional framing.
2. Quantitative performance gaps (e.g., Lettria's 81.67% vs. 57.50%) come from small proprietary studies and cannot be independently verified.
3. Peer-reviewed evidence suggests corrected Graph RAG gains are moderate (below 10% win-rate difference) rather than dramatic.
4. Hybrid RAG results are domain-specific (telecom, finance, healthcare, law) and may not generalize.
5. The field's evaluation methodology is acknowledged by peer-reviewed sources to be immature.