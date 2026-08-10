# Pending Tasks

Single list of open work for the LLM bakeoff eval. **This file is the backlog.**
`tasks/todo.md` is the opposite — a completed-work review log, one section per
finished task. Nothing here is done; move it there when it is.

Last updated 2026-08-08, after the N=3 live run that took Kimi to 3/3.

Spec: [docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md](docs/superpowers/specs/2026-08-03-llm-bakeoff-eval-design.md)
Harness plan: [docs/superpowers/plans/2026-08-04-bakeoff-harness-logging.md](docs/superpowers/plans/2026-08-04-bakeoff-harness-logging.md)

---

## P0 — Blocking Phase 0c go/no-go

Latest N=3 live run, 12 runs across 4 arms: **3 of 4 arms pass, 3/3 each.**
Sonnet 5 (bedrock-runtime), Nemotron and Kimi K2.5 all produce the correct diff
on every run. **Gemma is the only arm still failing** — 9/9 now, identically.

Kimi went 0/3 → 3/3 with no model change. Its failure was LiteLLM rewriting
`functions.Read:0` to `functions_Read_0`; see `tasks/todo.md`. That is the
second of two arms whose "model failure" turned out to be an adapter defect,
which is worth weighing when reading Gemma below.

- [ ] **Gemma 4 31B — `JSON-RPC error -32602: Job registration failed ...
  Generation failed`.** Bedrock-side, not a parameter rejection. **Observed
  6/6**, identically: 1 turn, 0 tool calls, 2 wire calls, no diff. Deterministic.
  The error has two surface forms — a plain `BadRequestError` when it lands
  before the stream and a `MidStreamFallbackError` when it lands during it.
  One fault, not two. At N=3 every proxy-side error in the run attributed to
  this arm and to no other.
  Next: this arm has produced no tool call in any run, so nothing is known
  about whether Gemma can drive Claude Code at all. Decide whether it is
  fixable at the adapter layer, or whether Gemma leaves the eval — that
  decision sets the arm count, which sizes the dataset, so make it before
  Phase 3.
  **Weigh the base rate before concluding capability.** Two of the three
  original "model failures" — Sonnet's and Kimi's — turned out to be adapter
  defects with one-line causes, and both looked equally deterministic and
  equally total beforehand. Gemma's error is Bedrock-side rather than in the
  bridge, which is a genuine difference, but it is not yet evidence about the
  model.

- [ ] **`kimi-k2-5-runtime` is newly unsafe** and must not be run without a
  fix. The tool-id patch removes the sanitizer process-wide; on the `bedrock/`
  Converse route the id becomes Bedrock's `toolUseId`, which enforces its own
  character class, so Kimi's `functions.Read:0` would now be rejected there.
  Harmless for every arm in `EVAL_ARMS` (Sonnet-runtime emits compliant
  `toolu_bdrk_…` ids, verified post-patch), but the deployment is configured
  and would fail if selected. Either scope the patch by resolved provider or
  drop the deployment.

- [ ] **Record the Sonnet transport asymmetry as a §6.4 confound.** Sonnet 5
  now signs SigV4 against bedrock-runtime while all three candidates go through
  the mantle passthrough. Different code path, different request shape: on
  runtime, beta features ride as an `additionalModelRequestFields.anthropic_beta`
  *body* field rather than an `anthropic-beta` header. The reference arm is
  therefore not transport-identical to the arms it is the reference for, and
  any Sonnet-vs-candidate delta carries that. It needs to be stated wherever
  the comparison is published, not just known here.

- [ ] **Re-run the four-arm smoke to a real GO.** The N=3 criterion is
  implemented and enforced (`smoke_test.py --repeats`, default 3 live, GO
  requires every repeat of every arm; a run below N=3 prints an explicit
  weaker-than-criterion warning). Latest run: **NO-GO at 3 of 4 arms** —
  Sonnet 5 3/3, Nemotron 3/3, Kimi 3/3, Gemma 0/3.
  Blocked on Gemma alone, not on the gate.

---

## P1 — Landmines that only fire at scale

None of these can appear at N=1. All will appear at N=10.

- [ ] **Sonnet's cost is order-dependent: 2.9× spread on byte-identical work.**
  Measured at N=3, three runs of the same one-line fix, same 8 turns, same
  157-byte diff:

  | run | cache_write | cache_read | cost |
  |---|---|---|---|
  | 1 | 125,853 | 210,149 | **$0.548** |
  | 2 | 118,499 | 217,519 | $0.522 |
  | 3 | 34,166 | 259,646 | $0.217 |

  Reproduced across two separate N=3 runs, with the *shape* stable and the
  *breakpoint* not: the first run measured 2.9× and the second 2.5×, and which
  run gets the cheap one moved. So this is not a fixed warm-up cost that could
  be subtracted — it depends on cache state at dispatch.

  The Bedrock prompt cache **persists across runs**, so a run that finds it
  cold pays cache_write at 1.25× and later runs read at 0.10×. Nothing about
  the model or the task changed. §5.7 randomizes and interleaves execution
  order, which means at N=10 per task each sample's cost depends on where the
  scheduler happened to put it — cost-per-task becomes partly an artifact of
  scheduling.
  It lands on one arm only: Sonnet is the sole arm with caching, so
  Sonnet-vs-candidate cost is confounded twice over (order, and the presence
  of caching at all).
  Next: decide the policy before any cost figure is published — report
  steady-state cost from warm runs, report cache-write separately, or pin
  execution order per task. All three are defensible; silently averaging is
  not.

- [ ] **Kimi's cost is unknown on any cache-warm run, and no rate exists.**
  The record-destroying half of this is fixed (see `tasks/todo.md`): a pricing
  failure now costs the price only, and the tokens survive so the run stays
  repriceable. What remains is that **there is no rate to reprice it with**.
  Measured 2026-08-08: Kimi returned `cache_read` on 2 of 3 runs (33,152 each),
  so those two records carry `cost_usd: null`. At N=10 per task the cache is
  warm far more often than not, so most Kimi rows will carry no cost at all.
  Nemotron and Gemma returned no cache fields in 9 runs; Sonnet is priced.
  **Blocks any published cost figure involving Kimi**, and it cannot be
  resolved inside the harness — see the pricing-permissions item below.
  Decide the fallback now in case AWS publishes nothing: report Kimi cost as a
  lower bound from cold runs, or exclude Kimi from cost claims and say so.

- [ ] **Request `pricing:GetProducts` and `pricing:DescribeServices`** on the
  `BedrockModelTester` SSO role. Read-only, free, no data access. It is the
  only way to settle whether AWS publishes cache rates for the candidates,
  which retires the guard properly rather than working around it.
  Checked 2026-08-07: **Bedrock returns no dollar figure on any call** — the
  API and its response headers carry token counts only, so the price book is
  the sole pricing authority and a gap in it must be reported, never
  estimated. Cost Explorer and CloudWatch are also denied to this role, and
  neither can attribute spend to a `run_id` anyway. Application inference
  profiles with cost-allocation tags could attribute per arm but not per run,
  and would change the model id in every deployment.

- [ ] **Both credentials expire mid-run, and now every arm is exposed.** The
  mantle bearer token is minted in memory by
  `smoke_bedrock.derive_mantle_token`; the SigV4 keys are frozen out of the SSO
  session by `smoke_test.freeze_sigv4_credentials`. Both inherit the SSO
  session's expiry — hours, not days. Phase 4 is 3–4 days mostly unattended, so
  the mantle arms start returning 401 and the runtime arms start failing
  signature validation partway through. No refresh path exists for either.
  Widened 2026-08-07: before Sonnet moved to bedrock-runtime this touched the
  candidates only, and the reference arm would have survived it.

- [ ] **Throttling makes the exclusion rate load-dependent.** No 429s at N=1. At
  scale they are routine, and the exclusion rate then correlates with when and
  how parallel the run was rather than with the model. §5.7's randomize-and-
  interleave is the mitigation and the spec says to verify it, not assume it.

- [ ] **Confirm Bedrock service quotas** for the concurrency the plan needs
  (OPEN-3). Wall clock, not cost, is the binding constraint.
  **Wall-clock and turn-count variance is the real problem, and N=3 exposed
  it.** On the *same one-line task*, Nemotron measured across two N=3 runs:

  | run | turns | wall |
  |---|---|---|
  | A1–A3 | 19 / 15 / 21 | 201.9s / 42.6s / 54.2s |
  | B1–B3 | 24 / 21 / 7 | 52.5s / 98.2s / 9.0s |

  A 22× spread in wall clock (9.0s to 201.9s) and 3.4× in turns, on identical
  work. The two do not track each other — B2 took 98.2s for 21 turns while A1
  took 201.9s for 19 — so per-turn latency is varying independently of how much
  the model chose to do.

  A single-sample estimate is therefore worthless for capacity planning: the
  original OPEN-3 sizing used one 311s sample, and 2,400 runs amplifies
  whichever end you picked. Size off the p95 of a repeated measurement, and
  measure it on a real task rather than this fixture.

- [ ] **`RunRecord.sampling` reflects the bridge, not just the config.** On the
  Responses path all three candidates reported `temperature=1.0`; switching to
  chat-completions made it read `absent`; it returned on nemotron's successful
  run. Pin which bridge each arm uses before the full run, or sampling
  provenance is unreliable across arms.

---

## P2 — Capture gaps found in passing

- [ ] **The wire log records a failure's status code but not its error body.**
  `BakeoffCallback._record` stores `response_obj`, which is `None` on failure,
  so a 400 lands as `{"raw_completion": "None"}`. Every diagnosis on
  2026-08-07 had to come from the proxy's own logs instead — and in the full
  run the proxy log is not an artifact of the record. §6.2 exists so a failure
  can be diagnosed after the fact; right now it cannot be.
  Confirmed again on the second run: the failing mantle Sonnet arm recorded
  exactly `{"status_code": 400, "raw_completion": "None"}`, and `invalid beta
  flag` was recoverable only by grepping `proxy.log`. Two runs, two diagnoses
  that the event log could not support on its own.

- [ ] **Every dataset task repo needs a `.gitignore`.** §5.6 stages everything
  (`git add -A`), so the first live run's diff led with a binary
  `__pycache__/calc.cpython-312.pyc`. Without this, diff size and file counts
  measure the interpreter rather than the agent. Done for the smoke fixture
  only.

- [ ] **`ToolCallStats.malformed` is never populated at harness time**, so
  `TOOL_MALFORMATION` and `ADAPTER_FAILURE` are both unreachable — and §6.4
  calls the adapter-vs-model distinction the eval's most consequential call.
  Deferred to the wire-log analysis in the scoring plan; nothing can fire it
  until that lands.

- [ ] **§5.2's config dump is an artifact, not a record field.** Written to
  `artifacts_root/<arm>/effective_config.json` and diffed across arms by the
  smoke script. The spec asks for it stored *in the run record*, which needs an
  `Artifacts.effective_config_json` field and a schema bump to **2.1.0** —
  1.2.0 was reserved here, but the pricing change took the schema to 2.0.0.

- [ ] **The adapter coerces `finish_reason` and the harness reads the result as
  intent.** LiteLLM's openai→anthropic translation maps `stop`→`end_turn`,
  `length`→`max_tokens`, `tool_calls`→`tool_use`, and **anything else to
  `end_turn`**. `assemble_record` treats `end_turn` as the agent deciding it
  was finished (`TerminationReason.AGENT_FINISH`), so an unrecognised provider
  finish_reason is silently recorded as a deliberate stop. The raw value is
  still in the wire log (`choices[0].finish_reason`); record it beside the
  translated `stop_reason` so the offline grader can tell a real end_turn from
  a coerced one.

- [ ] **Truncated tool-call JSON is silently repaired.** On the non-streaming
  adapter path LiteLLM closes unmatched brackets in tool arguments and returns
  the result as though the model emitted it — a repaired `Edit` is a different
  edit than the model asked for, and the only trace is a log line. Streaming
  forwards raw `input_json_delta` and does no repair, so this bites the
  occasional non-streaming call rather than every turn. Detect offline by
  comparing the wire log's raw arguments against the transcript.

- [ ] **`ToolCallStats.malformed` is hard-zero, so `ADAPTER_FAILURE` is
  unreachable** — and the Kimi tool-id bug was precisely an adapter failure the
  harness could never have flagged. Keep `malformed` at 0 at harness time (§6.4
  puts the adapter-vs-model call offline) and write the detector as a pass over
  `wire.jsonl.gz`: the set of `tool_use` ids in a response must equal the set
  of `tool_result` ids in the next request. That check would have caught the
  id-mangling defect directly, without a live run.

- [ ] **`TokenUsage.reasoning` is structurally 0 on all three candidates.** The
  adapter's usage translation emits input/output/cache fields and has no
  reasoning field to emit, so a zero there is "not representable", not "none
  used". Measure whether reasoning tokens are excluded from `completion_tokens`
  before publishing any cost-per-token comparison — if they are, the candidates'
  output is undercounted against Sonnet's.

---

## P3 — Decisions to settle before numbers are published

- [ ] **Sonnet 5 pricing.** `PRICE_BOOK` carries standard $3/$15 while Anthropic
  lists introductory $2/$10 through 2026-08-31. Whether Bedrock mirrors the
  introductory rate is unverified. Check the AWS pricing page before any cost
  figure leaves the team.

- [ ] **Sonnet's tokenizer is ~30% denser.** A uniform token cap gives Sonnet
  ~30% less *text* budget than the other arms — a config choice that would be
  scored as a capability difference (§5.4) — and cost-per-task is not directly
  comparable at equal text. Caps derive from the Phase 3 calibration pilot;
  decide the policy there.

- [ ] **`task_set_commit` stays `""`** by design until the dataset plan exists.
  Asserted empty on purpose so nobody reads it as populated.

- [ ] **`Checkpoint.tests_pass` stays `None`** by design — §5.5 requires offline
  grading. Lands with the scoring plan.

---

## Housekeeping

- [ ] **20 commits unpushed.** Pushed manually.

- [ ] **`bakeoff/.env` holds three commented-out static AWS credential lines
  with plaintext values**, annotated in-file as an expired STS session
  superseded by the SSO profile. Inert and gitignored, but a real secret-key /
  session-token pair sitting on disk with no expiry tracking. Delete the lines.

---

## Out of scope here — each needs its own plan

1. **Dataset construction** — transcript/PR/Jira join, task harvesting,
   container builds, stratification (§3)
2. **Scoring** — deterministic checks, offline checkpoint grading, judge
   protocol, κ calibration (§4)
3. **Analysis and reporting** — paired cluster bootstrap, pass@1/pass^k, Elo,
   scorecard (§10)
