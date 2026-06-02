# Ablation summary — verifier ON vs OFF (v1 Step 7.2)

> One row per prompt. Each Tier-1 metric is shown OFF → ON with the delta (Δ = ON − OFF). A positive Δgrounding / Δjudge means the **verifier** component helped; a positive Δfabrication means it caused MORE fabrication (so a [red]regression[/red] in the ON arm). Cost / elapsed deltas are markup attributable to ON.


| prompt | g (off) | g (on) | Δg | fab (off) | fab (on) | Δfab | cov (off) | cov (on) | Δcov | judge (off) | judge (on) | Δjudge | $ off | $ on | Δ$ | t off (s) | t on (s) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| What are the real-world risks and benefits of using synthet… | 100% | 94% | -6% | 0% | 0% | +0% | 57% | 86% | +29% | 4.00 | 3.50 | -0.50 | $0.7586 | $0.7500 | -$0.0087 | 325.8 | 323.5 |
| Is chain-of-thought prompting an effective reasoning strate… | 100% | 100% | +0% | 0% | 0% | +0% | 57% | 57% | +0% | 3.80 | 3.30 | -0.50 | $0.8465 | $0.9643 | +$0.1178 | 295.3 | 391.8 |
| What is the current state of inference-time compute scaling… | 92% | 94% | +2% | 0% | 0% | +0% | 57% | 71% | +14% | 3.50 | 3.30 | -0.20 | $0.8408 | $1.0122 | +$0.1714 | 323.8 | 423.5 |
| Map the research landscape of multi-agent LLM systems. Prod… | 100% | 100% | +0% | 0% | 0% | +0% | 71% | 29% | -43% | 2.50 | 2.00 | -0.50 | $0.7055 | $0.7511 | +$0.0456 | 324.6 | 378.6 |
| What's the current consensus on long-context models vs. ret… | 100% | 100% | +0% | 0% | 0% | +0% | 71% | 57% | -14% | 4.00 | 3.50 | -0.50 | $0.9090 | $0.7460 | -$0.1630 | 407.7 | 341.7 |
| Compare reported costs and latencies for production agentic… | 87% | 100% | +13% | 0% | 0% | +0% | 86% | 71% | -14% | 3.00 | 3.00 | +0.00 | $1.0121 | $1.2163 | +$0.2041 | 381.4 | 474.5 |

### Aggregate (mean Δ across both-arms-succeeded prompts)


- mean Δgrounding: **+2%**  (n=6)
- mean Δfabrication: **+0%**  (n=6)
- mean Δcoverage: **-5%**  (n=6)
- mean Δjudge.overall: **-0.37**  (n=6)
- mean Δcost: **+$0.0612**  (n=6)

### Per-prompt outputs

**1. What are the real-world risks and benefits of using synthetic data to train or …**
  - OFF: [final](./final_20260505T071011_9a67a44c_what_are_the_real_world_risks_and_benefi__off.md)  |  [metrics](./metrics_20260505T071011_9a67a44c_what_are_the_real_world_risks_and_benefi__off.md)
  - ON: [final](./final_20260505T071728_eec15ef0_what_are_the_real_world_risks_and_benefi__on.md)  |  [metrics](./metrics_20260505T071728_eec15ef0_what_are_the_real_world_risks_and_benefi__on.md)

**2. Is chain-of-thought prompting an effective reasoning strategy for LLMs, or does…**
  - OFF: [final](./final_20260505T072440_3aa43f4e_is_chain_of_thought_prompting_an_effecti__off.md)  |  [metrics](./metrics_20260505T072440_3aa43f4e_is_chain_of_thought_prompting_an_effecti__off.md)
  - ON: [final](./final_20260505T073246_0e5db119_is_chain_of_thought_prompting_an_effecti__on.md)  |  [metrics](./metrics_20260505T073246_0e5db119_is_chain_of_thought_prompting_an_effecti__on.md)

**3. What is the current state of inference-time compute scaling for LLM reasoning? …**
  - OFF: [final](./final_20260505T073951_5dda3f7d_what_is_the_current_state_of_inference_t__off.md)  |  [metrics](./metrics_20260505T073951_5dda3f7d_what_is_the_current_state_of_inference_t__off.md)
  - ON: [final](./final_20260505T074831_4f673be0_what_is_the_current_state_of_inference_t__on.md)  |  [metrics](./metrics_20260505T074831_4f673be0_what_is_the_current_state_of_inference_t__on.md)

**4. Map the research landscape of multi-agent LLM systems. Produce a structured rep…**
  - OFF: [final](./final_20260505T075529_ed33dba3_map_the_research_landscape_of_multi_agen__off.md)  |  [metrics](./metrics_20260505T075529_ed33dba3_map_the_research_landscape_of_multi_agen__off.md)
  - ON: [final](./final_20260505T080320_cdb62681_map_the_research_landscape_of_multi_agen__on.md)  |  [metrics](./metrics_20260505T080320_cdb62681_map_the_research_landscape_of_multi_agen__on.md)

**5. What's the current consensus on long-context models vs. retrieval for long-docu…**
  - OFF: [final](./final_20260505T081121_fab15f4c_what_s_the_current_consensus_on_long_con__off.md)  |  [metrics](./metrics_20260505T081121_fab15f4c_what_s_the_current_consensus_on_long_con__off.md)
  - ON: [final](./final_20260505T081832_20921f6d_what_s_the_current_consensus_on_long_con__on.md)  |  [metrics](./metrics_20260505T081832_20921f6d_what_s_the_current_consensus_on_long_con__on.md)

**6. Compare reported costs and latencies for production agentic-RAG built on LangGr…**
  - OFF: [final](./final_20260505T082628_b80fda64_compare_reported_costs_and_latencies_for__off.md)  |  [metrics](./metrics_20260505T082628_b80fda64_compare_reported_costs_and_latencies_for__off.md)
  - ON: [final](./final_20260505T083550_26abd373_compare_reported_costs_and_latencies_for__on.md)  |  [metrics](./metrics_20260505T083550_26abd373_compare_reported_costs_and_latencies_for__on.md)
