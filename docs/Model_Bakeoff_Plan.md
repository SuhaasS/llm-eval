# Cost-Efficient LLM Bakeoff for Agentic Coding

**Selecting a lower-cost model on AWS Bedrock to replace Claude Sonnet 5**

Prepared by Suhaas Surapaneni · Pindrop · July 2026 (refreshed July 29, 2026)

---

## Executive Summary

- **Model comparison:** Gemma 4 31B and Nemotron 3 Super 120B are cheapest (~95-96% savings); Kimi K2.5 leads on vendor-reported benchmarks but saves less (~80-82%).
- **Model comparison:** Vendor vs. independent benchmarks disagree sharply (Kimi's SWE-bench lead reverses under independent testing), so a blind internal eval on real repos decides.
- **LiteLLM vs. OpenRouter:** Route via a self-hosted LiteLLM proxy calling Bedrock directly - OpenRouter's Bedrock path still transits their servers plus a ~5% fee.
- **Timeline:** ~2-2.5 week bakeoff (build eval, run, score, recommend), then a 10-20% canary rollout with Sonnet 5 fallback.

## Recommendation

Our agentic coding workloads (project/feature-scale, multi-file work) currently run primarily on Claude Sonnet 5. Advance **Nemotron 3 Super 120B**, **Gemma 4 31B**, and **Kimi K2.5** to a ~2 week bakeoff on real Pindrop tasks. Nemotron is the cheapest of the three after Gemma and has the largest output ceiling, though its benchmark coverage leans more heavily on vendor-reported numbers than the other two; Kimi leads on the one benchmark it publishes itself (SWE-bench Verified, unverified) but trails on the one independently-run benchmark that covers it; Gemma leads on cost, license, and the independent benchmark data that exists, though it's the newest of the three on Bedrock. All three cut spend 80%+ vs. Sonnet 5, so the decision should turn on measured task quality on our own repos, not price or benchmark scores.

## Candidates at a Glance

| Model | SWE-bench Verified | Terminal-Bench Hard (agentic CLI, independent) | Bedrock price (in/out, per 1M) | Context / Max output |
|---|---|---|---|---|
| **Kimi K2.5** | 76.8% (vendor) | 34.8% | ~$0.60 / $2.50–3.00 | 256K / 16K |
| **Nemotron 3 Super 120B** | 60.5% (vendor) | 25.8% (vendor)§ | $0.15 / $0.65 | 256K / 32K |
| **Gemma 4 31B** | Not published | 36.4% | $0.14 / $0.40 | 256K / not disclosed |
| Claude Sonnet 5 (reference) | 85.2% (vendor)† | Not published | $3.00 / $15.00 (standard) | 1M / 128K |

SWE-bench Verified scores exist for Kimi, Nemotron, and Sonnet 5 (published by Moonshot, NVIDIA, and Anthropic respectively; NVIDIA is the only one of the three to disclose multiple harness variants, 53.7-60.5% depending on harness, rather than one cherry-picked number) - Gemma has none published anywhere we could find. Treat all published scores with a discount: OpenAI stopped reporting this benchmark for frontier evals in Feb 2026 after an audit of 138 tasks found ~59% had material test/problem-design flaws (the single largest category, overly narrow test cases, was ~35% on its own) and every frontier model tested had memorized the human-written fix - it's the most contamination-prone number in this table, included because it's the only benchmark with any real coverage, not because it's trustworthy.

### Artificial Analysis Index Scores

Independent, composite scores from Artificial Analysis - a different lens than the task-specific benchmarks above, so kept in its own table rather than mixed in:

| Model | AA Intelligence Index | AA Coding Index | Output speed (tok/s) |
|---|---|---|---|
| **Kimi K2.5** | 38 | 46.8 | 49.9 |
| **Nemotron 3 Super 120B** | 25 | 37.7 | 148.9 |
| **Gemma 4 31B** | 29.4 | 43.4 | 35.3 |
| Claude Sonnet 5 (reference) | 53 | 71.5 | 74.3 |

## Cost Comparison

At ~665M input + 200M output tokens/month (volume rescaled so the Sonnet 5 baseline lands at $5,000/month):

| Model | Price (in/out, per 1M) | Est. monthly cost | Savings vs. Sonnet 5 |
|---|---|---|---|
| Gemma 4 31B | $0.14 / $0.40 | ~$175 | ~96% |
| Nemotron 3 Super 120B | $0.15 / $0.65 | ~$230 | ~95% |
| Kimi K2.5 | ~$0.60 / $2.50–3.00 | ~$900–1,000 | ~80–82% |
| Claude Sonnet 5 | $3.00 / $15.00 | ~$5,000 | baseline |

Artificial Analysis found Sonnet 5's cost *per completed task* runs ~15% higher than Opus 4.8's despite its lower per-token price, because it uses more tokens per task - a reminder that cost per completed workflow, not per token, should decide this bakeoff.

## Model Trade-offs

- **Kimi K2.5** - leads on the vendor-reported SWE-bench Verified figure (76.8%) and has the highest AA Intelligence Index and AA Coding Index of the three candidates (38 and 46.8), but the one time an independent evaluator actually tested it (Terminal-Bench Hard), it scored 34.8% - below Gemma's 36.4% and well below its own vendor-claimed 50.8% on a different Terminal-Bench track. Treat its self-reported lead as a hypothesis, not a fact - this is direct evidence, not just a caveat, that vendor numbers and independent numbers can disagree by double digits. 16K max output, the smallest disclosed of the three (Gemma's isn't disclosed); watch for truncation on large diffs. Modified MIT license: above 100M MAU or $20M/month revenue, "Kimi K2.5" must be displayed in-product - not a blocker at Pindrop's scale.
- **Nemotron 3 Super 120B** - cheapest after Gemma ($0.15/$0.65), and the largest output ceiling of the three (32K). Weakest vendor-reported SWE-bench Verified of the three (60.5%), though NVIDIA is the only vendor here to disclose multiple harness variants rather than one cherry-picked number. AA Intelligence Index (25) and AA Coding Index (37.7) are both genuinely published - though its Terminal-Bench Hard figure (25.8%) is vendor-reported, not independently run like Gemma's and Kimi's. NVIDIA Nemotron Open Model License: no revenue or MAU threshold turned up in what we reviewed, but confirm full terms with legal like the others.
- **Gemma 4 31B** - cheapest, cleanest license (Apache 2.0, no thresholds), strong generalist with native function calling, and now the leader among the three candidates on both independent benchmarks that exist for it: AA Coding Index (43.4) and Terminal-Bench Hard (36.4%). No proven repo-issue-resolution track record - no SWE-bench Verified score exists for it. Newest on Bedrock (June 2026), so least production history of the three; served via the `bedrock-mantle` endpoint rather than the standard `bedrock-runtime` endpoint, which is why it may not appear under the console's Serverless model-catalog filter.

## Model Access Layer: LiteLLM vs. OpenRouter

**LiteLLM as proxy.** LiteLLM is an open-source gateway we can self-host that gives us one unified way to call and switch between all the candidate models, plus built-in cost tracking. Since it runs inside our own AWS account and talks to Bedrock directly, none of our data passes through a third-party vendor.

**Why not OpenRouter.** OpenRouter now offers a "Bring Your Own Key" mode that can reach a customer's own Bedrock account, but under BYOK requests are still proxied through OpenRouter's own servers before reaching Bedrock, and OpenRouter charges roughly 5% on top of Bedrock's normal cost. For a shop standardized on Bedrock for pricing and data-residency reasons, routing traffic through a third-party vendor's servers - even briefly, even under BYOK - cuts against the reason we're on Bedrock in the first place, so OpenRouter isn't being evaluated as the routing layer.

## Eval Approach

No public benchmark has full, contamination-clean coverage of all three candidates, so a two-tier internal eval set on real Pindrop repos is the primary decision input:

- **Tier A - bug fix** (~20–30 tasks): 1–2 files, clear pass/fail.
- **Tier B - feature scale** (~20–30 tasks): multi-file implementations/refactors, graded on cross-file consistency and end-to-end correctness. This is where the recommendation should actually be won or lost.

Grade on: task completion, tool-execution accuracy, cross-file consistency, tests passing, reviewer pass-through rate, latency, and **cost per completed workflow** (not per token). N≥3 samples per task, graded blind.

**Success criteria:** task completion ≥90% of Sonnet 5 (weighted to Tier B); terminal-command success ≥95%; cross-file changes correct ≥90%; cost ≤40% of Sonnet 5 per completed workflow.

## Timeline (~2.5–3 Weeks)

- **Phase 0 (~1 day):** Enable Bedrock model access for all three (auto-subscribe, allow ~15 min propagation), confirm region/`bedrock-mantle` endpoint per model, check Service Quotas (a quota increase can take ~1 business day), and re-check SWE-rebench and AA's Terminal-Bench Hard leaderboard for any newly-published independent scores on Nemotron or Sonnet 5.
- **Phase 1 (~1-1.5 weeks):** Build the two-tier eval set; run all three models N≥3 samples/task, graded blind.
- **Phase 2 (~2–3 days):** Score against success criteria; compute cost per completed workflow; compare to the Sonnet 5 baseline.
- **Phase 3 (~2 days):** Recommend one model; plan a 10–20% canary rollout with Sonnet 5 fallback.

## Decisions Needed

- Approve the candidate lineup (Nemotron 3 Super 120B, Gemma 4, Kimi K2.5) and the ~2.5–3 week timeline.
- Confirm Bedrock access, IAM permissions, and any needed Service Quota increases for the three models.
- Approve the 40–60 task two-tier eval set and the success thresholds above.
- Confirm Sonnet 5 (not Opus 4.8) is the correct baseline, and which pricing tier - standard ($3.00/$15.00, used throughout this report)
- Legal sign-off on Kimi's modified-MIT license and Nemotron's Nemotron Open Model License terms before production rollout.

## Sources

**Nemotron 3 Super 120B**
- SWE-bench Verified (60.5%, OpenHands harness; 53.7–60.5% range across harnesses) and vendor Terminal-Bench Hard figure (25.8%): [NVIDIA Nemotron 3 Super 120B model card](https://build.nvidia.com/nvidia/nemotron-3-super-120b-a12b/modelcard)
- Conflicting Terminal-Bench Hard figure (29%): [Artificial Analysis — Nemotron 3 Super article](https://artificialanalysis.ai/articles/nvidia-nemotron-3-super-the-new-leader-in-open-efficient-intelligence)
- Bedrock pricing ($0.15/$0.65), context/max output (256K/32K), model ID: [AWS Bedrock model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-nvidia-nemotron-super-3-120b.html) · [AWS Bedrock pricing](https://aws.amazon.com/bedrock/pricing/)
- AA Intelligence Index (25) and output speed (148.9 tok/s): [Artificial Analysis model page](https://artificialanalysis.ai/models/nvidia-nemotron-3-super-120b-a12b)
- AA Coding Index (37.7): [BenchLM.ai AA Coding Index leaderboard](https://benchlm.ai/benchmarks/aaCodingIndex)
- License terms: [NVIDIA Nemotron Open Model License](https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-nemotron-open-model-license)
- Bedrock GA date (March 2026): [AWS What's New](https://aws.amazon.com/about-aws/whats-new/2026/03/amazon-bedrock-nemotron-3-super)

**Kimi K2.5**
- SWE-bench Verified (76.8%) and vendor Terminal-Bench 2.0 figure (50.8%): [Kimi K2.5 GitHub](https://github.com/MoonshotAI/Kimi-K2.5) · [Hugging Face model card](https://huggingface.co/moonshotai/Kimi-K2.5)
- Independent Terminal-Bench Hard figure (34.8%): [Artificial Analysis Terminal-Bench Hard leaderboard](https://artificialanalysis.ai/evaluations/terminalbench-hard)
- Bedrock pricing, context/max output, model ID, Bedrock launch date (Jan 2026): [AWS Bedrock model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-moonshot-ai-kimi-k2-5.html)
- AA Intelligence Index (38) and default output speed (49.9 tok/s): [Artificial Analysis model page](https://artificialanalysis.ai/models/kimi-k2-5)
- AWS Bedrock-specific output speed (118 tok/s): [Artificial Analysis providers page](https://artificialanalysis.ai/models/kimi-k2-5/providers)
- License terms (100M MAU / $20M revenue threshold): [Kimi K2.5 LICENSE](https://github.com/MoonshotAI/Kimi-K2.5/blob/master/LICENSE)

**Gemma 4 31B**
- Terminal-Bench Hard (36.4%): [Artificial Analysis — Gemma 4 article](https://artificialanalysis.ai/articles/gemma-4-everything-you-need-to-know)
- Bedrock pricing, context, model ID, native function calling: [AWS Bedrock model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-google-gemma-4-31b.html) · [AWS Bedrock pricing](https://aws.amazon.com/bedrock/pricing/)
- AA Intelligence Index (29.4) and output speed (35.3 tok/s): [Artificial Analysis model page](https://artificialanalysis.ai/models/gemma-4-31b)
- License (Apache 2.0): [Google's Gemma 4 announcement](https://opensource.googleblog.com/2026/03/gemma-4-expanding-the-gemmaverse-with-apache-20.html)
- Bedrock GA date (June 2026): [AWS What's New](https://aws.amazon.com/about-aws/whats-new/2026/06/gemma-4-amazon-bedrock/)

**Claude Sonnet 5**
- SWE-bench Verified (85.2%) and the Pro-vs-Verified mislabeling risk (Pro = 63.2%): [Vellum — Sonnet 5 benchmarks explained](https://www.vellum.ai/blog/claude-sonnet-5-benchmarks-explained) · [Apidog — Sonnet 5 benchmarks](https://apidog.com/blog/claude-sonnet-5-benchmarks/)
- Independent Terminal-Bench v2.1 figure (74.53%): [vals.ai Terminal-Bench 2.1 leaderboard](https://www.vals.ai/benchmarks/terminal-bench-2-1)
- Vendor Terminal-Bench figure (80.4%): [Vellum — Sonnet 5 benchmarks explained](https://www.vellum.ai/blog/claude-sonnet-5-benchmarks-explained)
- Bedrock standard + introductory pricing, context/max output, model ID: [AWS Bedrock model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-5.html) · [AWS launch blog](https://aws.amazon.com/blogs/machine-learning/introducing-claude-sonnet-5-on-aws-anthropics-most-capable-sonnet-model/)
- AA Intelligence Index (53) and output speed (74.3 tok/s): [Artificial Analysis model page](https://artificialanalysis.ai/models/claude-sonnet-5)
- AA Coding Index (71.5): [BenchLM.ai Sonnet 5 page](https://benchlm.ai/models/claude-sonnet-5)
- Cost-per-completed-task finding (~15% above Opus 4.8): [Artificial Analysis — Sonnet 5 agentic cost article](https://artificialanalysis.ai/articles/claude-sonnet-5-agentic-cost)

**Model Access Layer**
- LiteLLM Bedrock provider support, self-hosted proxy/gateway architecture: [LiteLLM — Bedrock provider docs](https://docs.litellm.ai/docs/providers/bedrock) · [LiteLLM — Proxy Server docs](https://docs.litellm.ai/docs/simple_proxy) · [LiteLLM — Proxy reliability/fallback docs](https://docs.litellm.ai/docs/proxy/reliability) · [BerriAI/litellm GitHub](https://github.com/BerriAI/litellm/)
- OpenRouter Bring-Your-Own-Key support for Amazon Bedrock: [OpenRouter — BYOK use case docs](https://openrouter.ai/docs/use-cases/byok) · [OpenRouter — BYOK auth guide](https://openrouter.ai/docs/guides/overview/auth/byok) · [OpenRouter — Amazon Bedrock provider page](https://openrouter.ai/provider/amazon-bedrock)
- OpenRouter BYOK data path (traffic proxied through OpenRouter's servers before reaching the customer's Bedrock account): [stormacq.com — Xcode + OpenRouter + Bedrock walkthrough](https://stormacq.com/2026/02/19/xcode-openrouter-bedrock.html)
- OpenRouter BYOK ~5% fee on top of underlying provider cost: [OpenRouter Help Center — BYOK billing](https://openrouter.zendesk.com/hc/en-us/articles/43219817892123-Why-Am-I-Still-Being-Charged-When-Using-My-Own-Key-BYOK)

**Methodology / cross-cutting**
- OpenAI's SWE-bench Verified deprecation and audit findings (~59% of tasks flawed, contamination evidence): [OpenAI — Why we no longer evaluate SWE-bench Verified](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/)
- Terminal-Bench 2.0 / 2.1 / Hard as distinct, non-comparable tracks: [tbench.ai — Terminal-Bench 2.1 announcement](https://www.tbench.ai/news/terminal-bench-2-1) · [tbench.ai benchmarks](https://www.tbench.ai/benchmarks)
- AA Intelligence Index composition (9 evaluations): [Artificial Analysis methodology](https://artificialanalysis.ai/methodology/intelligence-benchmarking)
- AA Coding Index composition (Terminal-Bench v2.1 + SciCode): [Artificial Analysis coding capabilities page](https://artificialanalysis.ai/models/capabilities/coding)
